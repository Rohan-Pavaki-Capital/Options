"""Stage 3 — standardize the balance-sheet markdown into the fixed schema.

Builds the messages, calls the chat LLM (Together AI, OpenAI-compatible),
strips markdown fences, parses and validates the JSON. If the output is not
valid JSON or is missing required keys, it re-prompts once.

NOTE: LlamaParse only parses PDFs to markdown; the schema-mapping reasoning
happens here, in a chat LLM.
"""

import copy
import json
import logging
import re

from openai import OpenAI

from . import config
from .config import LLM_BASE_URL, LLM_MODEL, empty_result, require_together_key

logger = logging.getLogger("balance_sheet.standardizer")

SYSTEM_PROMPT = """You are a financial-statement standardizer. You are given the markdown of ONE company's
balance sheet. Map every line item into the FIXED schema provided. Return ONLY a JSON object
matching the schema — no explanation, no markdown fences.

RULES:
- Use ONLY the most recent period column (the left/most-recent date). Ignore prior-period
  columns entirely.
- Copy numbers EXACTLY as printed. Do NOT convert units, scale, or round. Strip only the
  currency symbol and thousands separators (commas). Negative numbers stay negative.
- Map EVERY asset line into exactly one asset bucket OR memo field, and EVERY liability line
  into exactly one liability bucket OR memo field. Do not drop any line. If a line has no
  obvious home, put it in the closest 'other_*' bucket (other_assets / other_current_assets /
  other_liabilities / other_current_liabilities) so the totals still reconcile.
- MEMO FIELDS (the downstream workbook consumes these separately — they are NOT buckets and
  their lines must NEVER also appear in any bucket):
  - memo.cash_and_st_investments = ONLY lines explicitly named cash / cash equivalents /
    short-term investments / current marketable securities. NOT "other current assets", NOT
    prepaid expenses, NOT restricted cash, NOT custodial/performance-bond balances (all of
    those stay in the buckets), NOT long-term investments (investment_assets).
  - memo.goodwill_and_intangibles = printed goodwill + identifiable/other intangible asset
    lines. This includes lines named ONLY "Intangible assets, net" / "Acquired intangibles"
    etc. — the word goodwill need not appear. Only lines the filing prints as
    goodwill/intangibles — a mixed "other non-current assets" line that merely INCLUDES
    intangibles stays in other_assets.
  - memo.long_term_debt = non-current interest-bearing debt (long-term debt, non-current
    borrowings, bonds/notes due beyond one year).
- Distinguish current vs non-current using the filing's own sub-headers when present. If the
  filing is unclassified (common for REITs/banks), use judgement: cash, receivables,
  inventory, short-term items -> current; property, long-term investments, intangibles ->
  non-current.
- DEBT SPLIT: map interest-bearing debt by the filing's current/non-current classification.
  - Current portion of long-term debt, notes payable, short-term borrowings, commercial paper,
    current debt -> current.debt.
  - Long-term debt / non-current borrowings -> memo.long_term_debt (there is no non-current
    debt bucket; long-term debt is a memo field, NEVER other_liabilities).
  Never lump long-term debt into current.debt.
  (If the filing is unclassified, use judgement: revolvers/commercial paper/current maturities
  -> current.debt; term debt, notes and bonds -> memo.long_term_debt.)
- DEFERRED TAX / INCOME TAX:
  - A PURE deferred income tax (or deferred revenue) liability line -> deferred_rev_and_tax
    (current or non-current per the filing).
  - A COMBINED line like "Deferred income taxes and other liabilities" -> non_current
    other_liabilities as a whole (exactly ONE bucket; do not split unless the filing itself
    splits it; deferred_rev_and_tax then stays 0 unless a pure deferred line also exists).
  - Income taxes payable (current) -> current.deferred_rev_and_tax.
  - Accrued liabilities -> other_current_liabilities.
  Do not double-count: each of these lines goes to exactly one bucket.
- ACCRUED INTEREST and any miscellaneous payables must be mapped (usually other_liabilities /
  other_current_liabilities) — never omitted, or liabilities will under-count.
- Equity is NOT part of the buckets. Do not map equity lines into liability buckets.
  (preferred_stock and mezzanine_equity are the only equity-adjacent buckets — fill them only
  if the filing explicitly shows preferred stock or mezzanine/temporary equity.)
  Common stock at stated/par value, capital in excess of stated/par value, additional paid-in
  capital, retained earnings, treasury stock and accumulated other comprehensive income are
  ordinary EQUITY — NEVER map them into preferred_stock or mezzanine_equity (or any bucket).
  mezzanine_equity is ONLY for temporary equity shown outside permanent equity (e.g. redeemable
  preferred stock, redeemable noncontrolling interests); if that line prints a dash/zero, leave
  the bucket 0.
- NEVER invent a number. Every bucket value must be a printed line value (or a ' + ' chain of
  printed line values) from the most-recent column. If the buckets do not sum to the printed
  total, the fix is ALWAYS re-mapping printed lines — never inserting a balancing figure.
- filing_totals.total_assets = the filing's PRINTED "Total assets" (most recent column).
  filing_totals.total_liabilities = the filing's PRINTED "Total liabilities" line. If no
  explicit "Total liabilities" line exists, compute it as printed Total liabilities & equity
  minus total equity, and note this in reasoning is NOT allowed — instead put the value you
  derive and add a note is also NOT allowed; if there is genuinely no total-liabilities line,
  set it to the sum of all liability lines you mapped.
- unit_label: copy the scale wording from the filing header ("in thousands"/"in millions")
  for labelling only. Never use it to scale the numbers.

FIELD-BY-FIELD MAPPING TABLE (filing particulars -> template field; this is the canonical
mapping — the rules above/below refine edge cases):

Assets, non-current:
- lease_assets            <- operating lease right-of-use (ROU) assets (non-current)
- real_estate_assets      <- land / buildings / property held (where broken out — REITs and
                             property companies only)
- investment_assets       <- trading/investment securities held long-term
- investment_in_other     <- long-term investments / equity-method investments / investments
                             in affiliates, unconsolidated JVs or other companies
- assets_held_for_sale    <- assets (of properties) held for sale
- asset_from_discontinued_business <- assets of discontinued operations
- pension_assets          <- pension / post-retirement plan assets (overfunded plans)
- other_assets            <- deferred income tax assets + other non-current assets
                             (EXCLUDING goodwill & intangibles — those are memo)
- ppe                     <- property, plant & equipment, net

Assets, current:
- lease_assets (current)  <- current portion of lease/ROU assets
- inventory               <- physical inventory / commodities
- accounts_trade_receivable <- trade accounts receivable, net (+ vendor receivables)
- tax                     <- current income tax receivable / current tax assets
- other_current_assets    <- every remaining current line (prepaid expenses, restricted cash,
                             misc current items) = total current assets minus the specific
                             buckets minus cash items (cash/ST investments are memo, NEVER
                             mapped here)

Liabilities, non-current:
- pension                 <- pension / post-retirement benefit obligations
- lease_liabilities       <- operating lease liabilities, non-current
- deferred_rev_and_tax    <- deferred revenue (non-current) + PURE deferred income tax
                             liability lines
- other_liabilities       <- other non-current liabilities (incl. combined "deferred income
                             taxes and other liabilities" lines; NEVER long-term debt — memo)

Liabilities, current:
- debt                    <- short-term debt / borrowings + current portion of long-term debt
                             + notes payable + commercial paper (long-term debt itself ->
                             memo.long_term_debt)
- lease_liabilities (current) <- operating lease liabilities, current
- accounts_trade_payable  <- accounts payable
- deferred_rev_and_tax (current) <- deferred revenue (current) + income taxes payable
- other_current_liabilities <- accrued expenses & other current liabilities

Equity-adjacent:
- preferred_stock         <- genuine preferred stock only
- mezzanine_equity        <- redeemable/temporary equity only — never ordinary common stock
                             (e.g. NIKE Class B common "3" is ordinary equity, excluded from
                             all buckets)

Never mapped into any bucket (memo fields — the workbook adds them independently):
cash + short-term investments -> memo.cash_and_st_investments ; goodwill and intangibles ->
memo.goodwill_and_intangibles ; long-term debt -> memo.long_term_debt.

SINGLE-BUCKET RULE (critical):
- Every source line contributes to EXACTLY ONE bucket or memo field.
- If a line is placed in a specific bucket or memo field (ppe, investment_assets,
  real_estate_assets, inventory, accounts_trade_receivable, debt, accounts_trade_payable,
  deferred_rev_and_tax, memo.cash_and_st_investments, memo.goodwill_and_intangibles,
  memo.long_term_debt, etc.), it MUST NOT also be included in any other_* bucket.
- other_* buckets (other_assets, other_current_assets, other_liabilities,
  other_current_liabilities) contain ONLY the lines not already captured by a specific bucket
  or memo field.
- Never take a subtotal AND its components. Map the individual line items, never a section
  subtotal (e.g. do not map "Total non-current assets" — map the lines under it).
- Self-verify before returning: sum of ALL asset buckets + memo.cash_and_st_investments +
  memo.goodwill_and_intangibles must equal printed Total Assets, and sum of ALL liability
  buckets + memo.long_term_debt must equal printed Total Liabilities. If they do not, you have
  either double-counted (same line in two places) or missed a line — fix it before returning.

OFFSETTING / CUSTODIAL BALANCES (critical for banks, brokers, exchanges, clearing houses):
Performance-bond, guaranty-fund, margin deposits, segregated customer funds, and other
custodial balances appear on BOTH sides of the balance sheet in near-equal amounts — the
entity holds cash/securities as an ASSET and owes them back as a LIABILITY. If you map such a
balance as a liability (e.g. into other_current_liabilities), you MUST also map its
corresponding asset (the cash and securities held as that collateral) into an asset bucket
(other_current_assets or other_assets — custodial balances are NOT memo cash). NEVER map one
side without the other.
After mapping, asset buckets + asset memo fields MUST equal printed Total Assets and liability
buckets + memo.long_term_debt MUST equal printed Total Liabilities. If assets fall short by
roughly the size of a large custodial liability you mapped, you omitted the matching custodial
asset — add it.

BUCKET PLACEMENT RULES (map into the CORRECT bucket, not just any bucket that makes the total
tie):

1. RESPECT THE FILING'S CURRENT vs NON-CURRENT HEADERS. If the balance sheet has "Current
   Assets" / "Current Liabilities" sections (most GAAP filings do), every line under those
   headers MUST go into a CURRENT bucket, and everything else into a NON-CURRENT bucket. Never
   put a line the filing lists as current into a non-current bucket or vice-versa.

2. CASH & MARKETABLE SECURITIES: map cash, cash equivalents, and current marketable /
   short-term securities into memo.cash_and_st_investments — NEVER into other_current_assets.
   Restricted cash and custodial balances stay in the buckets. NON-current holdings split per
   the mapping table: long-term trading/investment securities -> investment_assets ;
   long-term investments / equity-method / affiliate / JV stakes -> investment_in_other ;
   a mixed 'other' line that merely includes investments -> other_assets.

3. PP&E vs REAL ESTATE:
   - "Property, plant and equipment", "Property and equipment, net", "Property, net of
     depreciation" -> ppe.
   - real_estate_assets is ONLY for entities whose business is holding real estate (REITs:
     "Real estate properties", "Buildings and improvements", "Land"). Do NOT put ordinary
     operating PP&E into real_estate_assets.

4. INTANGIBLES & GOODWILL: map lines the filing prints as goodwill or intangible assets into
   memo.goodwill_and_intangibles — NEVER into other_assets. A mixed line ("Other assets" that
   merely includes intangibles, or "Deferred income taxes and other assets") stays in
   other_assets.

5. CUSTODIAL / PERFORMANCE-BOND / CLEARING BALANCES: map to the side AND the current/non-
   current level the filing shows. E.g. "Performance bonds and guaranty fund contributions"
   listed under Current Assets -> other_current_assets; the matching one under Current
   Liabilities -> other_current_liabilities. (Both sides must be mapped — see offsetting rule.)

6. DEBT SPLIT: map interest-bearing debt by the filing's current/non-current classification —
   current portion of long-term debt, notes payable, short-term borrowings, commercial paper
   -> current.debt ; long-term debt / non-current borrowings -> memo.long_term_debt (never
   other_liabilities). Never lump long-term debt into current.debt.

7. other_* buckets are a LAST RESORT for lines with no specific bucket — not a dumping ground.
   Only use them for genuinely miscellaneous items (and custodial balances per rule 5). Never
   move a line that has a correct specific bucket or memo field (ppe,
   accounts_trade_receivable, inventory, debt, cash, goodwill, long-term debt, etc.) into an
   other_* bucket.

After mapping: verify (a) every current-section line is in a current bucket and every
non-current line in a non-current bucket, AND (b) asset buckets + asset memo fields equal
printed Total Assets and liability buckets + memo.long_term_debt equal printed Total
Liabilities.

WORKED EXAMPLES (correct placement):

Example A — NIKE (classified GAAP 10-Q):
  Current assets: Cash 6,660 + Short-term investments 1,397 -> memo.cash_and_st_investments =
  8,057 ; AR net 5,369 -> accounts_trade_receivable ; Inventories 7,487 -> inventory ;
  Prepaid expenses and other current assets 2,271 -> other_current_assets.
  Non-current: PP&E net 4,766 -> ppe ; Operating lease right-of-use assets 2,886 ->
  lease_assets ; Identifiable intangible assets 259 + Goodwill 240 ->
  memo.goodwill_and_intangibles = 499 ; "Deferred income taxes and other assets" 5,729 ->
  other_assets (mixed line, NOT memo).
  Current liabilities: Current portion of long-term debt 999 -> current.debt ; Notes payable 0
  -> current.debt ; Accounts payable 2,888 -> accounts_trade_payable ; Current portion of
  operating lease liabilities 493 -> current.lease_liabilities ; Accrued liabilities 6,183 ->
  other_current_liabilities ; Income taxes payable 275 -> current.deferred_rev_and_tax.
  Non-current: Long-term debt 7,030 -> memo.long_term_debt ; Operating lease liabilities 2,656
  -> non_current.lease_liabilities ; "Deferred income taxes and other liabilities" 2,450 ->
  non_current.other_liabilities (combined line; deferred_rev_and_tax stays 0).
  Asset buckets 28,508 + memo 8,057 + 499 = printed 37,064 ; liability buckets 15,944 + memo
  7,030 = 22,974.

Example B — CME (exchange/clearing house), assets:
  Filing "Current Assets": Cash 2,391.2 + Marketable securities 124.2 ->
  memo.cash_and_st_investments = 2,515.4 ; AR 935.5 -> accounts_trade_receivable ; Other
  current 515.0 + Performance bonds 165,035.3 -> other_current_assets = 165,550.3 (custodial
  collateral is NOT memo cash).
  Filing non-current: Property net 355.4 -> ppe ; Intangibles 17,175.3 + 2,550.8 + Goodwill
  10,506.0 -> memo.goodwill_and_intangibles = 30,232.1 ; Other 2,404.5 -> other_assets.
  Buckets + memo tie to printed 201,993.5. NOTE 355.4 is ppe (not real_estate_assets).

Example C — REIT (e.g. Diversified Healthcare Trust):
  "Real estate properties, net" -> real_estate_assets (this IS the business). Property lines
  here are real_estate_assets, NOT ppe. (Contrast with Example B.)
  "Investments in unconsolidated joint ventures" -> investment_in_other.

If you are given a CORRECTION note describing an over- or under-count, fix ONLY the indicated
issue: move the offending amount out of / into the correct bucket or memo field so every line
is counted exactly once and buckets + memo fields equal the printed Total Assets and Total
Liabilities. Return the full corrected JSON, same schema, numbers as printed.

Return the JSON now.
"""


_CELL_NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?")

# Placeholder a filing prints for a zero/nil value in a column.
_DASH_CELLS = {"—", "–", "-", "--"}


def extract_line_items(markdown: str) -> list[tuple[str, float]]:
    """Pull (label, most-recent-column value) pairs from the markdown table
    IN CODE, skipping subtotal/total rows. Giving the LLM this checklist
    means it only classifies labels into buckets — it never re-reads the
    table, so it cannot hallucinate values, use the prior-period column, or
    map a subtotal row."""
    items = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        label = cells[0].strip("* ").strip()
        if not label or label.lower().startswith("total"):
            continue  # header/subtotal/total rows never map into buckets
        for cell in cells[1:]:
            raw = cell.strip("* ").replace("$", "").strip()
            if raw in _DASH_CELLS:
                # Most-recent column prints a dash = zero. Stop here — falling
                # through would read the PRIOR-period column (e.g. NIKE
                # "Notes payable | — | 5" must be 0, not 5).
                items.append((label, 0))
                break
            negative = raw.startswith("(") and raw.endswith(")")
            raw = raw.strip("()").strip()
            m = _CELL_NUM_RE.fullmatch(raw)
            if m:
                value = float(raw.replace(",", ""))
                if negative:
                    value = -value
                items.append((label, int(value) if value == int(value) else value))
                break  # first numeric/dash cell = most-recent column
    return items


def _build_user_message(markdown: str) -> str:
    schema = json.dumps(empty_result(), indent=2)
    items = extract_line_items(markdown)
    items_block = ""
    if len(items) >= 5:
        listing = "\n".join(f"- {label}: {value}" for label, value in items)
        items_block = (
            "LINE ITEMS (extracted in code from the most-recent column; "
            "subtotal/total rows already removed). Bucket EVERY asset and "
            "liability line below into exactly one bucket using these EXACT "
            "values — do not re-read them from the markdown. Equity lines "
            "stay out of the buckets:\n"
            f"{listing}\n\n"
        )
    return (
        "TARGET SCHEMA (return EXACTLY this shape; the bucket keys are FIXED — "
        "do not invent new keys):\n"
        f"{schema}\n\n"
        "NUMBERS: no digit-group commas inside a number. When several "
        "balance-sheet lines map into the same bucket, do NOT add them "
        "yourself — write the printed values joined by ' + ' (e.g. "
        '"other_assets": 100 + 200 + 300) and the caller will compute the '
        "exact sum. Never any other arithmetic. Every number you write must "
        "be copied character-for-character from the most-recent column of "
        "the markdown — never write a number that does not appear there, and "
        "never repeat a line's value in a second bucket.\n\n"
        f"{items_block}"
        "MAPPING HINTS for lines with no named bucket (they must NEVER be "
        "dropped, or the totals will not reconcile):\n"
        "- Cash and cash equivalents + short-term investments / current "
        "marketable securities -> memo.cash_and_st_investments (NOT "
        "other_current_assets). Prepaid expenses, restricted cash, current "
        "derivative/receivable odds and ends -> current.other_current_assets.\n"
        "- Goodwill and intangible-asset lines -> memo.goodwill_and_intangibles "
        "(NOT other_assets). Non-current derivatives, deferred tax assets, "
        "mixed 'deferred taxes and other assets' lines -> "
        "non_current.other_assets.\n"
        "- Right-of-use assets -> lease_assets (non_current unless the "
        "filing shows a current portion). Never fold ROU assets into ppe.\n"
        "- Long-term trading/investment securities -> investment_assets ; "
        "long-term investments / equity-method / affiliate / JV stakes -> "
        "investment_in_other.\n"
        "- Pension or post-retirement obligations -> non_current.pension ; "
        "overfunded pension plan assets -> pension_assets.\n"
        "- Interest-bearing debt splits by the filing's classification: "
        "current portion of long-term debt / notes payable / short-term "
        "borrowings -> current.debt ; long-term debt / non-current "
        "borrowings -> memo.long_term_debt (never lumped into current.debt "
        "or other_liabilities).\n"
        "- Income taxes payable -> current.deferred_rev_and_tax ; accrued "
        "liabilities -> other_current_liabilities ; a combined 'deferred "
        "income taxes and other liabilities' line -> non_current."
        "other_liabilities as a whole.\n"
        "- A printed line literally named 'Other liabilities' / 'Other "
        "assets' maps straight into the matching other_* bucket (current "
        "level per the filing's headers; non-current when unclassified) — "
        "it must NEVER be dropped.\n"
        "- Real estate blocks (Land / Buildings and improvements / less "
        "Accumulated depreciation) belong TOGETHER in exactly ONE bucket: "
        "real_estate_assets for REITs/property companies (include the "
        "negative accumulated depreciation), ppe otherwise. Never repeat or "
        "split the same property block across ppe AND real_estate_assets.\n"
        "- NEVER map subtotal or total rows (current-assets subtotal, total "
        "assets, total liabilities, total equity) into any bucket — "
        "individual line items only, or they will be double-counted.\n\n"
        "BALANCE SHEET MARKDOWN:\n"
        f"{markdown}"
    )


def _call_llm(messages: list[dict]) -> str:
    client = OpenAI(base_url=LLM_BASE_URL, api_key=require_together_key())
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0,
        max_tokens=4096,
    )
    return response.choices[0].message.content or ""


def _strip_fences(text: str) -> str:
    """Strip markdown fences / stray prose — keep the outermost JSON object."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in LLM output.")
    return text[start : end + 1]


# Unquoted number with digit-group commas, e.g.  "tax": 1,234,567
_GROUPED_NUM_RE = re.compile(r"(?<=[:\[,\s])(-?\d{1,3}(?:,\d{3})+)(?=\s*[,\}\]])")
# Literal addition/subtraction chain the model sometimes emits when several
# lines map into one bucket, e.g.  "other_assets": 178281 + 801709 + 89846
_EXPR_RE = re.compile(
    r"(?<=:)\s*(-?\d+(?:\.\d+)?(?:\s*[+-]\s*\d+(?:\.\d+)?)+)(?=\s*[,\n\}])"
)


def _repair_json_numbers(text: str) -> str:
    """Deterministic repair of two invalid-JSON number forms the LLM emits.
    Both keep the values exactly as the model wrote them: commas are stripped
    (the one allowed cleaning), and literal +/- chains are summed in code —
    the terms are the filing's own printed line values."""
    text = _GROUPED_NUM_RE.sub(lambda m: m.group(1).replace(",", ""), text)

    def _sum_expr(match: "re.Match") -> str:
        total = 0.0
        for term in re.findall(r"[+-]?\s*\d+(?:\.\d+)?", match.group(1)):
            total += float(term.replace(" ", ""))
        return f" {int(total)}" if total == int(total) else f" {total}"

    return _EXPR_RE.sub(_sum_expr, text)


def _validate(result: dict) -> None:
    """Ensure the exact schema shape — all fixed keys, no invented bucket keys."""
    template = empty_result()
    for top_key in template:
        if top_key not in result:
            raise ValueError(f"Missing required key: {top_key!r}")

    sections = {
        ("assets", "non_current"): config.ASSET_NON_CURRENT_KEYS,
        ("assets", "current"): config.ASSET_CURRENT_KEYS,
        ("liabilities", "non_current"): config.LIABILITY_NON_CURRENT_KEYS,
        ("liabilities", "current"): config.LIABILITY_CURRENT_KEYS,
    }
    for (group, sub), keys in sections.items():
        block = result.get(group, {}).get(sub)
        if not isinstance(block, dict):
            raise ValueError(f"Missing or invalid section: {group}.{sub}")
        for k in keys:
            if k not in block:
                raise ValueError(f"Missing bucket key: {group}.{sub}.{k}")
        extra = set(block) - set(keys)
        if extra:
            raise ValueError(f"Invented bucket keys in {group}.{sub}: {sorted(extra)}")

    for k in ("preferred_stock", "mezzanine_equity"):
        if k not in result["liabilities"]:
            raise ValueError(f"Missing key: liabilities.{k}")
    memo = result.get("memo")
    if not isinstance(memo, dict):
        raise ValueError("Missing or invalid section: memo")
    for k in config.MEMO_KEYS:
        if k not in memo:
            raise ValueError(f"Missing memo key: memo.{k}")
    extra = set(memo) - set(config.MEMO_KEYS)
    if extra:
        raise ValueError(f"Invented memo keys: {sorted(extra)}")
    for k in ("total_assets", "total_liabilities"):
        if k not in result.get("filing_totals", {}):
            raise ValueError(f"Missing key: filing_totals.{k}")


def _parse_and_validate(raw: str) -> dict:
    text = _strip_fences(raw)
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = json.loads(_repair_json_numbers(text))
    _validate(result)
    # Normalize optional blocks the LLM may omit or malform.
    template = empty_result()
    result.setdefault("warnings", [])
    if not isinstance(result["warnings"], list):
        result["warnings"] = [str(result["warnings"])]
    if not isinstance(result.get("tally"), dict):
        result["tally"] = copy.deepcopy(template["tally"])
    return result


def standardize(markdown: str) -> dict:
    """Map the balance-sheet markdown into the fixed schema. Re-prompts once
    if the LLM output fails to parse/validate; raises if it fails twice."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(markdown)},
    ]
    raw = _call_llm(messages)
    try:
        return _parse_and_validate(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("LLM output invalid (%s) — re-prompting once.", exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Your previous output was invalid: {exc}. Return ONLY a "
                    "valid JSON object matching the target schema exactly — "
                    "all fixed keys present, no extra keys, no markdown "
                    "fences, no prose."
                ),
            },
        ]
        raw = _call_llm(retry_messages)
        return _parse_and_validate(raw)  # let a second failure raise


def restandardize(markdown: str, previous_json: dict, gap_message: str) -> dict:
    """Tally-failure hook (Stage 4): re-call the LLM with the previous JSON
    and the exact gap so it can re-map missing/double-counted lines. Called
    in a loop by the pipeline until balanced or max retries."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(markdown)},
        {"role": "assistant", "content": json.dumps(previous_json)},
        {
            "role": "user",
            "content": (
                f"CORRECTION note: {gap_message} Cross-check the LINE ITEMS "
                "checklist first: EVERY asset/liability line in it must appear "
                "in exactly one bucket or memo field — find the line(s) you "
                "left out or double-counted; the gap equals their sum. Then "
                "correct the previous JSON with the SMALLEST "
                "possible edit: change ONLY the bucket(s) implicated by the "
                "gap(s) above and copy every other bucket value UNCHANGED "
                "from the previous JSON. When a bucket holds several lines, "
                "rewrite it as the printed values joined by ' + ' (e.g. 100 + "
                "200), copying each number character-for-character from the "
                "most-recent column of the markdown — never a value already "
                "placed in another bucket, never a subtotal row, never a "
                "number that is not printed there. Numbers exactly as printed "
                "(strip only '$' and commas), same fixed schema. Return ONLY "
                "the corrected JSON object."
            ),
        },
    ]
    raw = _call_llm(messages)
    return _parse_and_validate(raw)

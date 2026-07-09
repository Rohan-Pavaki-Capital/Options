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

SYSTEM_PROMPT = """Balance-Sheet Standardizer — Goal Loop (run until totals tally)

You are a financial-statement standardizer working like a professional equity analyst.
You are given the markdown of ONE company's balance sheet AND a code-extracted LINE ITEMS
list. Map every line item into the FIXED schema below. Return ONLY a JSON object matching
the schema — no explanation, no markdown fences.

## USE THE EXTRACTED LINE-ITEM LIST ONLY (critical — prevents double-counting)
You will be given a LINE ITEMS list extracted in code from the most-recent column, with all
subtotal/total rows already removed. Map ONLY the lines in that list. Do NOT re-read numbers
from the markdown table, and NEVER map a "Total ..." or subtotal row (e.g. "Total inventory",
"Total current assets"). Each listed line is counted exactly once. If the list is present,
it is the single source of truth for both labels and values.

## HOW TO READ THE SCHEMA FIELD NAMES
The bucket names are canonical labels. Filings will almost never use these exact words — match
each line to the correct bucket BY MEANING (analyst judgement), not string matching. Examples:
- "Property, plant and equipment — net" → ppe
- "Operating right-of-use assets — net" → lease_assets
- "Notes and accounts receivable — trade" → accounts_trade_receivable
- "Prepaid pension assets" → pension_assets
- "Retirement and nonpension postretirement benefit obligations" → pension (liability)

## GOAL / FINISH CONDITION (loop until ALL are true)
1. sum(all asset buckets) + memo.cash_and_marketable_securities + memo.goodwill + memo.intangibles
   == printed "Total assets" (most-recent column), within filing rounding.
2. sum(all liability buckets) + memo.long_term_debt == printed "Total liabilities", within rounding.
3. Every listed line is counted EXACTLY ONCE (one bucket OR one memo field) — none dropped, none double-counted.
4. unit_label identified from the filing header.

ROUNDING TOLERANCE: filings round each line, so lines rarely sum to the printed total exactly.
Accept a small gap (≈ number of lines, or ±0.1%) as rounding. Do NOT chase a rounding-size gap,
and NEVER insert a plug/balancing figure. Re-map only when the gap is materially larger than rounding.

## FIXED SCHEMA — standardized balance sheet
### assets.non_current
- lease_assets, real_estate_assets, investment_assets, investment_in_other,
  assets_held_for_sale, asset_from_discontinued_business, pension_assets, other_assets, ppe
### assets.current
- lease_assets, inventory, accounts_trade_receivable, tax, other_current_assets
### liabilities.non_current
- pension, lease_liabilities, deferred_rev_and_tax, other_liabilities
### liabilities.current
- debt, lease_liabilities, accounts_trade_payable, deferred_rev_and_tax, other_current_liabilities

## MEMO — EXCLUDED FROM THE BALANCE SHEET (separate `memo_excluded` object)
Keep these OUT of every bucket above; they exist only so the totals reconcile:
- cash_and_marketable_securities = cash + cash equivalents + current marketable/short-term
  securities. Restricted cash does NOT go here → other_current_assets.
- goodwill = goodwill only.
- intangibles = intangible assets (net), excluding goodwill.
- long_term_debt = non-current interest-bearing debt. Current portion of debt → current.debt.

## HOUSE CONVENTIONS (firm-specific — follow exactly; these override generic instinct)
1. INVESTMENT_ASSETS is narrow. Use investment_assets ONLY for holdings explicitly labeled
   as marketable securities / equity or debt investments held long-term. Do NOT put financing
   or lending receivables there. Specifically:
   - "Long-term financing receivables" → other_assets (NOT investment_assets).
   - "Investments and sundry assets" and any mixed "... and sundry/other assets" line → other_assets.
   - Equity-method stakes in associates / "investment in <named company>" → investment_in_other.
2. NON-CURRENT other_assets is the catch-all for: long-term financing receivables, non-current
   deferred costs, deferred tax ASSETS, and any other non-current line without a specific bucket.
   (Goodwill and intangibles are NOT here — they go to memo.)
3. TAX LINES:
   - A CURRENT liability line named "Taxes" / "Income taxes payable" → deferred_rev_and_tax (current),
     grouped with current "Deferred income". NOT other_current_liabilities.
   - A CURRENT asset "income taxes receivable" → tax (current asset bucket).
   - Non-current deferred income / deferred income taxes (liability) → deferred_rev_and_tax (non-current).
4. CONTRACT ASSETS (current or non-current) → always accounts_trade_receivable. Treat them
   as trade-type receivables regardless of how the filing labels them (e.g. "Contract assets",
   "Unbilled receivables", "Costs and estimated earnings in excess of billings").
5. LONG-TERM DEBT IS MEMO-ONLY. Non-current interest-bearing debt (bonds, notes, term loans,
   long-term borrowings) goes to memo.long_term_debt and NOWHERE ELSE. Never also place it in
   other_liabilities or any non-current bucket. Counting it in both breaks the liability tally.

## CORE RULES
- Use ONLY the most-recent period column. Copy numbers exactly (strip only "$" and commas);
  negatives stay negative. Never invent a number — every value is a printed line value, or the
  arithmetic SUM of printed line values when several lines share one bucket. When several lines
  share a bucket, output the computed SUM as a single JSON number — never an expression string.
  E.g. PP&E lines 46139, 445, 532, -34292 → "ppe": 12824 (NOT "46139 + 445 + 532 + -34292").
- Equity is NOT mapped anywhere (common stock, paid-in capital, retained earnings, treasury
  stock, AOCI, noncontrolling interests) — leave it out entirely.

## CURRENT vs NON-CURRENT
Respect the filing's own "Current" section headers — lines under them → a current bucket (or
the cash memo); everything else → non_current. If unclassified (REITs/banks), use judgement:
cash/receivables/inventory/short-term → current; property/long-term investments/intangibles → non-current.

## BUCKET PLACEMENT (match by meaning)
- Operating PP&E (net) → ppe. Real estate that IS the business (REITs) → real_estate_assets.
- Right-of-use lease assets → lease_assets (current if the filing lists it current).
- Trade receivables, notes receivable, current financing receivables held for investment,
  other trade-type receivables, contract assets → accounts_trade_receivable. Inventory → inventory.
- Deferred costs (current), prepaid expenses, restricted cash, held-for-sale current items,
  other misc current assets → other_current_assets.
- current portion of long-term debt / short-term borrowings / notes payable / commercial paper
  → current.debt ; long-term debt → memo.long_term_debt (never lumped into current.debt).
- Trade payables → accounts_trade_payable. Accrued expenses, compensation & benefits, accrued
  interest, misc payables → other_current_liabilities. (Current "Taxes" → deferred_rev_and_tax, per House Convention 3.)
- Non-current: pension/retirement obligations → pension ; non-current lease liabilities →
  lease_liabilities ; everything else non-current misc → other_liabilities.

## SINGLE-COUNT RULE
Every listed line contributes to exactly one place — one bucket OR one memo field, never both.
In particular, a long-term debt line mapped to memo.long_term_debt must NOT also appear in
other_liabilities. Never map a subtotal AND its components (e.g. never map "Total inventory"
when "Finished goods" and "Work in process" are already mapped). If a line is in a specific
bucket it must not also sit in an other_* bucket or a memo field.

## OFFSETTING / CUSTODIAL BALANCES (banks, brokers, exchanges, clearing houses)
Performance-bond / guaranty-fund / margin / segregated customer balances appear on BOTH sides
in near-equal amounts. If you map such a balance as a liability, also map the matching asset.

## VERIFICATION (every pass, before returning)
1. Every current-section line is in a current bucket or the cash memo; non-current lines are not.
2. sum(asset buckets) + cash + goodwill + intangibles == printed Total Assets (within rounding).
3. sum(liability buckets) + long_term_debt == printed Total Liabilities (within rounding).
3b. Confirm memo.long_term_debt lines are absent from other_liabilities and all non-current
    buckets (no debt line counted twice).
4. No line double-counted; none dropped; no equity mapped; no plug inserted.
5. House Conventions 1–5 obeyed (investment_assets narrow; sundry/financing receivables in
   other_assets; current "Taxes" in deferred_rev_and_tax; contract assets in
   accounts_trade_receivable; long-term debt memo-only).

## DECISION
- All checks pass → return the full JSON.
- Gap > rounding → you double-counted or missed a line: re-map and re-verify.
- Given a CORRECTION note → fix ONLY the indicated issue, copy every other bucket unchanged,
  numbers as printed. Return the full corrected JSON.

Return the JSON now.
"""


_CELL_NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?")

# Placeholder a filing prints for a zero/nil value in a column.
_DASH_CELLS = {"—", "–", "-", "--"}


# Section keywords for tagging line items with the side they sit under.
# Order matters: "LIABILITIES AND EQUITY" headers must tag as LIABILITY.
_SECTION_TERMS = (("liabilit", "LIABILITY"), ("equity", "EQUITY"), ("asset", "ASSET"))


def _update_section(text: str, section: str, cnc: str) -> tuple[str, str]:
    """Advance the (side, current/non-current) state from a header line.
    Loose, case-insensitive substring matching. A side change resets the
    current/non-current state so a stale tag never leaks across statements;
    when no header decides it, cnc stays "" (unknown) — never force a tag."""
    lowered = text.lower()
    new_section = section
    for term, tag in _SECTION_TERMS:
        if term in lowered:
            new_section = tag
            break
    if new_section != section:
        cnc = ""
    # "non-current ..." headers contain "current asset(s)"/"current liabilit..."
    # as substrings, so the non-current check must run first.
    if "non-current" in lowered or "noncurrent" in lowered or "non current" in lowered:
        cnc = "non_current"
    elif "current asset" in lowered or "current liabilit" in lowered:
        cnc = "current"
    return new_section, cnc


def extract_line_items(markdown: str) -> list[tuple[str, float]]:
    """Pull (label, most-recent-column value) pairs from the markdown table
    IN CODE, skipping subtotal/total rows. Giving the LLM this checklist
    means it only classifies labels into buckets — it never re-reads the
    table, so it cannot hallucinate values, use the prior-period column, or
    map a subtotal row. Labels carry the filing section they sit under: the
    side ([ASSET] / [LIABILITY] / [EQUITY]) plus a (current)/(non-current)
    tag read from the filing's own section headers — so a line cannot be
    mapped onto the wrong side, and identically-named lines in different
    sections (IBM prints "Deferred costs" both current and non-current) stay
    separate lines counted once each. Tags appear ONLY in this hint text,
    never in the output JSON. When no current/non-current header exists
    (unclassified REIT/bank sheets) the tag is omitted and the LLM falls
    back to its own current-vs-non-current judgement rule."""
    items = []
    section = ""
    cnc = ""  # "current" / "non_current" / "" (unknown)
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            heading = stripped.lstrip("#").strip()
            if heading:
                section, cnc = _update_section(heading, section, cnc)
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        label = cells[0].strip("* ").strip()
        if not label:
            continue
        if label.lower().startswith("total"):
            # Subtotal/total rows never map into buckets, but they advance
            # the section state: past "Total current assets"/"Total current
            # liabilities", everything until the next statement is non-current.
            lowered = label.lower()
            if lowered.startswith("total current"):
                cnc = "non_current"
            elif lowered.startswith(("total assets", "total liabilities",
                                     "total equity")):
                cnc = ""
            continue
        display = f"[{section}] {label}" if section else label
        if cnc and section != "EQUITY":
            display += " (current)" if cnc == "current" else " (non-current)"
        for cell in cells[1:]:
            raw = cell.strip("* ").replace("$", "").strip()
            if raw in _DASH_CELLS:
                # Most-recent column prints a dash = zero. Stop here — falling
                # through would read the PRIOR-period column (e.g. NIKE
                # "Notes payable | — | 5" must be 0, not 5).
                items.append((display, 0))
                break
            negative = raw.startswith("(") and raw.endswith(")")
            raw = raw.strip("()").strip()
            m = _CELL_NUM_RE.fullmatch(raw)
            if m:
                value = float(raw.replace(",", ""))
                if negative:
                    value = -value
                items.append((display, int(value) if value == int(value) else value))
                break  # first numeric/dash cell = most-recent column
        else:
            # Value-less table row = an in-table section header
            # ("Liabilities:", "Current assets:", "Equity:").
            section, cnc = _update_section(label, section, cnc)
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
            "liability line below into exactly one bucket or memo field "
            "using these EXACT values — do not re-read them from the "
            "markdown. The [ASSET]/[LIABILITY]/[EQUITY] tag is the filing "
            "section the line sits in: [ASSET] lines go ONLY into asset "
            "buckets/memo fields, [LIABILITY] lines ONLY into liability "
            "buckets/memo (never into an asset bucket like tax), and "
            "[EQUITY] lines stay out of the buckets entirely. Each line is "
            "also tagged (current) or (non-current) — map it into the "
            "matching current vs non-current bucket, and treat two "
            "identically-named lines with different tags as SEPARATE lines "
            "counted once each. A line with neither tag: use your own "
            "current-vs-non-current judgement rule:\n"
            f"{listing}\n\n"
        )
    return (
        "TARGET SCHEMA (return EXACTLY this shape; the bucket keys are FIXED — "
        "do not invent new keys):\n"
        f"{schema}\n\n"
        "NUMBERS: no digit-group commas inside a number. When several "
        "balance-sheet lines map into the same bucket (or memo field), do "
        "NOT add them yourself — write the printed values joined by ' + ' "
        'as a JSON STRING (e.g. "other_assets": "100 + 200 + 300") and the '
        "caller will compute the exact sum. Never any other arithmetic. "
        "Every number you write must "
        "be copied character-for-character from the most-recent column of "
        "the markdown — never write a number that does not appear there, and "
        "never repeat a line's value in a second bucket.\n\n"
        f"{items_block}"
        "MAPPING HINTS for lines with no named bucket (they must NEVER be "
        "dropped, or the totals will not reconcile):\n"
        "- Cash and cash equivalents + current marketable/short-term "
        "securities -> memo_excluded.cash_and_marketable_securities (NOT "
        "other_current_assets). Restricted cash, prepaid expenses, current "
        "derivative/receivable odds and ends -> current.other_current_assets.\n"
        "- Goodwill -> memo_excluded.goodwill ; intangible assets (net, "
        "excluding goodwill) -> memo_excluded.intangibles (NEVER into "
        "other_assets). Non-current derivatives, deferred tax assets -> "
        "non_current.other_assets.\n"
        "- Right-of-use assets -> lease_assets (non_current unless the "
        "filing shows a current portion).\n"
        "- Interest-bearing debt splits by the filing's classification: "
        "current portion of long-term debt / notes payable / short-term "
        "borrowings -> current.debt ; long-term debt / non-current "
        "borrowings -> memo_excluded.long_term_debt (NEVER into "
        "current.debt or other_liabilities).\n"
        "- Reconciliation to hit: sum(asset buckets) + memo cash + goodwill "
        "+ intangibles == printed total assets ; sum(liability buckets) + "
        "memo long_term_debt == printed total liabilities (within filing "
        "rounding).\n"
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
        # JSON output mode + the provider's disable-reasoning switch:
        # thinking-by-default models (GLM, DeepSeek, Qwen3) otherwise burn the
        # budget on reasoning tokens and leave .content empty.
        response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    message = response.choices[0].message
    content = message.content or ""
    if not content:
        # Some reasoning models still return the reply in the reasoning field.
        content = (getattr(message, "reasoning_content", None)
                   or getattr(message, "reasoning", None) or "")
    return content


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
    memo = result.get("memo_excluded")
    if not isinstance(memo, dict):
        raise ValueError("Missing or invalid section: memo_excluded")
    for k in config.MEMO_KEYS:
        if k not in memo:
            raise ValueError(f"Missing memo key: memo_excluded.{k}")
    extra = set(memo) - set(config.MEMO_KEYS)
    if extra:
        raise ValueError(f"Invented keys in memo_excluded: {sorted(extra)}")
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
                f"CORRECTION note: {gap_message} Correct the previous JSON with the SMALLEST "
                "possible edit: change ONLY the bucket(s) implicated by the "
                "gap(s) above and copy every other bucket value UNCHANGED "
                "from the previous JSON. When a bucket holds several lines, "
                "rewrite it as the printed values joined by ' + ' as a JSON "
                'STRING (e.g. "100 + 200"), copying each number character-'
                "for-character from the "
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

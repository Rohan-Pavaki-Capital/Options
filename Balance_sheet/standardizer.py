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
- Map EVERY asset line into exactly one asset bucket, and EVERY liability line into exactly
  one liability bucket. Do not drop any line. If a line has no obvious bucket, put it in the
  closest 'other_*' bucket (other_assets / other_current_assets / other_liabilities /
  other_current_liabilities) so the totals still reconcile.
- Distinguish current vs non-current using the filing's own sub-headers when present. If the
  filing is unclassified (common for REITs/banks), use judgement: cash, receivables,
  inventory, short-term items -> current; property, long-term investments, intangibles ->
  non-current.
- DEBT: combine ALL interest-bearing debt tranches (e.g. senior secured notes, senior
  unsecured notes, secured debt & finance leases, term debt, commercial paper, revolving
  credit) into 'debt'. If the filing splits current vs non-current debt, put the current
  portion in current.debt; if unclassified, put total debt in current.debt.
- ACCRUED INTEREST, deferred items, and any miscellaneous payables must be mapped (usually
  other_liabilities / deferred_rev_and_tax) — never omitted, or liabilities will under-count.
- Equity is NOT part of the buckets. Do not map equity lines into liability buckets.
  (preferred_stock and mezzanine_equity are the only equity-adjacent buckets — fill them only
  if the filing explicitly shows preferred stock or mezzanine/temporary equity.)
- filing_totals.total_assets = the filing's PRINTED "Total assets" (most recent column).
  filing_totals.total_liabilities = the filing's PRINTED "Total liabilities" line. If no
  explicit "Total liabilities" line exists, compute it as printed Total liabilities & equity
  minus total equity, and note this in reasoning is NOT allowed — instead put the value you
  derive and add a note is also NOT allowed; if there is genuinely no total-liabilities line,
  set it to the sum of all liability lines you mapped.
- unit_label: copy the scale wording from the filing header ("in thousands"/"in millions")
  for labelling only. Never use it to scale the numbers.

SINGLE-BUCKET RULE (critical):
- Every source line contributes to EXACTLY ONE bucket.
- If a line is placed in a specific bucket (ppe, investment_assets, real_estate_assets,
  inventory, accounts_trade_receivable, debt, accounts_trade_payable, deferred_rev_and_tax,
  etc.), it MUST NOT also be included in any other_* bucket.
- other_* buckets (other_assets, other_current_assets, other_liabilities,
  other_current_liabilities) contain ONLY the lines not already captured by a specific bucket.
- Never take a subtotal AND its components. Map the individual line items, never a section
  subtotal (e.g. do not map "Total non-current assets" — map the lines under it).
- Self-verify before returning: sum of ALL asset buckets must equal printed Total Assets, and
  sum of ALL liability buckets must equal printed Total Liabilities. If they do not, you have
  either double-counted (same line in two buckets) or missed a line — fix it before returning.

Return the JSON now.
"""


_CELL_NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?")


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
            negative = raw.startswith("(") and raw.endswith(")")
            raw = raw.strip("()").strip()
            m = _CELL_NUM_RE.fullmatch(raw)
            if m:
                value = float(raw.replace(",", ""))
                if negative:
                    value = -value
                items.append((label, int(value) if value == int(value) else value))
                break  # first numeric cell = most-recent column
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
        "- Cash and cash equivalents, short-term investments, prepaid "
        "expenses, current derivative/receivable odds and ends -> "
        "current.other_current_assets.\n"
        "- Goodwill, intangible assets, non-current derivatives, deferred "
        "tax assets -> non_current.other_assets.\n"
        "- Right-of-use assets -> lease_assets (non_current unless the "
        "filing shows a current portion).\n"
        "- Long-term/non-current debt (net of the current portion) -> "
        "non_current.other_liabilities (the non_current section has NO debt "
        "bucket).\n"
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
    """Tally-failure hook (Stage 4): re-call the LLM ONCE with the previous
    JSON and the exact gap so it can re-map missing lines."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(markdown)},
        {"role": "assistant", "content": json.dumps(previous_json)},
        {
            "role": "user",
            "content": (
                f"{gap_message} Correct the previous JSON with the SMALLEST "
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

"""Stage 3 helper — parse, repair and validate the LLM's JSON output.

Strips markdown fences, repairs the two invalid-number forms the model emits
(digit-group commas and literal +/- chains), validates the exact fixed schema
(all keys present, none invented), and normalizes optional blocks. The values
are never altered beyond the one allowed cleaning (stripping commas) and the
exact in-code summing of the +/- chains the model is told to emit.
"""

import copy
import json
import re

from .. import config
from ..config import empty_result


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


def parse_and_validate(raw: str) -> dict:
    """Strip fences, parse (repairing the LLM's invalid number forms if the
    first parse fails), validate the fixed schema, and normalize optional
    blocks the LLM may omit or malform. Raises ValueError/JSONDecodeError on
    unrecoverable output — the caller re-prompts once on that."""
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

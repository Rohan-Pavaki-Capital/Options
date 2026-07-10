"""Stage 4 — tally check, done IN CODE (not by the LLM).

Coerces every bucket and memo value to a number (the only cleaning allowed
is stripping "$" and ","), sums buckets + memo per side, compares against
the filing's printed totals within the rounding-aware tolerance, and sets
the balanced booleans. Reconciliation (memo-excluded design):
  sum(asset buckets) + memo cash + goodwill + intangibles == printed total_assets
  sum(liability buckets) + memo long_term_debt == printed total_liabilities
Also builds the exact-gap message used for the LLM re-prompt loop. A number
is NEVER fabricated to force a tie — an unreconciled side stays unbalanced,
with a warning.
"""

import itertools
import logging
import re
from collections import Counter

from .config import (
    ASSET_CURRENT_KEYS,
    ASSET_NON_CURRENT_KEYS,
    LIABILITY_CURRENT_KEYS,
    LIABILITY_NON_CURRENT_KEYS,
    MEMO_KEYS,
    tally_tolerance,
)

logger = logging.getLogger("balance_sheet.tally")

# Memo fields counted on each side of the reconciliation.
ASSET_MEMO_KEYS = [k for k in MEMO_KEYS if k != "long_term_debt"]
LIABILITY_MEMO_KEYS = ["long_term_debt"]


def coerce_number(value, warnings: list, label: str):
    """Coerce a bucket value to int/float. Only "$" and "," are stripped."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "").strip()
        if cleaned in ("", "-", "—"):
            return 0
        try:
            num = float(cleaned)
            return int(num) if num == int(num) else num
        except ValueError:
            pass
        # The LLM is told to report multi-line buckets as "a + b + c" and let
        # code do the exact addition — accept that form in strings too.
        # Normalize an operator immediately followed by a sign ("532 + -34292"),
        # which happens when a negative printed line (e.g. accumulated
        # depreciation) is chained — otherwise the strict guard below fails.
        norm = re.sub(r"\+\s*-", "-", cleaned)
        norm = re.sub(r"-\s*-", "+", norm)
        norm = re.sub(r"\+\s*\+", "+", norm)
        norm = re.sub(r"-\s*\+", "-", norm)
        if re.fullmatch(r"-?\d+(?:\.\d+)?(?:\s*[+-]\s*\d+(?:\.\d+)?)+", norm):
            total = sum(
                float(t.replace(" ", ""))
                for t in re.findall(r"[+-]?\s*\d+(?:\.\d+)?", norm)
            )
            return int(total) if total == int(total) else total
    warnings.append(f"Non-numeric value in {label}: {value!r} - treated as 0.")
    return 0


def coerce_result_numbers(result: dict) -> dict:
    """Coerce all bucket values and filing totals in place (LLM may return strings)."""
    warnings = result.setdefault("warnings", [])
    sections = [
        ("assets", "non_current", ASSET_NON_CURRENT_KEYS),
        ("assets", "current", ASSET_CURRENT_KEYS),
        ("liabilities", "non_current", LIABILITY_NON_CURRENT_KEYS),
        ("liabilities", "current", LIABILITY_CURRENT_KEYS),
    ]
    for group, sub, keys in sections:
        block = result[group][sub]
        for k in keys:
            block[k] = coerce_number(block.get(k), warnings, f"{group}.{sub}.{k}")
    for k in ("preferred_stock", "mezzanine_equity"):
        result["liabilities"][k] = coerce_number(
            result["liabilities"].get(k), warnings, f"liabilities.{k}"
        )
    memo = result.setdefault("memo_excluded", {})
    for k in MEMO_KEYS:
        memo[k] = coerce_number(memo.get(k), warnings, f"memo_excluded.{k}")
    for k in ("total_assets", "total_liabilities"):
        result["filing_totals"][k] = coerce_number(
            result["filing_totals"].get(k), warnings, f"filing_totals.{k}"
        )
    return result


def _sum_assets(result: dict) -> float:
    # Buckets + the asset-side memo fields: the printed Total Assets includes
    # cash/securities, goodwill and intangibles even though they are kept out
    # of the standardized buckets.
    a = result["assets"]
    memo = result.get("memo_excluded", {})
    return (sum(a["non_current"].values()) + sum(a["current"].values())
            + sum(memo.get(k, 0) for k in ASSET_MEMO_KEYS))


def _sum_liabilities(result: dict) -> float:
    # preferred_stock / mezzanine_equity are equity-adjacent and sit OUTSIDE
    # the filing's printed "Total liabilities", so they are excluded here.
    # Buckets + memo long_term_debt (excluded from buckets, in the printed total).
    li = result["liabilities"]
    memo = result.get("memo_excluded", {})
    return (sum(li["non_current"].values()) + sum(li["current"].values())
            + sum(memo.get(k, 0) for k in LIABILITY_MEMO_KEYS))


_OTHER_KEYS = {
    "other_assets", "other_current_assets",
    "other_liabilities", "other_current_liabilities",
}


def _side_buckets(result: dict, side: str):
    """Yield (subsection, key, value) for every bucket counted in the side's sum."""
    for sub in ("non_current", "current"):
        for key, value in result[side][sub].items():
            yield sub, key, value


def _side_memo_items(result: dict, side: str):
    """Yield (subsection, key, value) for the memo fields counted in the side's sum."""
    memo = result.get("memo_excluded", {})
    keys = ASSET_MEMO_KEYS if side == "assets" else LIABILITY_MEMO_KEYS
    for key in keys:
        yield "memo_excluded", key, memo.get(key, 0)


def _side_entries(result: dict, side: str):
    """Every entry (bucket + memo) counted in the side's sum."""
    return itertools.chain(_side_buckets(result, side),
                           _side_memo_items(result, side))


def _side_tolerance(result: dict, side: str) -> float:
    """Rounding-aware tolerance for one side: each mapped line is individually
    rounded in the filing. The non-zero entry count (buckets + memo) stands in
    for the number of mapped lines."""
    mapped = sum(1 for _s, _k, v in _side_entries(result, side) if v)
    return tally_tolerance(mapped, result["filing_totals"][f"total_{side}"])


def _diagnose_side(result: dict, side: str) -> dict:
    """Explain an unbalanced side. gap > 0 (over-count) is the double-count
    signature: look for a SPECIFIC bucket whose value ~= the gap — that line
    was almost certainly also folded into an other_* bucket. gap < 0 means a
    line was simply not mapped."""
    tally_d = result["tally"]
    gap = tally_d[f"sum_{side}"] - result["filing_totals"][f"total_{side}"]
    diagnosis = {
        "side": side,
        "gap": gap,
        "likely_double_counted_bucket": None,
        "bucket_value": None,
        "type": "double_count" if gap > 0 else "missing",
    }
    if gap > 0:
        # ~= match: the duplicated line's printed value can differ slightly
        # from the gap (e.g. Apple: gap 50,016 vs ppe 50,116), so allow 1%.
        tolerance = max(1, round(abs(gap) * 0.01))
        best = None
        for _sub, key, value in _side_entries(result, side):
            if key in _OTHER_KEYS or not value:
                continue
            distance = abs(value - gap)
            if distance <= tolerance and (best is None or distance < best[0]):
                best = (distance, key, value)
        if best:
            diagnosis["likely_double_counted_bucket"] = best[1]
            diagnosis["bucket_value"] = best[2]
    return diagnosis


def run_tally(result: dict) -> dict:
    """Sum buckets + memo per side, compare to the printed filing totals
    within the rounding-aware tolerance, set booleans. Unbalanced sides get
    a structured diagnosis in tally["diagnosis"]."""
    sum_assets = _sum_assets(result)
    sum_liabilities = _sum_liabilities(result)
    totals = result["filing_totals"]

    result["tally"] = {
        "sum_assets": sum_assets,
        "sum_liabilities": sum_liabilities,
        "assets_balanced": abs(sum_assets - totals["total_assets"])
                           <= _side_tolerance(result, "assets"),
        "liabilities_balanced": abs(sum_liabilities - totals["total_liabilities"])
                                <= _side_tolerance(result, "liabilities"),
    }
    diagnoses = []
    if not result["tally"]["assets_balanced"]:
        diagnoses.append(_diagnose_side(result, "assets"))
    if not result["tally"]["liabilities_balanced"]:
        diagnoses.append(_diagnose_side(result, "liabilities"))
    if diagnoses:
        result["tally"]["diagnosis"] = diagnoses

    logger.info(
        "Tally: assets %s vs printed %s (%s); liabilities %s vs printed %s (%s)",
        sum_assets, totals["total_assets"], result["tally"]["assets_balanced"],
        sum_liabilities, totals["total_liabilities"], result["tally"]["liabilities_balanced"],
    )
    return result


def is_balanced(result: dict) -> bool:
    return result["tally"]["assets_balanced"] and result["tally"]["liabilities_balanced"]


def _closest_bucket_to_gap(result: dict, side: str, gap: float):
    """Find the bucket/memo entry on this side whose value is closest to the
    gap, but only within 10% of it — naming a wildly-off bucket sends the
    correction re-prompt chasing the wrong line."""
    best = None
    for _sub, key, value in _side_entries(result, side):
        if not value:
            continue
        distance = abs(value - gap)
        if best is None or distance < best[0]:
            best = (distance, key, value)
    if best and best[0] <= max(1, abs(gap) * 0.10):
        return best[1]
    return None


def build_gap_message(result: dict) -> str:
    """Targeted CORRECTION message for the LLM re-prompt loop (Stage 4),
    built from the diagnosis: names the suspected double-counted bucket for
    over-counts; for under-counts, points at the missing line — very often
    the custodial/collateral asset matching a large mapped liability."""
    tally = result["tally"]
    totals = result["filing_totals"]
    parts = []
    for diag in tally.get("diagnosis", []):
        side = diag["side"]
        label = "Assets" if side == "assets" else "Liabilities"
        printed_label = "Total Assets" if side == "assets" else "Total Liabilities"
        other_hint = (
            "other_assets (or other_current_assets)" if side == "assets"
            else "other_liabilities (or other_current_liabilities)"
        )
        sum_v = tally[f"sum_{side}"]
        printed = totals[f"total_{side}"]
        gap = diag["gap"]
        memo_hint = (
            "cash/marketable securities, goodwill or intangibles → the matching "
            "memo_excluded field" if side == "assets"
            else "long-term debt → memo_excluded.long_term_debt"
        )
        if diag["type"] == "double_count":
            closest = (diag["likely_double_counted_bucket"]
                       or _closest_bucket_to_gap(result, side, gap))
            if closest:
                parts.append(
                    f"{label} over by {gap:,} (buckets + memo sum to {sum_v:,} vs "
                    f"printed {printed_label} {printed:,}). The value {gap:,} ~= "
                    f"'{closest}'. A line is counted in both '{closest}' and an "
                    f"other_* bucket — remove it from other_* so each line is "
                    f"counted once. Return corrected JSON."
                )
            else:
                memo_double_hint = (
                    "a cash or marketable-securities line also sitting in "
                    "other_current_assets (it belongs ONLY in memo_excluded."
                    "cash_and_marketable_securities), or a goodwill/intangibles "
                    "line also in other_assets" if side == "assets"
                    else "a long-term debt line also sitting in other_liabilities "
                    "or current.debt (it belongs ONLY in memo_excluded."
                    "long_term_debt)"
                )
                parts.append(
                    f"{label} over by {gap:,} (buckets + memo sum to {sum_v:,} vs "
                    f"printed {printed_label} {printed:,}). A printed line worth "
                    f"about {gap:,} is counted in TWO places. Check FIRST for "
                    f"{memo_double_hint}; otherwise a subtotal row was mapped or "
                    f"a line sits in two buckets — re-map so each line is counted "
                    f"exactly once. Return corrected JSON."
                )
        else:
            parts.append(
                f"{label} under by {abs(gap):,} (buckets + memo sum to {sum_v:,} "
                f"vs printed {printed_label} {printed:,}). A line summing to about "
                f"{abs(gap):,} was not mapped. If the missing line is "
                f"{memo_hint}; the custodial / performance-bond / collateral "
                f"{'ASSET' if side == 'assets' else 'balance'} matching a large "
                f"amount mapped on the other side, or other unmapped lines, go "
                f"to {other_hint}. Map the missing printed line(s) so {side} tie "
                f"to printed {printed_label}. Return corrected JSON."
            )
    if not parts:  # defensive: called without a diagnosis
        parts.append(
            "The bucket sums do not match the printed totals - re-map so every "
            "line is included in exactly one bucket. Return corrected JSON."
        )
    return " ".join(parts)


def add_unbalanced_warnings(result: dict) -> dict:
    """Name the remaining gap in warnings — never silently accept an imbalance.
    Includes the diagnosis: suspected double-counted bucket or missing amount."""
    tally = result["tally"]
    totals = result["filing_totals"]
    for diag in tally.get("diagnosis", []):
        side = diag["side"]
        gap = diag["gap"]
        text = (
            f"{side.capitalize()} do not tally: buckets + memo sum to "
            f"{tally[f'sum_{side}']:,} vs printed total {side} "
            f"{totals[f'total_{side}']:,} (gap {gap:+,}). "
        )
        if diag["type"] == "double_count" and diag["likely_double_counted_bucket"]:
            text += (
                f"Over by {gap:,} ~= {diag['likely_double_counted_bucket']} "
                f"({diag['bucket_value']:,}); likely double-counted in other_* - "
                f"remove it from the other_* bucket."
            )
        elif diag["type"] == "double_count":
            text += (
                f"Over by {gap:,}; a line was double-counted or a subtotal row "
                f"was mapped - re-map (single-bucket rule)."
            )
        else:
            text += (
                f"Under by {abs(gap):,}; a printed line was not mapped - the "
                f"side is left UNBALANCED (no number is ever fabricated to "
                f"force a tie); re-map the missing line."
            )
        result["warnings"].append(text)
    return result


_CURRENT_TAX_LABELS = {"taxes", "income taxes payable", "income tax payable"}

_ITEM_TAG_RE = re.compile(r"^\[(ASSET|LIABILITY|EQUITY)\]\s*(.*)$")


def _parse_item_label(label: str):
    """Split an extract_line_items label into (side, core_label, cnc).
    The tags exist only in the hint text — this reads them back for guards."""
    side, core = "", label
    m = _ITEM_TAG_RE.match(label)
    if m:
        side, core = m.group(1), m.group(2)
    cnc = ""
    for suffix, tag in ((" (current)", "current"), (" (non-current)", "non_current")):
        if core.endswith(suffix):
            core, cnc = core[: -len(suffix)], tag
            break
    return side, core.strip(), cnc


def regroup_current_tax_liability(result: dict, line_items: list) -> dict:
    """Deterministic split correction (House Convention 3): a CURRENT
    liability line labeled "Taxes" / "Income taxes payable" belongs in
    current.deferred_rev_and_tax, grouped with current deferred income — the
    LLM keeps dropping it into other_current_liabilities instead. Move the
    printed value ONLY when the bucket sums prove where it sits: the deferred
    bucket currently equals the current tax/deferred lines WITHOUT the Taxes
    line(s), and other_current_liabilities holds at least their value.
    Both buckets are in liabilities.current, so the liabilities total and
    the tally booleans are unchanged — only the split moves."""
    cur = result["liabilities"]["current"]
    taxes = []     # printed values of current-liability "Taxes"-type lines
    tax_like = []  # values of every current-liability tax/deferred line
    for label, value in line_items:
        side, core, cnc = _parse_item_label(label)
        if side != "LIABILITY" or cnc != "current" or not value:
            continue
        lowered = core.lower().rstrip(":").strip()
        if lowered in _CURRENT_TAX_LABELS:
            taxes.append(value)
        if "tax" in lowered or "deferred" in lowered:
            tax_like.append(value)
    if not taxes:
        return result
    moved = sum(taxes)
    without_taxes = sum(tax_like) - moved
    if abs(cur["deferred_rev_and_tax"] - without_taxes) > 0.01:
        return result  # already grouped there, or ambiguous — do not touch
    if cur["other_current_liabilities"] < moved:
        return result  # not sitting in other_* — nothing to move
    cur["other_current_liabilities"] -= moved
    cur["deferred_rev_and_tax"] += moved
    result["warnings"].append(
        f"Moved current tax liability {moved:,} from other_current_liabilities "
        f"to current.deferred_rev_and_tax (grouped with current deferred "
        f"income, House Convention 3). Same-side move - the liabilities total "
        f"is unchanged."
    )
    return result


def _side_raw_fields(raw: dict, side: str):
    """Yield (field_name, raw_value) for every field counted in the side's
    sum — the only places a duplicated chain term can distort the tally."""
    for sub in ("non_current", "current"):
        for key, value in raw[side][sub].items():
            yield f"{side}.{sub}.{key}", value
    memo = raw.get("memo_excluded", {})
    keys = ASSET_MEMO_KEYS if side == "assets" else LIABILITY_MEMO_KEYS
    for key in keys:
        yield f"memo_excluded.{key}", memo.get(key, 0)


_CHAIN_TERM_RE = re.compile(r"[+-]?\s*\d+(?:\.\d+)?")


def _field_terms(value) -> list:
    """Terms a RAW (pre-coercion) field value is built from: a ' + ' chain
    string names each printed line value it uses; a plain number is one term."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "")
        return [float(t.replace(" ", "")) for t in _CHAIN_TERM_RE.findall(cleaned)]
    if isinstance(value, (int, float)) and value:
        return [float(value)]
    return []


def find_chain_duplicates(raw_result: dict, line_items: list) -> list:
    """Single-count violations visible in the RAW LLM output: a printed
    line's value appearing as a term in MORE chains (bucket/memo fields on
    its side) than the filing prints it. Comparing against printed-value
    multiplicities keeps two different lines that legitimately share a value
    from being flagged, and custodial balances mapped once per side stay
    legal. Detection only — code never picks which chain is the correct
    home; that is the model's judgement in the correction loop."""
    duplicates = []
    for side, side_tag in (("assets", "ASSET"), ("liabilities", "LIABILITY")):
        printed = Counter()
        label_of = {}
        for label, value in line_items:
            tag, _core, _cnc = _parse_item_label(label)
            if not value or tag == "EQUITY":
                continue
            if tag in (side_tag, ""):  # untagged lines count on both sides
                v = float(value)
                printed[v] += 1
                label_of.setdefault(v, label)
        used = {}
        for name, value in _side_raw_fields(raw_result, side):
            for term in _field_terms(value):
                used.setdefault(term, []).append(name)
        for v, fields in used.items():
            if v in printed and len(fields) > printed[v]:
                duplicates.append({
                    "side": side,
                    "value": int(v) if v == int(v) else v,
                    "label": label_of[v],
                    "fields": fields,
                })
    return duplicates


def build_duplicate_message(duplicates: list) -> str:
    """CORRECTION text for chain-level duplicates: names the duplicated line
    and every chain using it, and instructs the model to keep it in exactly
    one — the model, not code, decides which chain is the correct home."""
    parts = []
    for dup in duplicates:
        counts = Counter(dup["fields"])
        listing = " AND ".join(
            name + (f" ({n} times)" if n > 1 else "")
            for name, n in counts.items()
        )
        parts.append(
            f"SINGLE-COUNT VIOLATION ({dup['side']}): the printed line "
            f"'{dup['label']}' = {dup['value']:,} appears as a term in more "
            f"than one place: {listing}. Keep {dup['value']:,} in exactly "
            f"ONE of them (whichever the mapping rules say) and REMOVE it "
            f"from the other chain(s), leaving every other term unchanged. "
            f"Return corrected JSON."
        )
    return " ".join(parts)


_EQUITY_ADJACENT_TERMS = {
    "preferred_stock": ("preferred",),
    "mezzanine_equity": ("mezzanine", "redeemable", "temporary equity", "preferred"),
}


def guard_equity_adjacent_buckets(result: dict, line_items: list) -> dict:
    """Code guard: preferred_stock / mezzanine_equity may only hold the value
    of a printed line actually labeled as such (preferred / redeemable /
    mezzanine / temporary equity). The LLM keeps sneaking ordinary equity
    lines (e.g. NIKE "Class B common stock at stated value" = 3) into
    mezzanine_equity despite the prompt rules — zero it and warn. These
    buckets sit outside the liabilities tally, so this never changes sums."""
    for bucket, terms in _EQUITY_ADJACENT_TERMS.items():
        value = result["liabilities"].get(bucket)
        if not value:
            continue
        matching = [v for label, v in line_items
                    if any(t in label.lower() for t in terms)]
        if value in matching or (matching and sum(matching) == value):
            continue
        result["warnings"].append(
            f"Removed {value:,} from {bucket}: no printed line labeled "
            f"preferred/redeemable/mezzanine/temporary equity carries that "
            f"value - it was an ordinary equity line (kept out of all buckets)."
        )
        result["liabilities"][bucket] = 0
    return result


def sanity_check_other_buckets(result: dict) -> dict:
    """Belt-and-suspenders guard: an other_* bucket holding most of a side
    while every specific bucket is empty is the signature of the LLM dumping
    everything (or a subtotal) into other_*. Warning only — never an error."""
    for side, total_key in (("assets", "total_assets"),
                            ("liabilities", "total_liabilities")):
        total = result["filing_totals"].get(total_key) or 0
        if total <= 0:
            continue
        if any(v for _s, k, v in _side_buckets(result, side) if k not in _OTHER_KEYS):
            continue  # specific buckets are filled — mapping looks real
        for _sub, key, value in _side_buckets(result, side):
            if key in _OTHER_KEYS and value > 0.6 * total:
                result["warnings"].append(
                    f"Sanity check: {key} = {value:,} is {value / total:.0%} of "
                    f"printed {total_key} and no specific {side} buckets are "
                    f"filled - possible subtotal mapping or everything dumped "
                    f"into other_*."
                )
    return result


_UNIT_FACTORS_TO_MILLIONS = {"thousands": 0.001, "millions": 1.0, "billions": 1000.0}


def normalize_to_millions(result: dict) -> dict:
    """Convert every numeric value to millions, whatever the filing's native
    scale. Run LAST — after the tally has passed in native units — so scaling
    (a linear op) never affects reconciliation. thousands ÷ 1,000, millions
    unchanged, billions × 1,000. Exact for thousands (whole-thousand lines →
    at most 3 decimals). Original scale is recorded in original_unit_label; if
    the scale was never identified, values are left as-is with a warning
    (guessing the unit would be worse than being honest)."""
    label = (result.get("unit_label") or "").lower()
    factor = unit_word = None
    for word, f in _UNIT_FACTORS_TO_MILLIONS.items():
        if word in label:
            factor, unit_word = f, word
            break
    if factor is None:
        result.setdefault("warnings", []).append(
            "Unit scale not identified - values left in the filing's original "
            "unit; NOT converted to millions."
        )
        return result
    if factor == 1.0:
        result["unit_label"] = "in millions"
        return result  # already millions — nothing to scale

    def scale(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return v
        out = round(v * factor, 3)
        return int(out) if out == int(out) else out

    for sub in ("non_current", "current"):
        for k in result["assets"][sub]:
            result["assets"][sub][k] = scale(result["assets"][sub][k])
        for k in result["liabilities"][sub]:
            result["liabilities"][sub][k] = scale(result["liabilities"][sub][k])
    for k in ("preferred_stock", "mezzanine_equity"):
        result["liabilities"][k] = scale(result["liabilities"][k])
    for k in result["memo_excluded"]:
        result["memo_excluded"][k] = scale(result["memo_excluded"][k])
    for k in ("total_assets", "total_liabilities"):
        result["filing_totals"][k] = scale(result["filing_totals"][k])
    for k in ("sum_assets", "sum_liabilities"):
        result["tally"][k] = scale(result["tally"][k])

    result["original_unit_label"] = f"in {unit_word}"
    result["unit_label"] = "in millions"
    result.setdefault("warnings", []).append(
        f"All values converted from '{unit_word}' to millions (x{factor}); "
        f"original scale recorded in original_unit_label."
    )
    return result

"""Stage 4 — tally check, done IN CODE (not by the LLM).

Coerces every bucket value to a number (the only cleaning allowed is
stripping "$" and ","), sums the buckets, compares against the filing's
printed totals within TALLY_TOLERANCE, and sets the balanced booleans.
Also builds the exact-gap message used for the one-shot LLM re-prompt.
"""

import logging
import math
import re

from .config import (
    ASSET_CURRENT_KEYS,
    ASSET_NON_CURRENT_KEYS,
    LIABILITY_CURRENT_KEYS,
    LIABILITY_NON_CURRENT_KEYS,
    MEMO_ASSET_KEYS,
    MEMO_KEYS,
    MEMO_LIABILITY_KEYS,
    PLUG_SUSPICIOUS_GAP,
    TALLY_TOLERANCE,
)

logger = logging.getLogger("balance_sheet.tally")


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
        if re.fullmatch(r"-?\d+(?:\.\d+)?(?:\s*[+-]\s*\d+(?:\.\d+)?)+", cleaned):
            total = sum(
                float(t.replace(" ", ""))
                for t in re.findall(r"[+-]?\s*\d+(?:\.\d+)?", cleaned)
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
    memo = result.setdefault("memo", {})
    for k in MEMO_KEYS:
        memo[k] = coerce_number(memo.get(k), warnings, f"memo.{k}")
    for k in ("total_assets", "total_liabilities"):
        result["filing_totals"][k] = coerce_number(
            result["filing_totals"].get(k), warnings, f"filing_totals.{k}"
        )
    return result


def _sum_assets(result: dict) -> float:
    # Memo cash/goodwill are asset lines kept out of the buckets for the
    # workbook — they still count toward the printed Total Assets.
    a = result["assets"]
    memo = result.get("memo", {})
    return (sum(a["non_current"].values()) + sum(a["current"].values())
            + sum(memo.get(k, 0) for k in MEMO_ASSET_KEYS))


def _sum_liabilities(result: dict) -> float:
    # preferred_stock / mezzanine_equity are equity-adjacent and sit OUTSIDE
    # the filing's printed "Total liabilities", so they are excluded here.
    # memo.long_term_debt IS part of printed Total Liabilities.
    li = result["liabilities"]
    memo = result.get("memo", {})
    return (sum(li["non_current"].values()) + sum(li["current"].values())
            + sum(memo.get(k, 0) for k in MEMO_LIABILITY_KEYS))


_OTHER_KEYS = {
    "other_assets", "other_current_assets",
    "other_liabilities", "other_current_liabilities",
}


def _side_buckets(result: dict, side: str):
    """Yield (subsection, key, value) for every bucket counted in the side's
    sum — including the side's memo fields (they behave like specific buckets
    for diagnosis; they are never other_* plug targets)."""
    for sub in ("non_current", "current"):
        for key, value in result[side][sub].items():
            yield sub, key, value
    memo_keys = MEMO_ASSET_KEYS if side == "assets" else MEMO_LIABILITY_KEYS
    memo = result.get("memo", {})
    for key in memo_keys:
        yield "memo", key, memo.get(key, 0)


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
        tolerance = max(TALLY_TOLERANCE, round(abs(gap) * 0.01))
        best = None
        for _sub, key, value in _side_buckets(result, side):
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
    """Sum the buckets, compare to the printed filing totals, set booleans.
    Unbalanced sides get a structured diagnosis in tally["diagnosis"]."""
    sum_assets = _sum_assets(result)
    sum_liabilities = _sum_liabilities(result)
    totals = result["filing_totals"]

    result["tally"] = {
        "sum_assets": sum_assets,
        "sum_liabilities": sum_liabilities,
        "assets_balanced": abs(sum_assets - totals["total_assets"]) <= TALLY_TOLERANCE,
        "liabilities_balanced": abs(sum_liabilities - totals["total_liabilities"]) <= TALLY_TOLERANCE,
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
    """Find the bucket on this side whose value is closest to the gap
    (any bucket, no distance cutoff — used for the over-count message)."""
    best = None
    for _sub, key, value in _side_buckets(result, side):
        if not value:
            continue
        distance = abs(value - gap)
        if best is None or distance < best[0]:
            best = (distance, key, value)
    return best[1] if best else None


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
            "other_current_assets if the filing lists the line under Current "
            "Assets — e.g. performance bonds — else other_assets"
            if side == "assets"
            else "other_current_liabilities if listed under Current "
            "Liabilities, else other_liabilities"
        )
        sum_v = tally[f"sum_{side}"]
        printed = totals[f"total_{side}"]
        gap = diag["gap"]
        if diag["type"] == "double_count":
            closest = (diag["likely_double_counted_bucket"]
                       or _closest_bucket_to_gap(result, side, gap))
            if closest:
                parts.append(
                    f"{label} over by {gap:,} (buckets sum to {sum_v:,} vs printed "
                    f"{printed_label} {printed:,}). The value {gap:,} ~= bucket "
                    f"'{closest}'. A line is counted in both '{closest}' and an "
                    f"other_* bucket — remove it from other_* so each line is "
                    f"counted once. Return corrected JSON."
                )
            else:
                parts.append(
                    f"{label} over by {gap:,} (buckets sum to {sum_v:,} vs printed "
                    f"{printed_label} {printed:,}). A line is counted in two "
                    f"buckets or a subtotal row was mapped — re-map so each line "
                    f"is counted exactly once. Return corrected JSON."
                )
        else:
            memo_hint = (
                "cash/short-term investments (-> memo.cash_and_st_investments) "
                "or goodwill/intangibles (-> memo.goodwill_and_intangibles)"
                if side == "assets"
                else "long-term debt (-> memo.long_term_debt)"
            )
            parts.append(
                f"{label} under by {abs(gap):,} (buckets + memo sum to {sum_v:,} "
                f"vs printed {printed_label} {printed:,}). A printed line summing "
                f"to about {abs(gap):,} was not mapped. Check first for unmapped "
                f"{memo_hint}; otherwise it is very often the custodial / "
                f"performance-bond / collateral {'ASSET' if side == 'assets' else 'balance'} "
                f"that matches a large amount you already mapped on the other "
                f"side (-> {other_hint}). Map the missing printed line(s) to the "
                f"correct bucket or memo field so {side} tie to printed "
                f"{printed_label}. Return corrected JSON."
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
            f"{side.capitalize()} do not tally: buckets sum to "
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
                f"Under by {abs(gap):,}; a line was not mapped - add the missing "
                f"amount to the appropriate other_* bucket."
            )
        result["warnings"].append(text)
    return result


def apply_deterministic_plug(result: dict) -> dict:
    """Last-resort reconcile AFTER the LLM retries: force each side's buckets
    to sum to the printed total by adjusting ONLY an other_* bucket (never a
    specific bucket), with a warning making the plug visible for review.
    Guarantees the bucket sums always tie to the printed totals."""
    for side, printed_label in (("assets", "Total Assets"),
                                ("liabilities", "Total Liabilities")):
        gap = result["tally"][f"sum_{side}"] - result["filing_totals"][f"total_{side}"]
        if abs(gap) <= TALLY_TOLERANCE:
            continue
        if abs(gap) <= PLUG_SUSPICIOUS_GAP:
            # A correct mapping ties exactly — a small residual is the
            # signature of a wrong-bucket mis-map, not rounding.
            result["warnings"].append(
                f"LIKELY WRONG-BUCKET MAPPING ({side}): the remaining gap is "
                f"only {abs(gap):,} — a correct mapping should tie exactly, "
                f"so a small residual means a line was placed in the wrong "
                f"bucket (not rounding). The plug is applied below, but the "
                f"bucket breakdown needs review."
            )
        other_key = "other_assets" if side == "assets" else "other_liabilities"
        if gap < 0:
            # Buckets fall short — add the shortfall to the non-current other_* bucket.
            shortfall = -gap
            shortfall = int(shortfall) if shortfall == int(shortfall) else shortfall
            result[side]["non_current"][other_key] += shortfall
            result["warnings"].append(
                f"Auto-plugged {shortfall:,} into {other_key} to reconcile to "
                f"printed {printed_label}."
            )
        else:
            # Buckets exceed printed — subtract the overage from the largest
            # other_* bucket on this side.
            overage = int(gap) if gap == int(gap) else gap
            largest = None
            for sub, key, value in _side_buckets(result, side):
                if key in _OTHER_KEYS and (largest is None or value > largest[2]):
                    largest = (sub, key, value)
            sub, key, _value = largest
            result[side][sub][key] -= overage
            result["warnings"].append(
                f"Auto-removed {overage:,} from {key} to reconcile to printed "
                f"{printed_label} (buckets exceeded the printed total)."
            )
    return run_tally(result)


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


# Placement guards: (label terms, "memo"|"bucket", target key). A printed
# line whose label matches a term belongs in the target field, never folded
# into other_assets.
_PLACEMENT_GUARDS = (
    (("goodwill", "intangible"), "memo", "goodwill_and_intangibles"),
    (("property, plant", "property and equipment"), "bucket", "ppe"),
)


def enforce_asset_placements(result: dict, line_items: list) -> dict:
    """Code guard (placement quality): printed goodwill/intangible lines
    belong in memo.goodwill_and_intangibles and printed PP&E lines in ppe,
    but a first pass that already balances never triggers the correction
    loop, so the LLM can leave them folded into other_assets (CME/AAPL
    regressions). When a target field falls short of its printed lines and
    other_assets holds at least the difference, move the shortfall across —
    all these fields count toward the assets tally, so the sums never
    change."""
    for terms, kind, key in _PLACEMENT_GUARDS:
        expected = sum(
            v for label, v in line_items
            if isinstance(v, (int, float)) and v > 0
            and any(t in label.lower() for t in terms)
        )
        if not expected:
            continue
        holder = (result.setdefault("memo", {}) if kind == "memo"
                  else result["assets"]["non_current"])
        target = f"{kind}.{key}" if kind == "memo" else key
        current = holder.get(key, 0) or 0
        shortfall = expected - current
        if shortfall <= TALLY_TOLERANCE:
            continue
        other = result["assets"]["non_current"].get("other_assets", 0) or 0
        if other + TALLY_TOLERANCE < shortfall:
            result["warnings"].append(
                f"{target} ({current:,}) is short of the printed matching "
                f"lines ({expected:,}) but other_assets ({other:,}) does not "
                f"hold the difference - placement left for review (no values "
                f"moved)."
            )
            continue
        shortfall = (int(shortfall) if shortfall == int(shortfall)
                     else round(shortfall, 2))
        result["assets"]["non_current"]["other_assets"] = other - shortfall
        holder[key] = current + shortfall
        result["warnings"].append(
            f"Moved {shortfall:,} of printed lines out of other_assets into "
            f"{target} (placement guard; totals unchanged)."
        )
    return result


def _to_millions(value):
    """Divide a thousands-scale value by 1,000 and round half-away-from-zero
    to whole millions (user decision 2026-07-07)."""
    scaled = value / 1000.0
    return int(math.copysign(math.floor(abs(scaled) + 0.5), scaled))


def convert_to_millions(result: dict) -> dict:
    """Final normalization — the standard output scale is MILLIONS (user
    decision 2026-07-07). Runs AFTER the tally verification, which always
    ties the numbers at the filing's printed scale:
      - "in thousands"  -> every value divided by 1,000 and rounded to whole
        millions (so rounded buckets may differ from rounded totals by a few
        units — the balanced flags keep the printed-scale verdict);
      - "in millions"   -> values left exactly as printed (never re-rounded);
      - anything else / missing -> values left at printed scale with a
        warning (never guess a divisor).
    unit_label is normalized to "in millions" whenever the scale is known."""
    label = (result.get("unit_label") or "").lower()
    if "million" in label:
        result["unit_label"] = "in millions"
        return result
    if "thousand" not in label:
        result.setdefault("warnings", []).append(
            f"unit_label {result.get('unit_label')!r} is not recognized as "
            f"thousands or millions - values left at the filing's printed scale."
        )
        return result

    sections = [
        ("assets", "non_current", ASSET_NON_CURRENT_KEYS),
        ("assets", "current", ASSET_CURRENT_KEYS),
        ("liabilities", "non_current", LIABILITY_NON_CURRENT_KEYS),
        ("liabilities", "current", LIABILITY_CURRENT_KEYS),
    ]
    for group, sub, keys in sections:
        block = result[group][sub]
        for k in keys:
            block[k] = _to_millions(block.get(k, 0) or 0)
    for k in ("preferred_stock", "mezzanine_equity"):
        result["liabilities"][k] = _to_millions(result["liabilities"].get(k, 0) or 0)
    memo = result.setdefault("memo", {})
    for k in MEMO_KEYS:
        memo[k] = _to_millions(memo.get(k, 0) or 0)
    for k in ("total_assets", "total_liabilities"):
        result["filing_totals"][k] = _to_millions(result["filing_totals"].get(k, 0) or 0)
    # Scale the sums for display consistency; the balanced booleans keep the
    # verdict of the printed-scale verification and are NOT recomputed here.
    for k in ("sum_assets", "sum_liabilities"):
        result["tally"][k] = _to_millions(result["tally"].get(k, 0) or 0)

    result["unit_label"] = "in millions"
    result.setdefault("warnings", []).append(
        "Converted from thousands to millions (rounded to whole millions; "
        "the tally was verified at the filing's printed scale before "
        "conversion)."
    )
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

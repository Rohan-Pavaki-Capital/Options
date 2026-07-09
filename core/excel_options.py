"""
excel_options.py — reduce a full SBC extraction result to the minimal,
Excel-ready option-plan inputs the Damodaran valuation workbook needs.

The HTTP layer (backend.py: GET /api/excel/options) reuses the EXISTING
extraction pipeline and then calls `map_plans_to_excel()` here. All of the
selection / field-mapping / units / sort / top-3 logic lives in this one pure
function so it is unit-testable independently of FastAPI.

Per emitted plan we surface ONLY four fields:
    count_mn, strike, maturity_years, kind
Nothing else from the full plans[] structure leaks out.

Run `python excel_options.py` to execute the inline AAPL / NKE assertions.
"""

from __future__ import annotations


# --------------------------------------------------------------------------- #
# Coercion helpers — every numeric field must end up a number or None, never a
# string and never "NA". Coerce numeric strings; on failure treat as None.
# --------------------------------------------------------------------------- #
def _to_float(value):
    """Best-effort coerce to float. Returns None if it can't be a real number.

    bool is deliberately rejected (True/False are not option counts/prices even
    though bool subclasses int in Python)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except (ValueError, OverflowError):
            return None
        # Reject NaN / inf — they are not usable workbook inputs.
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        if s == "" or s.upper() in {"NA", "N/A", "NONE", "NULL", "-", "—"}:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _to_millions(value, units_label):
    """Convert a raw count to MILLIONS using the plan's units_label.

        "millions"                      -> value as-is
        "thousands"                     -> value / 1_000
        "units"/"shares"/raw/None/other -> value / 1_000_000
    """
    v = _to_float(value)
    if v is None:
        return None
    label = (units_label or "").strip().lower()
    if label in {"millions", "million", "mn", "m", "mln"}:
        return v
    if label in {"thousands", "thousand", "k", "000s", "'000"}:
        return v / 1_000.0
    # "units", "shares", raw numbers, None, or anything unrecognised -> shares.
    return v / 1_000_000.0


# --------------------------------------------------------------------------- #
# Per-plan classification & field mapping
# --------------------------------------------------------------------------- #
def _classify_plan(plan):
    """Map ONE plan dict from the extraction's plans[] to a workbook plan, or
    return None to SKIP it. Every field access is guarded — a plan may have any
    subset of fields null."""
    if not isinstance(plan, dict):
        return None

    closing_mn = _to_millions(plan.get("closing_balance"), plan.get("units_label"))
    plan_type = (plan.get("plan_type") or "").strip().upper()

    # 3. SKIP: no ending balance (e.g. ESPP roll-forwards with no closing_balance)
    #    or an ESPP with no usable balance. The workbook column stays blank.
    if closing_mn is None:
        return None
    if plan_type == "ESPP" and closing_mn <= 0:
        return None

    is_nil = plan.get("is_nil_cost")
    waep = _to_float(plan.get("weighted_avg_exercise_price"))

    # 1. OPTION plan: has a real exercise price AND is not nil-cost.
    if waep is not None and is_nil is False:
        # NOTE: maturity priority is WARCL-first per the agreed default.
        #       Fallback chain: remaining_contractual_life -> vesting -> 4.0.
        #       To prefer vesting instead, swap the first two lookups below.
        maturity = _to_float(plan.get("weighted_avg_remaining_contractual_life_years"))
        if maturity is None:
            maturity = _to_float(plan.get("vesting_period_years"))
        if maturity is None:
            maturity = 4.0
        return {
            "count_mn": closing_mn,
            "strike": waep,
            "maturity_years": maturity,
            "kind": "option",
        }

    # 2. RSU / nil-cost plan (RSU, PSU): dilutes but has no real strike.
    if is_nil is True:
        # Proxy strike = grant-date fair value. If null/0/missing, use the 0.1
        # floor (never 0, never null — the workbook's Black-Scholes divides by it).
        strike = _to_float(plan.get("weighted_avg_grant_date_fair_value"))
        if strike is None or strike == 0:
            strike = 0.1
        # WARCL-first, same fallback chain as the option branch.
        maturity = _to_float(plan.get("weighted_avg_remaining_contractual_life_years"))
        if maturity is None:
            maturity = _to_float(plan.get("vesting_period_years"))
        if maturity is None:
            maturity = 4.0
        return {
            "count_mn": closing_mn,
            "strike": strike,
            "maturity_years": maturity,
            "kind": "rsu",
        }

    # Anything else (e.g. nil-cost flag null AND no exercise price) -> skip.
    return None


def _sanitize(plan):
    """Final guard before a plan is emitted. Returns a clean dict or None (skip).

        count_mn       must be a positive number          else skip
        strike         must be a number > 0               else skip
        maturity_years must be a number > 0               else fall back to 4.0
    """
    def _num(x):
        return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None

    count = _num(plan.get("count_mn"))
    if count is None or count <= 0:
        return None

    strike = _num(plan.get("strike"))
    if strike is None or strike <= 0:
        return None

    maturity = _num(plan.get("maturity_years"))
    if maturity is None or maturity <= 0:
        maturity = 4.0

    return {
        "count_mn": count,
        "strike": strike,
        "maturity_years": maturity,
        "kind": plan.get("kind"),
    }


def map_plans_to_excel(extraction_result) -> list[dict]:
    """Reduce a full extraction result to at most 3 workbook-ready option plans,
    ranked by count_mn DESCENDING. Returns [] (never raises) when there is
    nothing to emit."""
    if not isinstance(extraction_result, dict):
        return []
    plans = extraction_result.get("plans")
    if not isinstance(plans, list):
        return []

    mapped = []
    for plan in plans:
        classified = _classify_plan(plan)
        if classified is None:
            continue
        clean = _sanitize(classified)
        if clean is not None:
            mapped.append(clean)

    # Sort by count_mn descending, keep the top 3 (workbook columns D, I, N).
    mapped.sort(key=lambda p: p["count_mn"], reverse=True)
    return mapped[:3]


# --------------------------------------------------------------------------- #
# Inline tests — run with: python excel_options.py
# --------------------------------------------------------------------------- #
def _approx(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) < tol


def _run_tests():
    # ---- AAPL: RSU-only, is_nil_cost true, exercise price null ---------------
    aapl = {
        "currency": "USD",
        "plans": [
            {
                "plan_name": "Restricted Stock Units",
                "plan_type": "RSU",
                "is_nil_cost": True,
                "units_label": None,                 # raw shares -> / 1e6
                "closing_balance": 151_574_000,
                "weighted_avg_exercise_price": None,
                "weighted_avg_grant_date_fair_value": 189.75,
                "weighted_avg_remaining_contractual_life_years": None,
                "vesting_period_years": None,        # -> 4.0
            }
        ],
    }
    out = map_plans_to_excel(aapl)
    assert len(out) == 1, f"AAPL expected 1 plan, got {len(out)}: {out}"
    p = out[0]
    assert p["kind"] == "rsu", p
    assert _approx(p["count_mn"], 151.574), p
    assert _approx(p["strike"], 189.75), p
    assert _approx(p["maturity_years"], 4.0), p
    print("AAPL ->", out)

    # ---- NKE: option plan + RSU plan + ESPP (ESPP skipped) -------------------
    nke = {
        "currency": "USD",
        "plans": [
            {   # ESPP — no closing balance -> SKIP
                "plan_name": "Employee Stock Purchase Plan",
                "plan_type": "ESPP",
                "is_nil_cost": False,
                "units_label": None,
                "closing_balance": None,
                "weighted_avg_exercise_price": None,
            },
            {   # RSU plan
                "plan_name": "Restricted Stock Units",
                "plan_type": "RSU",
                "is_nil_cost": True,
                "units_label": None,                 # raw shares -> / 1e6
                "closing_balance": 10_700_000,
                "weighted_avg_exercise_price": None,
                "weighted_avg_grant_date_fair_value": 94.29,
                "vesting_period_years": None,        # -> 4.0
            },
            {   # Option plan
                "plan_name": "Stock Option Plan",
                "plan_type": "ESOP",
                "is_nil_cost": False,
                "units_label": None,                 # raw shares -> / 1e6
                "closing_balance": 75_100_000,
                "weighted_avg_exercise_price": 97.99,
                "weighted_avg_remaining_contractual_life_years": 5.4,  # WARCL-first
                "vesting_period_years": 4.0,
            },
        ],
    }
    out = map_plans_to_excel(nke)
    assert len(out) == 2, f"NKE expected 2 plans (ESPP skipped), got {len(out)}: {out}"
    # Sorted by count_mn DESC: option (75.1) before rsu (10.7).
    opt, rsu = out[0], out[1]
    assert opt["kind"] == "option", opt
    assert _approx(opt["count_mn"], 75.1), opt
    assert _approx(opt["strike"], 97.99), opt
    assert _approx(opt["maturity_years"], 5.4), opt   # WARCL (flip chain for 4.0)
    assert rsu["kind"] == "rsu", rsu
    assert _approx(rsu["count_mn"], 10.7), rsu
    assert _approx(rsu["strike"], 94.29), rsu
    assert _approx(rsu["maturity_years"], 4.0), rsu
    print("NKE  ->", out)

    print("\nAll assertions passed.")


if __name__ == "__main__":
    _run_tests()

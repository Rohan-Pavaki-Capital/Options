"""IONS regression test — the distorted-anchor failure case.

Real failure: IONS returned FAST_GROWER with target_margin = 0.0108 (the
distorted biotech anchor, damodaran_us_margin < 0.05) while its own comps
showed 15-30% and mature_state_anchor was 0.295.

Offline tests exercise the deterministic validator (no API key needed).
The live test calls Together AI and is skipped unless TOGETHER_API_KEY is set.

Run:  python test_ions.py        (or: pytest test_ions.py)
"""

import asyncio
import json
import os

import config  # noqa: F401  (loads .env so the live-test key check works)
from schemas import ForwardEstimate, HistoricalPeriod, MarginRequest
from validator import (
    JudgmentValidationError,
    _parse_comp_revenue_millions,
    validate_judgment,
)

IONS_TTM_REVENUE = 1058.2

IONS_REQUEST = MarginRequest(
    ticker="IONS",
    company_name="Ionis Pharmaceuticals",
    country="United States",
    industry_us="Drugs (Biotechnology)",
    industry_global="Drugs (Biotechnology)",
    currency="USD",
    units="millions",
    historicals=[
        HistoricalPeriod(period="TTM", revenue=IONS_TTM_REVENUE, op_income=-349.2, op_margin=-0.330),
        HistoricalPeriod(period="FY2024", revenue=705.0, op_income=-472.4, op_margin=-0.670),
        HistoricalPeriod(period="FY2023", revenue=774.2, op_income=-401.3, op_margin=-0.518),
        HistoricalPeriod(period="FY2022", revenue=587.6, op_income=-312.1, op_margin=-0.531),
        HistoricalPeriod(period="FY2021", revenue=573.3, op_income=-215.4, op_margin=-0.376),
    ],
    forward_estimates=[
        ForwardEstimate(period="FY2025", revenue_est=1250.0, eps_est=-1.35),
        ForwardEstimate(period="FY2026", revenue_est=1520.0, eps_est=-0.60),
        ForwardEstimate(period="FY2027", revenue_est=1900.0, eps_est=-0.05),
        ForwardEstimate(period="FY2028", revenue_est=2350.0, eps_est=0.55),
    ],
    rd_adjusted_margins=[-0.08, -0.21, -0.30],
    revenue_cagr_5y=0.17,
    revenue_cagr_10y=None,
    nol_carryforward=1900.0,
    past_avg_margin=-0.49,
    damodaran_us_margin=0.0108,
    damodaran_global_margin=-0.0043,
    mature_state_anchor=0.295,
    mature_state_anchor_source="Damodaran Pharma (Major) US pre-tax operating margin",
)

GOOD_JUDGMENT = {
    "classification": "FAST_GROWER",
    "target_margin": 0.20,
    "confidence": "medium",
    "margin_driver": True,
    "anchor_bypassed": True,
    "damodaran_anchor_used": 0.295,
    "comps_used": [
        "Alnylam ~$2.4B revenue ~15% margin",
        "Neurocrine ~$2.3B revenue ~25% margin",
    ],
    "rationale": "EPS turns positive in FY2028 (0.55); TTM margin -33% on revenue 1,058.2.",
}


def _validate(judgment: dict) -> dict:
    return validate_judgment(json.dumps(judgment), IONS_REQUEST)


def test_distorted_anchor_target_rejected() -> None:
    """The exact production failure: target = the 0.0108 distorted anchor."""
    bad = dict(GOOD_JUDGMENT)
    bad["target_margin"] = 0.0108
    try:
        _validate(bad)
        raise AssertionError("distorted-anchor target 0.0108 was accepted")
    except JudgmentValidationError as exc:
        assert "distorted" in str(exc)


def test_good_judgment_accepted_with_floor_and_effective_anchor() -> None:
    """A correct answer (0.20) passes: above the 0.0885 floor, inside the comp
    band, and NOT killed by the 1.5x cap (which now uses the 0.295 anchor,
    not the distorted 0.0108)."""
    result = _validate(GOOD_JUDGMENT)
    assert result["classification"] == "FAST_GROWER"
    assert 0.12 <= result["target_margin"] <= 0.30
    assert result["convergence_year"] == 3
    assert result["anchor_bypassed"] is True


def test_oversized_comp_rejected() -> None:
    """IONS at $1.06B cannot use Gilead at $24.7B (23x)."""
    bad = dict(GOOD_JUDGMENT)
    bad["comps_used"] = GOOD_JUDGMENT["comps_used"] + ["Gilead ~$24.7B revenue ~30% margin"]
    try:
        _validate(bad)
        raise AssertionError("Gilead-sized comp was accepted")
    except JudgmentValidationError as exc:
        assert "0.3x-3.0x" in str(exc)


def test_target_outside_comp_band_rejected() -> None:
    """Comps at 15-25% with a 1% target is internally inconsistent."""
    bad = dict(GOOD_JUDGMENT)
    bad["target_margin"] = 0.01
    try:
        _validate(bad)
        raise AssertionError("target far below own comps was accepted")
    except JudgmentValidationError as exc:
        assert "comps" in str(exc)


def test_comp_revenue_parsing() -> None:
    assert _parse_comp_revenue_millions("Alnylam ~$2.4B revenue ~15% margin") == 2400.0
    assert _parse_comp_revenue_millions("SmallCo ~$950M revenue ~10% margin") == 950.0
    assert _parse_comp_revenue_millions("NoRevenueStated ~15% margin") is None


def test_live_llm_ions_regression() -> None:
    """End-to-end against Together AI. Asserts the full expected IONS outcome.
    Skipped without TOGETHER_API_KEY. If this passes only intermittently, the
    model is too weak for the rubric — switch MODEL_NAME (see README)."""
    if not os.getenv("TOGETHER_API_KEY"):
        print("SKIP live test: TOGETHER_API_KEY not set")
        return
    from service import judge_operating_margin

    response = asyncio.run(judge_operating_margin(IONS_REQUEST))
    assert response.status == "ok", f"error: {response.error_code} {response.error_detail}"
    assert response.classification == "FAST_GROWER", response.classification
    assert response.anchor_bypassed is True
    assert 0.12 <= response.target_margin <= 0.30, response.target_margin
    assert response.convergence_year == 3
    for comp in response.comps_used:
        rev = _parse_comp_revenue_millions(comp)
        if rev is not None:
            ratio = rev / IONS_TTM_REVENUE
            assert 0.3 <= ratio <= 3.0, f"{comp}: {ratio:.1f}x"
    print(f"live OK: target={response.target_margin} comps={response.comps_used}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")

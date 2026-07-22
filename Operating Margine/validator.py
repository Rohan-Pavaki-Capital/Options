"""Validation rules for the LLM judgment output.

Deterministic enforcement layer — the prompt states the rules, but nothing
here relies on the LLM obeying them.
"""

import json
import re
from typing import Any, Optional

from schemas import MarginRequest

ALLOWED_CLASSIFICATIONS = {
    "SLOW_GROWER",
    "STALWART",
    "FAST_GROWER",
    "CYCLICAL",
    "TURNAROUND",
    "ASSET_PLAY",
}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}

REQUIRED_FIELDS = (
    "classification",
    "target_margin",
    "confidence",
    "margin_driver",
    "anchor_bypassed",
    "damodaran_anchor_used",
    "comps_used",
    "rationale",
)

TARGET_MARGIN_MIN: float = -0.05
TARGET_MARGIN_MAX: float = 0.60
ANCHOR_MULTIPLE_CAP: float = 1.5

# Convergence year is a fixed business rule, never an LLM decision.
FIXED_CONVERGENCE_YEAR: int = 3

# When every forward EPS estimate is negative, the target may not exceed
# this fraction of the effective anchor.
NEGATIVE_EPS_ANCHOR_FRACTION: float = 0.6

# A Damodaran US anchor below this is distorted by loss-making firms.
DISTORTED_ANCHOR_THRESHOLD: float = 0.05
# Distorted-anchor floor: target >= 0.5 * mature_state_anchor * 0.6.
DISTORTED_ANCHOR_FLOOR_FRACTION: float = 0.5 * 0.6

# target_margin must lie within [min(comp margins) - band, max + band].
COMP_MARGIN_BAND: float = 0.10
# Every comp's revenue must be within this ratio band of subject TTM revenue.
COMP_REVENUE_RATIO_MIN: float = 0.3
COMP_REVENUE_RATIO_MAX: float = 3.0

# comps_used string patterns, e.g. "Alnylam ~$2.2B revenue ~15% margin"
_COMP_REVENUE_RE = re.compile(r"~?\$(\d+(?:\.\d+)?)\s*([BbMm])")
_COMP_MARGIN_RE = re.compile(r"~(-?\d+(?:\.\d+)?)\s*%")


class JudgmentValidationError(Exception):
    """Raised when the LLM output fails validation."""


def _effective_anchor(req: MarginRequest) -> float:
    """Highest usable anchor — includes the mature-state anchor so the
    1.5x/0.6x caps don't reject correct answers when the Damodaran anchor
    is distorted (e.g. biotech 0.0108)."""
    candidates = [req.damodaran_us_margin, req.damodaran_global_margin]
    if req.mature_state_anchor is not None:
        candidates.append(req.mature_state_anchor)
    return max(candidates)


def _subject_ttm_revenue(req: MarginRequest) -> Optional[float]:
    for h in req.historicals:
        if "ttm" in h.period.lower():
            return h.revenue
    return req.historicals[0].revenue if req.historicals else None


def _parse_comp_revenue_millions(comp: str) -> Optional[float]:
    m = _COMP_REVENUE_RE.search(comp)
    if not m:
        return None
    value = float(m.group(1))
    return value * 1000 if m.group(2) in ("B", "b") else value


def _parse_comp_margin(comp: str) -> Optional[float]:
    # Strip the revenue token first so "~$2.2B" can never be read as a margin.
    stripped = _COMP_REVENUE_RE.sub("", comp)
    m = _COMP_MARGIN_RE.search(stripped)
    return float(m.group(1)) / 100 if m else None


def validate_judgment(raw_text: str, req: MarginRequest) -> dict[str, Any]:
    """Parse and validate the raw LLM output. Returns the parsed dict or raises.

    The returned dict always carries convergence_year == FIXED_CONVERGENCE_YEAR,
    regardless of anything the LLM returned.
    """
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise JudgmentValidationError(f"Output is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise JudgmentValidationError("Output JSON must be an object")

    missing = [f for f in REQUIRED_FIELDS if f not in data or data[f] is None]
    if missing:
        raise JudgmentValidationError(f"Missing required fields: {', '.join(missing)}")

    if data["classification"] not in ALLOWED_CLASSIFICATIONS:
        raise JudgmentValidationError(
            f"classification '{data['classification']}' not in {sorted(ALLOWED_CLASSIFICATIONS)}"
        )

    if data["confidence"] not in ALLOWED_CONFIDENCE:
        raise JudgmentValidationError(
            f"confidence '{data['confidence']}' not in {sorted(ALLOWED_CONFIDENCE)}"
        )

    try:
        target = float(data["target_margin"])
    except (TypeError, ValueError) as exc:
        raise JudgmentValidationError("target_margin is not a number") from exc
    if not TARGET_MARGIN_MIN <= target <= TARGET_MARGIN_MAX:
        raise JudgmentValidationError(
            f"target_margin {target} outside [{TARGET_MARGIN_MIN}, {TARGET_MARGIN_MAX}]"
        )
    effective_anchor = _effective_anchor(req)
    if effective_anchor > 0 and target > effective_anchor * ANCHOR_MULTIPLE_CAP:
        raise JudgmentValidationError(
            f"target_margin {target} exceeds {ANCHOR_MULTIPLE_CAP}x the highest "
            f"usable anchor ({effective_anchor})"
        )

    # All provided forward EPS estimates negative -> the company cannot
    # plausibly reach industry margin in 3 years; cap the target.
    provided_eps = [fe.eps_est for fe in req.forward_estimates if fe.eps_est is not None]
    if provided_eps and all(e < 0 for e in provided_eps):
        cap = NEGATIVE_EPS_ANCHOR_FRACTION * effective_anchor
        if effective_anchor > 0 and target > cap:
            raise JudgmentValidationError(
                f"All forward EPS estimates are negative, but target_margin {target} "
                f"exceeds {NEGATIVE_EPS_ANCHOR_FRACTION}x the highest usable anchor "
                f"({effective_anchor}). Set a reduced target (FAST_GROWER sub-case c)."
            )

    # DISTORTED ANCHOR RULE: with a distorted Damodaran anchor, a FAST_GROWER
    # target must sit near the mature-state anchor, never near the distortion.
    if (
        req.damodaran_us_margin < DISTORTED_ANCHOR_THRESHOLD
        and data["classification"] == "FAST_GROWER"
        and req.mature_state_anchor is not None
    ):
        floor = DISTORTED_ANCHOR_FLOOR_FRACTION * req.mature_state_anchor
        if target < floor:
            raise JudgmentValidationError(
                f"damodaran_us_margin ({req.damodaran_us_margin}) is distorted (< "
                f"{DISTORTED_ANCHOR_THRESHOLD}); a FAST_GROWER target must be >= "
                f"{floor:.4f} (0.5 x mature_state_anchor {req.mature_state_anchor} "
                f"x 0.6), got {target}. Anchor to mature_state_anchor and the "
                f"profitable comps, not the distorted industry anchor."
            )

    if not isinstance(data["margin_driver"], bool):
        raise JudgmentValidationError("margin_driver must be a boolean")

    if not isinstance(data["anchor_bypassed"], bool):
        raise JudgmentValidationError("anchor_bypassed must be a boolean")

    if data["classification"] == "ASSET_PLAY":
        if data["margin_driver"] is not False:
            raise JudgmentValidationError("ASSET_PLAY requires margin_driver = false")
        if req.past_avg_margin is None:
            raise JudgmentValidationError(
                "ASSET_PLAY requires past_avg_margin in the input data; it was not "
                "provided — choose a different classification"
            )
        if abs(target - req.past_avg_margin) > 1e-6:
            raise JudgmentValidationError(
                f"ASSET_PLAY requires target_margin == past_avg_margin "
                f"({req.past_avg_margin}), got {target}"
            )

    if not isinstance(data["comps_used"], list) or not all(
        isinstance(c, str) for c in data["comps_used"]
    ):
        raise JudgmentValidationError("comps_used must be a list of strings")

    # COMP CONSISTENCY: target must lie within the band spanned by the comps'
    # own stated margins.
    comp_margins = [
        m for m in (_parse_comp_margin(c) for c in data["comps_used"]) if m is not None
    ]
    if comp_margins:
        low = min(comp_margins) - COMP_MARGIN_BAND
        high = max(comp_margins) + COMP_MARGIN_BAND
        if not low <= target <= high:
            raise JudgmentValidationError(
                f"target_margin {target} is outside the range spanned by your own "
                f"comps' margins [{low:.4f}, {high:.4f}] (comps: {data['comps_used']}). "
                f"The target must be consistent with the comps you chose."
            )

    # COMP SCALE: every parseable comp revenue must be within 0.3x-3x of the
    # subject's TTM revenue (subject revenue assumed to be in millions).
    ttm_revenue = _subject_ttm_revenue(req)
    if ttm_revenue and ttm_revenue > 0:
        for comp in data["comps_used"]:
            comp_rev = _parse_comp_revenue_millions(comp)
            if comp_rev is None:
                continue
            ratio = comp_rev / ttm_revenue
            if not COMP_REVENUE_RATIO_MIN <= ratio <= COMP_REVENUE_RATIO_MAX:
                raise JudgmentValidationError(
                    f"Comp '{comp}' has revenue {ratio:.1f}x the subject's TTM revenue "
                    f"({ttm_revenue:,.1f}) — comps must be within "
                    f"{COMP_REVENUE_RATIO_MIN}x-{COMP_REVENUE_RATIO_MAX}x. Replace it "
                    f"with a comp at similar scale."
                )

    if not isinstance(data["rationale"], str) or not data["rationale"].strip():
        raise JudgmentValidationError("rationale must be a non-empty string")

    data["target_margin"] = target
    data["convergence_year"] = FIXED_CONVERGENCE_YEAR
    return data

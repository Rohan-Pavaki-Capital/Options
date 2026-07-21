"""Validation rules for the LLM judgment output."""

import json
from typing import Any

ALLOWED_CLASSIFICATIONS = {"RAMPING", "MATURE", "DISTRESSED", "TRANSFORMING"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}

REQUIRED_FIELDS = (
    "classification",
    "target_margin",
    "convergence_year",
    "confidence",
    "damodaran_anchor_used",
    "comps_used",
    "rationale",
)

TARGET_MARGIN_MIN: float = -0.05
TARGET_MARGIN_MAX: float = 0.60
ANCHOR_MULTIPLE_CAP: float = 1.5


class JudgmentValidationError(Exception):
    """Raised when the LLM output fails validation."""


def validate_judgment(
    raw_text: str, damodaran_us_margin: float, damodaran_global_margin: float
) -> dict[str, Any]:
    """Parse and validate the raw LLM output. Returns the parsed dict or raises."""
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
    max_anchor = max(damodaran_us_margin, damodaran_global_margin)
    if max_anchor > 0 and target > max_anchor * ANCHOR_MULTIPLE_CAP:
        raise JudgmentValidationError(
            f"target_margin {target} exceeds {ANCHOR_MULTIPLE_CAP}x the highest "
            f"Damodaran anchor ({max_anchor})"
        )

    year = data["convergence_year"]
    if isinstance(year, float) and year.is_integer():
        year = int(year)
    if not isinstance(year, int) or isinstance(year, bool) or not 1 <= year <= 10:
        raise JudgmentValidationError("convergence_year must be an integer between 1 and 10")
    data["convergence_year"] = year

    if not isinstance(data["comps_used"], list) or not all(
        isinstance(c, str) for c in data["comps_used"]
    ):
        raise JudgmentValidationError("comps_used must be a list of strings")

    if not isinstance(data["rationale"], str) or not data["rationale"].strip():
        raise JudgmentValidationError("rationale must be a non-empty string")

    data["target_margin"] = target
    return data

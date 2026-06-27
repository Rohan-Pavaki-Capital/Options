"""
Stage 3 — Anthropic Claude Sonnet extraction package.

Receives the final classified pages from Stages 1+2 and extracts structured
share-based compensation JSON using two-pass extraction (extract + validate),
then runs arithmetic roll-forward checks and merges per-batch results.

Public API:
    extract_with_claude(client, texts, images, model, ...)
    validate_all_plans(data) -> data with _validation_warnings
    merge_results(results_list) -> single merged dict
    set_verbose(bool)  # toggles stderr logging
    OUTPUT_SCHEMA      # JSON schema string
"""

from Anthropic.code import (
    extract_with_claude,
    validate_all_plans,
    validate_rollforward,
    validate_final_output,
    merge_results,
    normalize_currency,
    set_verbose,
)
from Anthropic.prompt import SYSTEM_PROMPT, EXTRACTION_PROMPT, VALIDATION_PROMPT
from Anthropic.schema import OUTPUT_SCHEMA

__all__ = [
    "extract_with_claude",
    "validate_all_plans",
    "validate_rollforward",
    "validate_final_output",
    "merge_results",
    "normalize_currency",
    "set_verbose",
    "SYSTEM_PROMPT",
    "EXTRACTION_PROMPT",
    "VALIDATION_PROMPT",
    "OUTPUT_SCHEMA",
]

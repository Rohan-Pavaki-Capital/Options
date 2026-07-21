"""Stage 4 — tally / reconciliation package.

The implementation lives in `tally.py`; this package re-exports its public
API so existing callers keep using it as `from . import tally` and then
`tally.run_tally(...)`, `tally.ASSET_MEMO_KEYS`, etc., unchanged.
"""

from .tally import (
    ASSET_MEMO_KEYS,
    LIABILITY_MEMO_KEYS,
    coerce_number,
    coerce_result_numbers,
    run_tally,
    is_balanced,
    build_gap_message,
    add_unbalanced_warnings,
    regroup_current_tax_liability,
    find_chain_duplicates,
    build_duplicate_message,
    guard_equity_adjacent_buckets,
    sanity_check_other_buckets,
    normalize_to_millions,
    attach_debt_summary,
)

__all__ = [
    "ASSET_MEMO_KEYS",
    "LIABILITY_MEMO_KEYS",
    "coerce_number",
    "coerce_result_numbers",
    "run_tally",
    "is_balanced",
    "build_gap_message",
    "add_unbalanced_warnings",
    "regroup_current_tax_liability",
    "find_chain_duplicates",
    "build_duplicate_message",
    "guard_equity_adjacent_buckets",
    "sanity_check_other_buckets",
    "normalize_to_millions",
    "attach_debt_summary",
]

"""Configuration for the balance-sheet standardization pipeline.

All secrets come from .env (python-dotenv) — never hardcoded.
Required keys:
  LLAMAPARSE_API_KEY  - LlamaParse / LlamaCloud (PDF -> markdown)
  TOGETHER_API_KEY    - Together AI (schema-mapping LLM)
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load the project-root .env (Balance_sheet/ sits directly under the root),
# falling back to the default python-dotenv search.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv()  # no-op if already loaded; covers non-standard layouts

LLAMAPARSE_API_KEY = os.getenv("LLAMAPARSE_API_KEY")
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY")

# LLM provider + model for the standardization step (Stage 3).
# Together AI exposes an OpenAI-compatible API; change these two constants
# (or set BALANCE_SHEET_MODEL in .env) to switch provider/model.
LLM_BASE_URL = "https://api.together.xyz/v1"
LLM_MODEL = os.getenv("BALANCE_SHEET_MODEL", "deepseek-ai/DeepSeek-V4-Pro")

# Stage 1 — balance-sheet title variants (matched case-insensitively).
TITLE_VARIANTS = [
    "CONDENSED CONSOLIDATED BALANCE SHEET",
    "CONSOLIDATED BALANCE SHEET",
    "BALANCE SHEET",
    "STATEMENTS OF FINANCIAL POSITION",
    "CONSOLIDATED STATEMENTS OF FINANCIAL POSITION",
    # IFRS singular wording (e.g. Scout24) — the plural variants above never
    # match "Consolidated statement of financial position".
    "CONSOLIDATED STATEMENT OF FINANCIAL POSITION",
    # Bare singular, no "consolidated" — ASX/AASB filers (e.g. Clinuvel) title
    # the primary statement just "Statement of Financial Position as at <date>".
    # This was previously excluded to avoid management-report "condensed
    # statement of financial position" summary tables (EUR million, % change)
    # shadowing the real statement; the locator now skips %-change summary
    # pages (_looks_like_change_summary), so the bare variant is safe.
    "STATEMENT OF FINANCIAL POSITION",
    # French (ESEF filings are official-language only; e.g. Eiffage's URD).
    # Matching is accent-folded in the locator, so accents here are cosmetic.
    "BILAN CONSOLIDÉ",
    "ÉTAT CONSOLIDÉ DE LA SITUATION FINANCIÈRE",
    "ÉTAT DE LA SITUATION FINANCIÈRE CONSOLIDÉE",
    # Eiffage-style URDs title the statement pages just "Actif" / "Capitaux
    # propres et passifs" under the running header "Comptes consolidés" —
    # broad, but the assets-total + equity-total confirm gates in the locator
    # keep it from matching notes/TOC pages.
    "COMPTES CONSOLIDÉS",
]

# Stage 4 — rounding-aware tally tolerance. Filings round every printed line,
# so summed lines rarely tie to the printed total exactly. Never require an
# exact tie; never plug the difference.
def tally_tolerance(mapped_lines: int, printed_total: float) -> float:
    """Allowed |sum - printed total| gap: max(number of mapped lines,
    0.1% of the printed total), floored at 1."""
    return max(mapped_lines, abs(printed_total) * 0.001, 1)


# Stage 4 — max LLM correction re-prompts when a side does not tally
# (env-overridable; after these, the result is returned UNBALANCED with a
# clear warning — a reconciling number is never fabricated).
TALLY_MAX_RETRIES = int(os.getenv("BALANCE_SHEET_TALLY_RETRIES", "2"))

# ---------------------------------------------------------------------------
# Fixed target schema (Damodaran-style buckets). These keys are FIXED —
# every balance-sheet line maps into exactly one; no new keys may be invented.
# ---------------------------------------------------------------------------

ASSET_NON_CURRENT_KEYS = [
    "lease_assets", "real_estate_assets", "investment_assets",
    "investment_in_other", "assets_held_for_sale",
    "asset_from_discontinued_business", "pension_assets",
    "other_assets", "ppe",
]

ASSET_CURRENT_KEYS = [
    "lease_assets", "inventory", "accounts_trade_receivable",
    "tax", "other_current_assets",
]

# NOTE: the keys mirror the Excel template's rows EXACTLY — no extra fields.
# There is deliberately no non-current debt bucket: long-term debt stays OUT
# of the buckets, in memo_excluded.long_term_debt (memo-excluded design).
LIABILITY_NON_CURRENT_KEYS = [
    "pension", "lease_liabilities", "deferred_rev_and_tax", "other_liabilities",
]

LIABILITY_CURRENT_KEYS = [
    "debt", "lease_liabilities", "accounts_trade_payable",
    "deferred_rev_and_tax", "other_current_liabilities",
]

# Memo-excluded categories — kept OUT of every bucket above, in a separate
# memo_excluded object. Reconciliation (Stage 4):
#   sum(asset buckets) + cash_and_marketable_securities + goodwill + intangibles
#     == printed total_assets
#   sum(liability buckets) + long_term_debt == printed total_liabilities
MEMO_KEYS = [
    "cash_and_marketable_securities", "goodwill", "intangibles", "long_term_debt",
]


def empty_result() -> dict:
    """Return a fresh copy of the exact output JSON shape."""
    return {
        "company": "",
        "period": "",
        "currency": "",
        "unit_label": "",
        "source_pages": [],
        "assets": {
            "non_current": {k: 0 for k in ASSET_NON_CURRENT_KEYS},
            "current": {k: 0 for k in ASSET_CURRENT_KEYS},
        },
        "liabilities": {
            "non_current": {k: 0 for k in LIABILITY_NON_CURRENT_KEYS},
            "current": {k: 0 for k in LIABILITY_CURRENT_KEYS},
            "preferred_stock": 0,
            "mezzanine_equity": 0,
        },
        "memo_excluded": {k: 0 for k in MEMO_KEYS},
        "filing_totals": {"total_assets": 0, "total_liabilities": 0},
        "tally": {
            "sum_assets": 0, "sum_liabilities": 0,
            "assets_balanced": False, "liabilities_balanced": False,
        },
        "warnings": [],
    }


def require_llamaparse_key() -> str:
    if not LLAMAPARSE_API_KEY:
        raise RuntimeError(
            "LLAMAPARSE_API_KEY is missing from .env - LlamaParse (PDF -> "
            "markdown) cannot run. Add it to the project .env file."
        )
    return LLAMAPARSE_API_KEY


def require_together_key() -> str:
    if not TOGETHER_API_KEY:
        raise RuntimeError(
            "TOGETHER_API_KEY is missing from .env - the standardization LLM "
            "step cannot run and will NOT be silently skipped. Add "
            "TOGETHER_API_KEY to the project .env file."
        )
    return TOGETHER_API_KEY

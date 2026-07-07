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
LLM_MODEL = os.getenv("BALANCE_SHEET_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo")

# Stage 1 — balance-sheet title variants (matched case-insensitively).
TITLE_VARIANTS = [
    "CONDENSED CONSOLIDATED BALANCE SHEET",
    "CONSOLIDATED BALANCE SHEET",
    "BALANCE SHEET",
    "STATEMENTS OF FINANCIAL POSITION",
    "CONSOLIDATED STATEMENTS OF FINANCIAL POSITION",
]

# Stage 4 — allowed rounding difference between summed buckets and printed totals.
TALLY_TOLERANCE = 1

# Stage 4 — max LLM correction re-prompts when a side does not tally
# (env-overridable; after these, the deterministic other_* plug reconciles).
TALLY_MAX_RETRIES = int(os.getenv("BALANCE_SHEET_TALLY_RETRIES", "2"))

# Stage 4 — a plug at or below this size is NOT rounding noise: a correct
# mapping ties exactly, so a small residual gap means a line landed in the
# wrong bucket. The plug still applies, but with a loud wrong-bucket warning.
PLUG_SUSPICIOUS_GAP = int(os.getenv("BALANCE_SHEET_PLUG_SUSPICIOUS_GAP", "50"))

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

# NOTE: the bucket keys mirror the Excel template's rows EXACTLY — no extra
# bucket fields. There is deliberately no non-current debt bucket and no cash
# or goodwill bucket: those lines go into the MEMO section below (user
# decision 2026-07-05 — the workbook's internal logic consumes them; they must
# NOT be folded into the other_* buckets).
LIABILITY_NON_CURRENT_KEYS = [
    "pension", "lease_liabilities", "deferred_rev_and_tax", "other_liabilities",
]

LIABILITY_CURRENT_KEYS = [
    "debt", "lease_liabilities", "accounts_trade_payable",
    "deferred_rev_and_tax", "other_current_liabilities",
]

# Memo lines the Excel workbook handles with its own internal logic — kept OUT
# of the buckets but still part of the tally verification:
#   sum(asset buckets) + cash_and_st_investments + goodwill_and_intangibles
#       == printed Total Assets
#   sum(liability buckets) + long_term_debt == printed Total Liabilities
MEMO_ASSET_KEYS = ["cash_and_st_investments", "goodwill_and_intangibles"]
MEMO_LIABILITY_KEYS = ["long_term_debt"]
MEMO_KEYS = MEMO_ASSET_KEYS + MEMO_LIABILITY_KEYS


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
        "memo": {k: 0 for k in MEMO_KEYS},
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

"""
industry_route.py — standalone "company industry" feature.

Exposes an APIRouter that app.py mounts on the SAME origin / uvicorn server as
the Simply Wall St forecast, the beta lookup and the credit-rating lookup:

  GET  /api/industry?ticker=AAPL  -> ticker -> Damodaran industry (JSON)

What it does:
  1. Look up the stock on the GuruFocus API (stock "summary" endpoint) and read
     its most granular industry field (Morningstar/GICS-style `subindustry`,
     e.g. "Consumer Electronics", "Oil & Gas Integrated").
  2. Standardise that GuruFocus industry to ONE of Aswath Damodaran's exact 94
     industry names (e.g. "Electronics (Consumer & Office)", "Oil/Gas
     (Integrated)") via a 3-layer mapping:
        (a) a hardcoded GF_TO_DAMODARAN dict (exact, case-insensitive, trimmed),
        (b) a normalised fuzzy match (difflib) against the 94 names, cutoff 0.8,
        (c) null — no confident match; never guess.
  3. Return JSON with the ticker, the raw GF industry, and the Damodaran name
     (or null) plus how it was matched.

CRITICAL: ``damodaran_industry`` in every response is EXACTLY one of the 94
Damodaran names or null. The value is written into an Excel data-validation
dropdown that rejects anything not on the list, so a final guard forces any
value not in the 94-name set to null. The endpoint never raises (never 500):
on any failure it returns ``damodaran_industry: null`` with an ``error`` field.

Output shape (success):
  {
    "ticker": "AAPL",
    "gf_industry": "Consumer Electronics",
    "damodaran_industry": "Electronics (Consumer & Office)",
    "match_method": "dict",            # "dict" | "fuzzy" | "none"
    "source": "gurufocus"
  }
Output shape (no confident match):
  {
    "ticker": "AAPL",
    "gf_industry": "Consumer Electronics",
    "damodaran_industry": null,
    "match_method": "none",
    "source": "gurufocus"
  }

Config (read from the environment; app.py loads them from .env):
  GURUFOCUS_API_KEY   required — the GuruFocus API token.
  GURUFOCUS_BASE_URL  optional — defaults to https://api.gurufocus.com/public/user
"""
from __future__ import annotations

import difflib
import os
import re

from fastapi import APIRouter, Query

router = APIRouter()

GURUFOCUS_API_KEY = os.environ.get("GURUFOCUS_API_KEY", "").strip()
GURUFOCUS_BASE_URL = os.environ.get(
    "GURUFOCUS_BASE_URL", "https://api.gurufocus.com/public/user"
).strip().rstrip("/")


# ===========================================================================
# THE 94 DAMODARAN INDUSTRY NAMES — the ONLY allowed output values.
# Copied byte-for-byte from the spec, including the deliberate quirks:
#   "Heathcare Information and Technology" (misspelled),
#   "Rubber& Tires" (no space), the periods and parentheses.
# ===========================================================================
DAMODARAN_INDUSTRIES = [
    "Advertising",
    "Aerospace/Defense",
    "Air Transport",
    "Apparel",
    "Auto & Truck",
    "Auto Parts",
    "Bank (Money Center)",
    "Banks (Regional)",
    "Beverage (Alcoholic)",
    "Beverage (Soft)",
    "Broadcasting",
    "Brokerage & Investment Banking",
    "Building Materials",
    "Business & Consumer Services",
    "Cable TV",
    "Chemical (Basic)",
    "Chemical (Diversified)",
    "Chemical (Specialty)",
    "Coal & Related Energy",
    "Computer Services",
    "Computers/Peripherals",
    "Construction Supplies",
    "Diversified",
    "Drugs (Biotechnology)",
    "Drugs (Pharmaceutical)",
    "Education",
    "Electrical Equipment",
    "Electronics (Consumer & Office)",
    "Electronics (General)",
    "Engineering/Construction",
    "Entertainment",
    "Environmental & Waste Services",
    "Farming/Agriculture",
    "Financial Svcs. (Non-bank & Insurance)",
    "Food Processing",
    "Food Wholesalers",
    "Furn/Home Furnishings",
    "Green & Renewable Energy",
    "Healthcare Products",
    "Healthcare Support Services",
    "Heathcare Information and Technology",
    "Homebuilding",
    "Hospitals/Healthcare Facilities",
    "Hotel/Gaming",
    "Household Products",
    "Information Services",
    "Insurance (General)",
    "Insurance (Life)",
    "Insurance (Prop/Cas.)",
    "Investments & Asset Management",
    "Machinery",
    "Metals & Mining",
    "Office Equipment & Services",
    "Oil/Gas (Integrated)",
    "Oil/Gas (Production and Exploration)",
    "Oil/Gas Distribution",
    "Oilfield Svcs/Equip.",
    "Packaging & Container",
    "Paper/Forest Products",
    "Power",
    "Precious Metals",
    "Publishing & Newspapers",
    "R.E.I.T.",
    "Real Estate (Development)",
    "Real Estate (General/Diversified)",
    "Real Estate (Operations & Services)",
    "Recreation",
    "Reinsurance",
    "Restaurant/Dining",
    "Retail (Automotive)",
    "Retail (Building Supply)",
    "Retail (Distributors)",
    "Retail (General)",
    "Retail (Grocery and Food)",
    "Retail (Online)",
    "Retail (Special Lines)",
    "Rubber& Tires",
    "Semiconductor",
    "Semiconductor Equip",
    "Shipbuilding & Marine",
    "Shoe",
    "Software (Entertainment)",
    "Software (Internet)",
    "Software (System & Application)",
    "Steel",
    "Telecom (Wireless)",
    "Telecom. Equipment",
    "Telecom. Services",
    "Tobacco",
    "Transportation",
    "Transportation (Railroads)",
    "Trucking",
    "Utility (General)",
    "Utility (Water)",
]

# Fast membership set — the final correctness guard checks against this.
DAMODARAN_SET = set(DAMODARAN_INDUSTRIES)


# ===========================================================================
# Layer 1: GuruFocus (Morningstar) industry -> Damodaran name.
# Keys are GuruFocus `subindustry` strings (the most granular Morningstar
# industry). Matched case-insensitively after trimming and whitespace/​dash
# normalisation (see _normalize_key). Every VALUE here must be one of the 94
# names above — enforced at import time by _validate_dict().
# ===========================================================================
GF_TO_DAMODARAN = {
    # --- Basic Materials -------------------------------------------------
    "Agricultural Inputs": "Farming/Agriculture",
    "Building Materials": "Building Materials",
    "Chemicals": "Chemical (Basic)",
    "Specialty Chemicals": "Chemical (Specialty)",
    "Coking Coal": "Coal & Related Energy",
    "Thermal Coal": "Coal & Related Energy",
    "Aluminum": "Metals & Mining",
    "Copper": "Metals & Mining",
    "Other Industrial Metals & Mining": "Metals & Mining",
    "Gold": "Precious Metals",
    "Silver": "Precious Metals",
    "Other Precious Metals & Mining": "Precious Metals",
    "Paper & Paper Products": "Paper/Forest Products",
    "Lumber & Wood Production": "Paper/Forest Products",
    "Steel": "Steel",

    # --- Communication Services -----------------------------------------
    "Advertising Agencies": "Advertising",
    "Publishing": "Publishing & Newspapers",
    "Broadcasting": "Broadcasting",
    "Entertainment": "Entertainment",
    "Telecom Services": "Telecom. Services",
    "Electronic Gaming & Multimedia": "Software (Entertainment)",
    "Internet Content & Information": "Software (Internet)",

    # --- Consumer Cyclical ----------------------------------------------
    "Auto & Truck Dealerships": "Retail (Automotive)",
    "Auto Manufacturers": "Auto & Truck",
    "Auto Parts": "Auto Parts",
    "Recreational Vehicles": "Recreation",
    "Furnishings, Fixtures & Appliances": "Furn/Home Furnishings",
    "Residential Construction": "Homebuilding",
    "Textile Manufacturing": "Apparel",
    "Apparel Manufacturing": "Apparel",
    "Footwear & Accessories": "Shoe",
    "Packaging & Containers": "Packaging & Container",
    "Personal Services": "Business & Consumer Services",
    "Restaurants": "Restaurant/Dining",
    "Apparel Retail": "Retail (Special Lines)",
    "Department Stores": "Retail (General)",
    "Home Improvement Retail": "Retail (Building Supply)",
    "Luxury Goods": "Retail (Special Lines)",
    "Internet Retail": "Retail (Online)",
    "Specialty Retail": "Retail (Special Lines)",
    "Gambling": "Hotel/Gaming",
    "Leisure": "Recreation",
    "Lodging": "Hotel/Gaming",
    "Resorts & Casinos": "Hotel/Gaming",
    "Travel Services": "Hotel/Gaming",

    # --- Consumer Defensive ---------------------------------------------
    "Beverages - Brewers": "Beverage (Alcoholic)",
    "Beverages - Wineries & Distilleries": "Beverage (Alcoholic)",
    "Beverages - Non-Alcoholic": "Beverage (Soft)",
    "Confectioners": "Food Processing",
    "Farm Products": "Farming/Agriculture",
    "Household & Personal Products": "Household Products",
    "Packaged Foods": "Food Processing",
    "Education & Training Services": "Education",
    "Discount Stores": "Retail (General)",
    "Food Distribution": "Food Wholesalers",
    "Grocery Stores": "Retail (Grocery and Food)",
    "Tobacco": "Tobacco",

    # --- Energy ----------------------------------------------------------
    "Oil & Gas Drilling": "Oilfield Svcs/Equip.",
    "Oil & Gas E&P": "Oil/Gas (Production and Exploration)",
    "Oil & Gas Integrated": "Oil/Gas (Integrated)",
    "Oil & Gas Midstream": "Oil/Gas Distribution",
    "Oil & Gas Refining & Marketing": "Oil/Gas (Integrated)",
    "Oil & Gas Equipment & Services": "Oilfield Svcs/Equip.",
    "Uranium": "Coal & Related Energy",
    "Solar": "Green & Renewable Energy",

    # --- Financial Services ---------------------------------------------
    "Banks - Diversified": "Bank (Money Center)",
    "Banks - Regional": "Banks (Regional)",
    "Mortgage Finance": "Financial Svcs. (Non-bank & Insurance)",
    "Capital Markets": "Brokerage & Investment Banking",
    "Financial Data & Stock Exchanges": "Brokerage & Investment Banking",
    "Insurance - Life": "Insurance (Life)",
    "Insurance - Property & Casualty": "Insurance (Prop/Cas.)",
    "Insurance - Reinsurance": "Reinsurance",
    "Insurance - Specialty": "Insurance (General)",
    "Insurance - Diversified": "Insurance (General)",
    "Insurance Brokers": "Insurance (General)",
    "Asset Management": "Investments & Asset Management",
    "Credit Services": "Financial Svcs. (Non-bank & Insurance)",
    "Financial Conglomerates": "Financial Svcs. (Non-bank & Insurance)",
    "Shell Companies": "Diversified",

    # --- Healthcare ------------------------------------------------------
    "Biotechnology": "Drugs (Biotechnology)",
    "Drug Manufacturers - General": "Drugs (Pharmaceutical)",
    "Drug Manufacturers - Specialty & Generic": "Drugs (Pharmaceutical)",
    "Healthcare Plans": "Healthcare Support Services",
    "Medical Care Facilities": "Hospitals/Healthcare Facilities",
    "Pharmaceutical Retailers": "Retail (Grocery and Food)",
    "Health Information Services": "Heathcare Information and Technology",
    "Medical Devices": "Healthcare Products",
    "Medical Instruments & Supplies": "Healthcare Products",
    "Diagnostics & Research": "Healthcare Products",
    "Medical Distribution": "Healthcare Support Services",

    # --- Industrials -----------------------------------------------------
    "Aerospace & Defense": "Aerospace/Defense",
    "Specialty Industrial Machinery": "Machinery",
    "Farm & Heavy Construction Machinery": "Machinery",
    "Industrial Distribution": "Retail (Distributors)",
    "Business Equipment & Supplies": "Office Equipment & Services",
    "Conglomerates": "Diversified",
    "Consulting Services": "Business & Consumer Services",
    "Rental & Leasing Services": "Business & Consumer Services",
    "Security & Protection Services": "Business & Consumer Services",
    "Staffing & Employment Services": "Business & Consumer Services",
    "Specialty Business Services": "Business & Consumer Services",
    "Electrical Equipment & Parts": "Electrical Equipment",
    "Engineering & Construction": "Engineering/Construction",
    "Infrastructure Operations": "Engineering/Construction",
    "Building Products & Equipment": "Building Materials",
    "Pollution & Treatment Controls": "Environmental & Waste Services",
    "Tools & Accessories": "Machinery",
    "Metal Fabrication": "Machinery",
    "Airports & Air Services": "Air Transport",
    "Airlines": "Air Transport",
    "Railroads": "Transportation (Railroads)",
    "Marine Shipping": "Shipbuilding & Marine",
    "Trucking": "Trucking",
    "Integrated Freight & Logistics": "Transportation",
    "Waste Management": "Environmental & Waste Services",

    # --- Real Estate -----------------------------------------------------
    # All REIT subtypes -> R.E.I.T. (also covered by the "REIT" prefix rule).
    "REIT - Diversified": "R.E.I.T.",
    "REIT - Healthcare Facilities": "R.E.I.T.",
    "REIT - Hotel & Motel": "R.E.I.T.",
    "REIT - Industrial": "R.E.I.T.",
    "REIT - Mortgage": "R.E.I.T.",
    "REIT - Office": "R.E.I.T.",
    "REIT - Residential": "R.E.I.T.",
    "REIT - Retail": "R.E.I.T.",
    "REIT - Specialty": "R.E.I.T.",
    "Real Estate - Development": "Real Estate (Development)",
    "Real Estate - Diversified": "Real Estate (General/Diversified)",
    "Real Estate Services": "Real Estate (Operations & Services)",

    # --- Technology ------------------------------------------------------
    "Information Technology Services": "Computer Services",
    "Software - Application": "Software (System & Application)",
    "Software - Infrastructure": "Software (System & Application)",
    "Communication Equipment": "Telecom. Equipment",
    "Computer Hardware": "Computers/Peripherals",
    "Consumer Electronics": "Electronics (Consumer & Office)",
    "Electronic Components": "Electronics (General)",
    "Electronics & Computer Distribution": "Retail (Distributors)",
    "Scientific & Technical Instruments": "Electronics (General)",
    "Semiconductor Equipment & Materials": "Semiconductor Equip",
    "Semiconductors": "Semiconductor",

    # --- Utilities -------------------------------------------------------
    "Utilities - Diversified": "Utility (General)",
    "Utilities - Regulated Electric": "Utility (General)",
    "Utilities - Regulated Gas": "Utility (General)",
    "Utilities - Regulated Water": "Utility (Water)",
    "Utilities - Renewable": "Green & Renewable Energy",
    "Utilities - Independent Power Producers": "Power",
}


# ===========================================================================
# Normalisation + matching
# ===========================================================================
def _normalize_key(s: str) -> str:
    """Normalise a GF industry string for dict lookup.

    Lower-cases, unifies dash variants (–, —, -) and ampersand spacing, and
    collapses whitespace so "Banks-Regional", "Banks - Regional" and
    "banks  -  regional" all collide.
    """
    s = (s or "").strip().lower()
    s = s.replace("–", "-").replace("—", "-")  # en/em dash -> hyphen
    s = re.sub(r"\s*-\s*", " - ", s)                      # standardise " - "
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# Pre-build a normalised view of the dict for case/whitespace-insensitive hits.
_NORMALIZED_DICT = {_normalize_key(k): v for k, v in GF_TO_DAMODARAN.items()}


def _normalize_fuzzy(s: str) -> str:
    """Aggressive normalisation for fuzzy matching: drop all punctuation."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Map normalised-Damodaran-name -> exact Damodaran name, for fuzzy lookups.
_FUZZY_DAMODARAN = {_normalize_fuzzy(name): name for name in DAMODARAN_INDUSTRIES}
_FUZZY_KEYS = list(_FUZZY_DAMODARAN.keys())


def map_industry(gf_industry: str) -> tuple[str | None, str]:
    """Map a GuruFocus industry to a Damodaran name.

    Returns (damodaran_industry_or_None, match_method) where match_method is
    one of "dict", "fuzzy", "none". Never returns a value outside the 94 names
    (the caller also re-guards before returning to the client).
    """
    if not gf_industry or not gf_industry.strip():
        return None, "none"

    # Layer 1: exact/normalised dict.
    key = _normalize_key(gf_industry)
    if key in _NORMALIZED_DICT:
        return _NORMALIZED_DICT[key], "dict"

    # Layer 1b: any "REIT ..." subtype not explicitly listed -> R.E.I.T.
    if key.startswith("reit"):
        return "R.E.I.T.", "dict"

    # Layer 2: normalised fuzzy match against the 94 names (high cutoff).
    fz = _normalize_fuzzy(gf_industry)
    if fz:
        close = difflib.get_close_matches(fz, _FUZZY_KEYS, n=1, cutoff=0.8)
        if close:
            return _FUZZY_DAMODARAN[close[0]], "fuzzy"

    # Layer 3: no confident match.
    return None, "none"


# ===========================================================================
# GuruFocus integration
# ===========================================================================
# Small in-memory cache: ticker (upper) -> full response dict. Survives for the
# life of the process so repeat calls don't re-hit GuruFocus.
_CACHE: dict[str, dict] = {}


def _fetch_gf_industry(ticker: str) -> str | None:
    """Fetch the most granular GuruFocus industry for a ticker.

    Returns the `subindustry` string (Morningstar industry) or None if the
    response carries no industry. Raises on network/HTTP/parse errors so the
    caller can convert them into a safe null+error response.
    """
    import requests

    url = f"{GURUFOCUS_BASE_URL}/{GURUFOCUS_API_KEY}/stock/{ticker}/summary"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"GuruFocus HTTP {r.status_code}")
    data = r.json()  # raises ValueError on non-JSON

    general = ((data or {}).get("summary") or {}).get("general") or {}
    if not isinstance(general, dict):
        return None
    # Prefer the most granular field; fall back through coarser ones.
    for field in ("subindustry", "group", "sector"):
        val = general.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def get_industry(ticker: str) -> dict:
    """Resolve a ticker to a Damodaran industry. Never raises.

    On any failure returns damodaran_industry=null with an "error" field.
    """
    ticker = (ticker or "").strip().upper()
    result = {
        "ticker": ticker,
        "gf_industry": None,
        "damodaran_industry": None,
        "match_method": "none",
        "source": "gurufocus",
    }

    if not ticker:
        result["error"] = "A ticker is required."
        return result

    if ticker in _CACHE:
        return _CACHE[ticker]

    if not GURUFOCUS_API_KEY:
        result["error"] = "GURUFOCUS_API_KEY is not set."
        return result

    try:
        gf_industry = _fetch_gf_industry(ticker)
    except Exception as e:  # network, HTTP, JSON, anything — never 500.
        result["error"] = f"GuruFocus lookup failed: {type(e).__name__}: {e}"
        return result

    result["gf_industry"] = gf_industry
    if not gf_industry:
        result["error"] = "GuruFocus returned no industry for this ticker."
        # leave damodaran_industry null, match_method "none"
        _CACHE[ticker] = result
        return result

    damodaran, method = map_industry(gf_industry)

    # CRITICAL final guard: never emit a value that isn't one of the 94 names.
    if damodaran not in DAMODARAN_SET:
        damodaran, method = None, "none"

    result["damodaran_industry"] = damodaran
    result["match_method"] = method
    _CACHE[ticker] = result
    return result


@router.get("/api/industry")
def api_industry(
    ticker: str = Query(..., description="Stock ticker, e.g. AAPL"),
):
    """Ticker -> GuruFocus industry standardised to a Damodaran name (JSON).

    Always returns HTTP 200. ``damodaran_industry`` is exactly one of the 94
    Damodaran names or null.
    """
    return get_industry(ticker)


# ===========================================================================
# Startup validation — guarantees the dict can never emit an invalid industry.
# ===========================================================================
def _validate_dict() -> None:
    """Assert every GF_TO_DAMODARAN value is one of the 94 Damodaran names.

    Runs at import time, so a typo in a mapping value fails fast at startup
    rather than silently emitting an Excel-rejected string at request time.
    """
    bad = {k: v for k, v in GF_TO_DAMODARAN.items() if v not in DAMODARAN_SET}
    if bad:
        raise ValueError(
            "GF_TO_DAMODARAN has values not in the 94 Damodaran names: "
            + ", ".join(f"{k!r} -> {v!r}" for k, v in bad.items())
        )
    if len(DAMODARAN_INDUSTRIES) != 94 or len(DAMODARAN_SET) != 94:
        raise ValueError(
            f"Expected 94 unique Damodaran names, got "
            f"{len(DAMODARAN_INDUSTRIES)} ({len(DAMODARAN_SET)} unique)."
        )


_validate_dict()


# ===========================================================================
# Standalone CLI for quick testing:
#   python Company_Industry/industry_route.py            # runs the test list
#   python Company_Industry/industry_route.py AAPL XOM   # specific tickers
# ===========================================================================
if __name__ == "__main__":
    import json
    import sys

    # Load GURUFOCUS_* from a sibling/parent .env when run directly.
    if not GURUFOCUS_API_KEY:
        for _dir in (os.path.dirname(__file__), os.path.dirname(os.path.dirname(__file__))):
            _envp = os.path.join(_dir, ".env")
            if os.path.exists(_envp):
                with open(_envp, encoding="utf-8") as _f:
                    for _line in _f:
                        _line = _line.strip()
                        if _line.startswith("GURUFOCUS_API_KEY"):
                            GURUFOCUS_API_KEY = _line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif _line.startswith("GURUFOCUS_BASE_URL"):
                            GURUFOCUS_BASE_URL = _line.split("=", 1)[1].strip().strip('"').strip("'").rstrip("/")
            if GURUFOCUS_API_KEY:
                break

    tickers = sys.argv[1:] or ["AAPL", "XOM", "JPM", "NVDA", "KO", "PFE", "AMZN"]
    print(f"{'TICKER':8} {'GF_INDUSTRY':34} -> DAMODARAN_INDUSTRY  [method]")
    print("-" * 90)
    for t in tickers:
        res = get_industry(t)
        gf = res.get("gf_industry") or "(none)"
        dam = res.get("damodaran_industry") or "NULL"
        method = res.get("match_method")
        line = f"{t:8} {gf:34} -> {dam}  [{method}]"
        if res.get("error"):
            line += f"  ERROR: {res['error']}"
        print(line)
    print()
    # Also dump the AAPL response object so the JSON shape is visible.
    print(json.dumps(get_industry(tickers[0]), indent=2, ensure_ascii=False))

"""
Indian Ticker / Name / ISIN → BSE scrip code Resolver
=====================================================

The Indian equivalent of br_resolve.py. BSE (the Bombay Stock Exchange — whose
public API exposes company annual reports) identifies every listed company by a
numeric **scrip code** (e.g. 500325 for Reliance Industries). NSE/BSE tickers
(RELIANCE, TCS), ISINs (INE002A01018) and company names are NOT the API key, so
this module bridges "whatever the frontend collected" to the BSE scrip code.

Resolution (first hit wins):
    1. Numeric BSE scrip code (6 digits)  → used directly.
    2. ISIN (INExxxxxxxxxx)                → exact ISIN match.
    3. Ticker / scrip_id (e.g. RELIANCE)   → exact scrip_id match.
    4. Company name                        → name-contains match (Active first).

Backed by BSE's public scrip master (ListofScripData), disk-cached daily like
CVM's cadastral CSV — no API key required.

Public API:
    resolve_company_number(ticker, company_name=None)
        -> {company_number(scrip code), isin, ticker, title, matched_via, candidates}
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

import requests

_MASTER_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
_CACHE_FILE = Path(__file__).parent / ".cache" / "bse_scrip_master.json"
_CACHE_MAX_AGE = 24 * 3600          # refresh the scrip master at most daily
_HTTP_TIMEOUT = 90
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OptionsExtractor",
    "Accept": "application/json",
    "Referer": "https://www.bseindia.com/",
}

_CACHE: Optional[list[dict[str, Any]]] = None   # in-process parsed scrip rows


def _load_master() -> list[dict[str, Any]]:
    """Fetch + parse BSE's scrip master, with a daily disk cache."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    text: Optional[str] = None
    if _CACHE_FILE.exists() and (time.time() - _CACHE_FILE.stat().st_mtime) < _CACHE_MAX_AGE:
        text = _CACHE_FILE.read_text(encoding="utf-8")
    else:
        try:
            resp = requests.get(_MASTER_URL, headers=_HEADERS, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            text = resp.text
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(text, encoding="utf-8")
        except Exception:
            if _CACHE_FILE.exists():           # serve a stale cache rather than fail
                text = _CACHE_FILE.read_text(encoding="utf-8")
            else:
                raise

    rows = json.loads(text)
    _CACHE = rows if isinstance(rows, list) else []
    return _CACHE


def _as_result(row: dict[str, Any], matched_via: str) -> dict[str, Any]:
    code = str(row.get("SCRIP_CD") or "").strip()
    name = (row.get("Scrip_Name") or "").strip()
    return {
        "company_number": code,                 # BSE scrip code (used by in_fetch)
        "isin": (row.get("ISIN_NUMBER") or "").strip(),
        "ticker": (row.get("scrip_id") or "").strip(),
        "title": name,
        "matched_via": matched_via,
        "candidates": [
            {"number": code, "ticker": (row.get("scrip_id") or "").strip(), "title": name}
        ],
    }


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve an Indian listing to its BSE scrip code.

    Returns:
        {
          "company_number": "500325",          # BSE scrip code
          "isin": "INE002A01018",
          "ticker": "RELIANCE",
          "title": "Reliance Industries Ltd",
          "matched_via": "scrip_code" | "isin" | "ticker" | "company_name",
          "candidates": [ {number, ticker, title}, ... ]
        }

    Raises LookupError if nothing usable is found.
    """
    ticker = (ticker or "").strip()
    company_name = (company_name or "").strip()
    if not ticker and not company_name:
        raise LookupError("No ticker, company name or ISIN supplied to resolve.")

    rows = _load_master()

    # 1) Numeric BSE scrip code (6 digits).
    if ticker.isdigit() and len(ticker) >= 5:
        for r in rows:
            if str(r.get("SCRIP_CD")).strip() == ticker:
                return _as_result(r, "scrip_code")

    # 2) ISIN (e.g. INE002A01018).
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", ticker.upper()):
        want = ticker.upper()
        for r in rows:
            if (r.get("ISIN_NUMBER") or "").strip().upper() == want:
                return _as_result(r, "isin")

    # 3) Ticker / scrip_id (exact, case-insensitive).
    if ticker:
        want = ticker.upper()
        for r in rows:
            if (r.get("scrip_id") or "").strip().upper() == want:
                return _as_result(r, "ticker")

    # 4) Company name contains (Active companies, then shortest name first).
    query = (company_name or ticker).upper()
    if query:
        hits = [r for r in rows if query in (r.get("Scrip_Name") or "").upper()]
        active = [r for r in hits if (r.get("Status") or "").upper() == "ACTIVE"]
        pool = active or hits
        if pool:
            pool.sort(key=lambda r: len(r.get("Scrip_Name") or ""))
            return _as_result(pool[0], "company_name" if company_name else "ticker")

    raise LookupError(
        f"No BSE (India) company found for {ticker or company_name!r}. "
        f"Try the BSE scrip code, the NSE/BSE ticker, the ISIN, or the company name."
    )


if __name__ == "__main__":
    # Manual smoke test:  python in_resolve.py RELIANCE   (no API key needed)
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_company_number(t, name), ensure_ascii=False, indent=2))

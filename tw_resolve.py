"""
Taiwan Ticker / Name → TWSE stock code Resolver
===============================================

The Taiwanese equivalent of kr_resolve.py / jp_resolve.py. Taiwan's listed
companies are identified by a **4-digit stock code** (公司代號, e.g. 2330 for
TSMC) on the Taiwan Stock Exchange (TWSE). The disclosure system is MOPS
(Market Observation Post System), which keys everything off that code.

This module bridges "whatever the frontend collected" (a stock code, an English
or Chinese name) to the stock code, using TWSE's free open-data listed-company
registry (openapi.twse.com.tw) — no API key required.

Resolution strategy (first hit wins):
    1. 4-digit numeric code  → exact stock-code lookup.
    2. Name (EN or ZH)       → match on Chinese name / short name / English short.

Public API:
    resolve_company_number(ticker, company_name=None)
        -> {company_number, name_en, title, matched_via, candidates}
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import requests

# TWSE-listed (上市) company basic info. (OTC/TPEx-listed companies are not in
# this dataset; resolution there would need the TPEx open-data feed.)
_LIST_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
_CACHE_FILE = Path(__file__).parent / ".cache" / "twse_listed.json"
_CACHE_MAX_AGE = 24 * 3600
_HTTP_TIMEOUT = 60
_UA = {"User-Agent": "Mozilla/5.0 (OptionsExtractor)"}

_CACHE: Optional[list[dict[str, Any]]] = None

_CODE = "公司代號"
_NAME = "公司名稱"
_SHORT = "公司簡稱"
_EN = "英文簡稱"


def _load_listed() -> list[dict[str, Any]]:
    """Fetch + parse TWSE's listed-company registry, with a daily disk cache."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    data: Optional[list[dict[str, Any]]] = None
    if _CACHE_FILE.exists() and (time.time() - _CACHE_FILE.stat().st_mtime) < _CACHE_MAX_AGE:
        try:
            data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = None
    if data is None:
        try:
            resp = requests.get(_LIST_URL, timeout=_HTTP_TIMEOUT, headers=_UA)
            resp.raise_for_status()
            data = resp.json()
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False),
                                   encoding="utf-8")
        except Exception:
            if _CACHE_FILE.exists():           # serve stale cache rather than fail
                data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            else:
                raise

    _CACHE = data
    return data


def _normalize_code(ticker: str) -> Optional[str]:
    """A TWSE stock code is 4 digits (TSMC 2330). 'TSMC' → None; '2330.TW' → '2330'."""
    digits = "".join(ch for ch in str(ticker or "") if ch.isdigit())
    return digits if 4 <= len(digits) <= 6 else None


def _as_result(row: dict[str, Any], matched_via: str) -> dict[str, Any]:
    code = str(row.get(_CODE) or "").strip()
    name = (row.get(_SHORT) or row.get(_NAME) or code).strip()
    return {
        "company_number": code,                 # TWSE stock code (used by tw_fetch)
        "name_en": (row.get(_EN) or "").strip() or None,
        "title": name,
        "matched_via": matched_via,
        "candidates": [
            {"number": code, "title": name, "name_en": (row.get(_EN) or "").strip() or None}
        ],
    }


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a Taiwanese listing to its TWSE stock code.

    Returns:
        {
          "company_number": "2330",
          "name_en": "TSMC",
          "title": "台積電",
          "matched_via": "stock_code" | "company_name" | "ticker",
          "candidates": [ {number, title, name_en}, ... ]
        }

    Raises LookupError if nothing usable is found.
    """
    ticker = (ticker or "").strip()
    company_name = (company_name or "").strip()
    if not ticker and not company_name:
        raise LookupError("No ticker or company name supplied to resolve.")

    rows = _load_listed()

    # 1) Numeric stock code → exact lookup.
    code = _normalize_code(ticker)
    if code:
        for r in rows:
            if str(r.get(_CODE) or "").strip() == code:
                return _as_result(r, "stock_code")

    # 2) Name match (Chinese name / short name / English short name).
    query = (company_name or ticker).strip()
    q = query.upper()
    if q:
        matches = [
            r for r in rows
            if q in str(r.get(_NAME) or "").upper()
            or q in str(r.get(_SHORT) or "").upper()
            or q in str(r.get(_EN) or "").upper()
        ]
        if matches:
            return _as_result(matches[0],
                              "company_name" if company_name else "ticker")

    raise LookupError(
        f"No TWSE (Taiwan) company found for {ticker or company_name!r}. "
        f"Try the 4-digit stock code (e.g. 2330) or the company's name."
    )


if __name__ == "__main__":
    # Manual smoke test:  python tw_resolve.py 2330   (no API key needed)
    import sys

    t = sys.argv[1] if len(sys.argv) > 1 else "2330"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_company_number(t, name), ensure_ascii=False, indent=2))

"""
Korean Ticker / Name → DART corp_code Resolver
==============================================

The Korean equivalent of dk_resolve.py / jp_resolve.py. South Korea's DART
(Data Analysis, Retrieval and Transfer system, run by the FSS) identifies every
filer by an 8-digit **corp_code** (e.g. 00126380 for Samsung Electronics).
Listed companies also carry a 6-digit **KRX stock code** (e.g. Samsung 005930).

This module bridges "whatever the frontend collected" (a KRX ticker, optionally
a company name) to the authoritative corp_code, using the dart-fss library's
corp list (built from OpenDART's corpCode.xml).

NOTE — unlike Japan's EDINET code list (a public download), DART's corp list is
served through the keyed OpenDART API, so resolution here REQUIRES a valid
DART_API_KEY. There is no keyless path for Korea.

Resolution strategy (first hit wins):
    1. Numeric ticker (e.g. "005930")  → exact KRX stock-code lookup.
    2. Explicit company name           → name match (listed companies first).
    3. Raw ticker string               → name match (last resort).

Public API:
    resolve_company_number(ticker, company_name=None)
        -> {company_number, stock_code, title, matched_via, candidates}
"""

from __future__ import annotations

import os
from typing import Any, Optional


def _api_key() -> str:
    key = os.environ.get("DART_API_KEY")
    if not key or key == "your_dart_key_here":
        raise RuntimeError(
            "DART_API_KEY not set in .env — required for OpenDART (Korea). "
            "Get a free key instantly at https://opendart.fss.or.kr (API registration)."
        )
    return key


def _normalize_stock(ticker: str) -> Optional[str]:
    """Normalize a KRX ticker to the 6-digit stock code dart-fss expects.

    "5930" → "005930";  "005930" → "005930";  "005930.KS" → "005930";
    non-numeric → None.
    """
    digits = "".join(ch for ch in str(ticker or "") if ch.isdigit())
    if not digits or len(digits) > 6:
        return None
    return digits.zfill(6)


def _as_result(corp, matched_via: str) -> dict[str, Any]:
    corp_code = getattr(corp, "corp_code", None)
    stock_code = getattr(corp, "stock_code", None) or None
    name = getattr(corp, "corp_name", None) or corp_code
    return {
        "company_number": corp_code,        # DART corp_code (used by kr_fetch)
        "stock_code": stock_code,
        "title": name,
        "matched_via": matched_via,
        "candidates": [
            {"number": corp_code, "title": name, "stock_code": stock_code}
        ],
    }


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a Korean listing to its DART corp_code.

    Returns:
        {
          "company_number": "00126380",   # DART corp_code
          "stock_code": "005930",
          "title": "삼성전자",
          "matched_via": "stock_code" | "company_name" | "ticker",
          "candidates": [ {number, title, stock_code}, ... ]
        }

    Raises LookupError if nothing usable is found, RuntimeError if no key.
    """
    ticker = (ticker or "").strip().upper()
    company_name = (company_name or "").strip()
    if not ticker and not company_name:
        raise LookupError("No ticker or company name supplied to resolve.")

    import dart_fss as dart

    dart.enable_spinner(False)   # avoid yaspin spinner crashing on non-UTF8 consoles
    dart.set_api_key(_api_key())
    corp_list = dart.get_corp_list()

    # 1) Numeric ticker → exact KRX stock-code lookup.
    sc = _normalize_stock(ticker)
    if sc:
        corp = corp_list.find_by_stock_code(sc)
        if corp is not None:
            return _as_result(corp, "stock_code")

    # 2) Company name → name match; prefer listed companies (have a stock code).
    name_query = company_name or ticker
    if name_query:
        matches = corp_list.find_by_corp_name(name_query, exactly=False) or []
        listed = [c for c in matches if getattr(c, "stock_code", None)]
        chosen = listed or matches
        if chosen:
            return _as_result(chosen[0], "company_name" if company_name else "ticker")

    raise LookupError(
        f"No DART (Korea) company found for {ticker or company_name!r}. "
        f"Try the 6-digit KRX code (e.g. 005930) or the registered company name."
    )


if __name__ == "__main__":
    # Manual smoke test:  python kr_resolve.py 005930   (needs DART_API_KEY)
    import json
    import sys

    t = sys.argv[1] if len(sys.argv) > 1 else "005930"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_company_number(t, name), ensure_ascii=False, indent=2))

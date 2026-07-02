"""
Chinese Ticker / Name → CNINFO orgId Resolver
==============================================

The Chinese equivalent of kr_resolve.py / tw_resolve.py. CNINFO
(www.cninfo.com.cn — the official disclosure portal for the Shanghai, Shenzhen
and Beijing exchanges, the "EDGAR of China") identifies every listed company by
a 6-digit **stock code** (e.g. 600519 for Kweichow Moutai) AND an internal
**orgId** (e.g. gssh0600519). Annual-report search needs the orgId, so this
module bridges "whatever the frontend collected" (a stock code or a company
name, Chinese or pinyin) to {code, orgId, plate, column} via CNINFO's public
`topSearch` autocomplete endpoint — no API key required.

Resolution:
    1. 6-digit stock code  → exact code match among topSearch hits.
    2. Company name (中文 or pinyin) → first topSearch hit.

The exchange "plate"/"column" needed by the announcement query is derived from
the code prefix:  6→sh/sse, 0/3→sz/szse, 4/8/9→bj/bj (Beijing Stock Exchange).

Public API:
    resolve_company_number(ticker, company_name=None)
        -> {company_number(code), org_id, plate, column, title, matched_via, candidates}
"""

from __future__ import annotations

from typing import Any, Optional

import requests

_TOPSEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
_HTTP_TIMEOUT = 40
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OptionsExtractor",
    "Accept": "*/*",
    "Referer": "http://www.cninfo.com.cn/",
    "X-Requested-With": "XMLHttpRequest",
}


def _plate_column(code: str) -> tuple[str, str]:
    """Map a 6-digit code prefix to CNINFO's (plate, column) query parameters."""
    c = (code or "").strip()
    if c[:1] == "6":
        return "sh", "sse"          # Shanghai Stock Exchange
    if c[:1] in ("0", "3"):
        return "sz", "szse"         # Shenzhen Stock Exchange (main + ChiNext)
    if c[:1] in ("4", "8", "9"):
        return "bj", "bj"           # Beijing Stock Exchange / NEEQ
    return "sz", "szse"             # sensible default


def _topsearch(keyword: str) -> list[dict[str, Any]]:
    """Call CNINFO autocomplete; returns [{code, orgId, zwjc, pinyin, category}, ...]."""
    q = (keyword or "").strip()
    if not q:
        return []
    resp = requests.post(
        _TOPSEARCH_URL,
        data={"keyWord": q, "maxNum": "20"},
        headers=_HEADERS,
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        return []
    try:
        rows = resp.json()
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def lookup_org_id(code: str) -> Optional[str]:
    """Return the CNINFO orgId for a 6-digit stock code (used by cn_fetch so the
    fetcher stays self-contained, mirroring tw_fetch). None if not found."""
    code = (code or "").strip()
    for r in _topsearch(code):
        if str(r.get("code")) == code and r.get("orgId"):
            return str(r.get("orgId"))
    rows = _topsearch(code)
    return str(rows[0].get("orgId")) if rows and rows[0].get("orgId") else None


def _as_result(row: dict[str, Any], matched_via: str,
               candidates: list[dict[str, Any]]) -> dict[str, Any]:
    code = str(row.get("code") or "")
    plate, column = _plate_column(code)
    return {
        "company_number": code,                 # 6-digit stock code (used by cn_fetch)
        "org_id": str(row.get("orgId") or ""),
        "plate": plate,
        "column": column,
        "title": row.get("zwjc") or code,       # 中文简称 (short Chinese name)
        "matched_via": matched_via,
        "candidates": candidates,
    }


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a Chinese listing to its CNINFO orgId + exchange plate/column.

    Returns:
        {
          "company_number": "600519",           # 6-digit stock code
          "org_id": "gssh0600519",
          "plate": "sh", "column": "sse",
          "title": "贵州茅台",
          "matched_via": "code" | "company_name",
          "candidates": [ {number, org_id, title}, ... ]
        }

    Raises LookupError if nothing usable is found.
    """
    ticker = (ticker or "").strip()
    company_name = (company_name or "").strip()
    if not ticker and not company_name:
        raise LookupError("No stock code or company name supplied to resolve.")

    is_code = ticker.isdigit()
    query = ticker or company_name
    rows = _topsearch(query)
    if not rows and company_name:
        rows = _topsearch(company_name)
    if not rows:
        raise LookupError(
            f"No CNINFO (China) listing found for {ticker or company_name!r}. "
            f"Try the 6-digit stock code (e.g. 600519) or the registered name."
        )

    candidates = [
        {"number": str(r.get("code")), "org_id": str(r.get("orgId")),
         "title": r.get("zwjc")}
        for r in rows[:10]
    ]

    chosen = None
    matched_via = "company_name"
    if is_code:
        for r in rows:
            if str(r.get("code")) == ticker:
                chosen, matched_via = r, "code"
                break
    if chosen is None:
        chosen = rows[0]
        matched_via = "code" if is_code else "company_name"

    return _as_result(chosen, matched_via, candidates)


if __name__ == "__main__":
    # Manual smoke test:  python cn_resolve.py 600519   (no API key needed)
    import json
    import sys

    t = sys.argv[1] if len(sys.argv) > 1 else "600519"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_company_number(t, name), ensure_ascii=False, indent=2))

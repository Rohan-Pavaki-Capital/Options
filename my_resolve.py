"""
Malaysia (Bursa Malaysia) Ticker / Name -> stock-code Resolver
==============================================================

The Malaysian equivalent of in_resolve.py. Bursa Malaysia's public
announcements API keys every listed company by its numeric **stock code**
(e.g. 1155 = Malayan Banking Berhad / "MAYBANK"). The annual-report fetch
(`my_fetch`) is driven by this stock code, so this module bridges "whatever the
analyst typed" — a stock code, a Bursa short-name ticker, or a company name —
to it.

Resolution (first hit wins):
    1. Numeric stock code (4-5 digits)        -> used directly (universal path).
    2. Curated short-name ticker (MAYBANK..)  -> verified major-issuer map.
    3. Company name / unknown ticker          -> Bursa announcement keyword
       search; tally the stock codes behind the result rows' company links and
       fuzzy-match the linked company name to the query (rapidfuzz).

WHY NO MASTER LIST: Bursa's listing-directory rows load via JS (no static table)
and the dedicated company-search endpoints 404. The announcement search,
however, IS reachable over plain HTTP and every result row links to a
`company-profile?stock_code=<code>` with the issuer's full name — enough to
resolve a name without a master. Major tickers use the curated map for
precision; the numeric stock code works for any issuer.

Public API:
    resolve_company_number(ticker, company_name=None)
        -> {company_number(stock code), ticker, title, matched_via, candidates}
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import quote

from curl_cffi import requests as creq

try:
    from rapidfuzz import fuzz
    _HAVE_FUZZ = True
except Exception:                                   # pragma: no cover
    _HAVE_FUZZ = False

_SEARCH_URL = ("https://www.bursamalaysia.com/api/v1/announcements/search"
               "?ann_type=company&keyword={kw}&per_page=50&page=1&sort_dir=desc")
_HTTP_TIMEOUT = 30

# Verified short-name ticker -> Bursa stock code (the alphabetic ticker is NOT
# the API key; the 4-digit stock code is). A small map of major issuers for
# precision; the numeric-code path covers everything else.
_TICKER_MAP: dict[str, str] = {
    "maybank": "1155", "pbbank": "1295", "publicbank": "1295", "cimb": "1023",
    "tenaga": "5347", "tnb": "5347", "pchem": "5183", "petgas": "6033",
    "ihh": "5225", "axiata": "6888", "maxis": "6012", "genting": "3182",
    "genm": "4715", "topglov": "7113", "harta": "5168", "nestle": "4707",
    "misc": "3816", "petdag": "5681", "rhbbank": "1066", "hlbank": "5819",
    "cdb": "6947", "digi": "6947", "pmetal": "8869", "sime": "4197",
    "simeplt": "5285", "klk": "2445", "ioicorp": "1961", "ppb": "4065",
    "mrdiy": "5296", "ql": "7084", "ytlpowr": "6742", "ytl": "4677",
}

# Curated name token -> stock code, for clean company-name input on majors.
_NAME_HINTS: dict[str, str] = {
    "malayan banking": "1155", "maybank": "1155",
    "public bank": "1295", "cimb": "1023",
    "tenaga nasional": "5347", "petronas chemicals": "5183",
    "petronas gas": "6033", "ihh healthcare": "5225", "axiata": "6888",
    "maxis": "6012", "genting": "3182", "nestle": "4707",
    "sime darby": "4197", "press metal": "8869", "rhb bank": "1066",
    "hong leong bank": "5819", "mr d.i.y": "5296", "mr diy": "5296",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _search_codes(query: str) -> list[tuple[str, str]]:
    """Search Bursa announcements for `query` and return (stock_code, company
    name) pairs harvested from the result rows' company-profile links, in
    first-seen order. Empty list on any failure."""
    try:
        url = _SEARCH_URL.format(kw=quote(query))
        r = creq.get(url, impersonate="chrome", timeout=_HTTP_TIMEOUT)
        rows = (r.json() or {}).get("data") or []
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        cell = row[2] if len(row) > 2 and row[2] else ""
        m = re.search(r"stock_code=(\w+)[^>]*>\s*([^<]+?)\s*<", cell)
        if not m:
            continue
        code, name = m.group(1).strip(), m.group(2).strip()
        if code.isdigit() and code not in seen:
            seen.add(code)
            out.append((code, name))
    return out


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a Bursa Malaysia listing to its numeric stock code.

    Accepts (priority order): a numeric stock code verbatim, a curated
    short-name ticker, or a company name / unknown ticker (resolved via Bursa's
    announcement search + fuzzy name match).

    Returns {company_number, ticker, title, matched_via, candidates}.
    Raises LookupError with guidance if nothing matches.
    """
    ticker = (ticker or "").strip()
    company_name = (company_name or "").strip()
    if not ticker and not company_name:
        raise LookupError("No stock code, ticker or company name supplied to resolve.")

    # 1) numeric stock code verbatim (universal path).
    for cand in (ticker, company_name):
        if cand.isdigit() and 3 <= len(cand) <= 5:
            return {"company_number": cand, "ticker": ticker, "title": company_name or ticker,
                    "matched_via": "stock_code", "candidates": []}

    # 2) curated short-name ticker.
    t = _norm(ticker).replace(".kl", "").replace(" ", "")
    if t in _TICKER_MAP:
        return {"company_number": _TICKER_MAP[t], "ticker": ticker,
                "title": company_name or ticker, "matched_via": "ticker", "candidates": []}

    # 3) curated name hint (contains-match).
    for q in (_norm(company_name), _norm(ticker)):
        for hint, code in _NAME_HINTS.items():
            if q and hint in q:
                return {"company_number": code, "ticker": ticker,
                        "title": company_name or ticker,
                        "matched_via": "company_name", "candidates": []}

    # 4) live announcement search + fuzzy name match.
    query = company_name or ticker
    pairs = _search_codes(query)
    if pairs:
        if _HAVE_FUZZ:
            ql = query.lower()
            pairs_scored = sorted(
                pairs, key=lambda p: fuzz.token_set_ratio(ql, p[1].lower()), reverse=True
            )
            best_code, best_name = pairs_scored[0]
            candidates = [{"number": c, "title": n} for c, n in pairs_scored[:6]]
        else:
            best_code, best_name = pairs[0]
            candidates = [{"number": c, "title": n} for c, n in pairs[:6]]
        return {"company_number": best_code, "ticker": ticker, "title": best_name,
                "matched_via": "company_name", "candidates": candidates}

    raise LookupError(
        f"Could not resolve {ticker or company_name!r} to a Bursa Malaysia stock "
        f"code. Supply the 4-digit stock code (shown in the bursamalaysia.com "
        f"company-profile URL) or a clearer company name."
    )


if __name__ == "__main__":
    import json
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "MAYBANK"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_company_number(t, name), ensure_ascii=False, indent=2))

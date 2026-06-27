"""
UK Ticker / Name → Companies House Number Resolver
===================================================

SEC EDGAR identifies companies by ticker and resolves it to a CIK internally.
Companies House has NO concept of a stock ticker — it identifies every entity
by an 8-character **company number** (e.g. "00445790" for Tesco PLC).

This module bridges that gap. Given whatever the frontend collected (a ticker,
optionally a company name), it returns the best-matching Companies House
company number, using the official free Search API:

    GET https://api.company-information.service.gov.uk/search/companies?q=...

Resolution strategy (first hit wins):
    1. Explicit company name supplied by the caller  → search.
    2. Ticker found in TICKER_NAME_HINTS            → search by the hinted name.
    3. Raw ticker string                            → search (last resort).

We deliberately do NOT hard-code company *numbers* (they go stale / can be
mistyped); we only keep ticker→*name* hints and let the official API return the
authoritative number. Among the search hits we prefer active companies whose
type looks like a listed issuer (plc).

Public API:
    resolve_company_number(ticker, company_name=None, api_key=None)
        -> {company_number, title, matched_via, candidates}
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests


def _api_base() -> str:
    """Companies House REST API base host.

    Defaults to the LIVE host. Override with COMPANIES_HOUSE_API_BASE
    (e.g. https://api-sandbox.company-information.service.gov.uk) for testing.
    NOTE: a *sandbox* API key only works against the sandbox host and the
    sandbox contains synthetic data only — real filings require a LIVE key.
    """
    return (
        os.environ.get("COMPANIES_HOUSE_API_BASE")
        or "https://api.company-information.service.gov.uk"
    ).rstrip("/")


_TIMEOUT = 30


# ── Common LSE ticker → company *name* hints ─────────────────────────
# These are search hints only (NOT company numbers). They help when the user
# types a bare ticker like "TSCO" that the full-text search would not match on
# its own. The official API still returns the authoritative company number.
TICKER_NAME_HINTS: dict[str, str] = {
    "TSCO": "Tesco PLC",
    "BP": "BP p.l.c.",
    "VOD": "Vodafone Group",
    "HSBA": "HSBC Holdings",
    "BARC": "Barclays PLC",
    "LLOY": "Lloyds Banking Group",
    "NWG": "NatWest Group",
    "GSK": "GSK plc",
    "AZN": "AstraZeneca PLC",
    "ULVR": "Unilever PLC",
    "DGE": "Diageo plc",
    "RR": "Rolls-Royce Holdings",
    "BT-A": "BT Group plc",
    "BT": "BT Group plc",
    "SHEL": "Shell plc",
    "RIO": "Rio Tinto plc",
    "AAL": "Anglo American",
    "NG": "National Grid plc",
    "SSE": "SSE plc",
    "PRU": "Prudential plc",
    "AV": "Aviva plc",
    "LGEN": "Legal & General Group",
    "SBRY": "Sainsbury",
    "MKS": "Marks and Spencer Group",
    "NXT": "Next plc",
    "BRBY": "Burberry Group",
    "RKT": "Reckitt Benckiser Group",
    "STAN": "Standard Chartered PLC",
    "BA": "BAE Systems",
    "IMB": "Imperial Brands",
    "BATS": "British American Tobacco",
    "CPG": "Compass Group",
    "REL": "RELX PLC",
    "EXPN": "Experian plc",
    "WTB": "Whitbread PLC",
    "ITV": "ITV plc",
    "JD": "JD Sports Fashion",
    "OCDO": "Ocado Group",
    "ABF": "Associated British Foods",
}


def _api_key(explicit: Optional[str]) -> str:
    """Resolve the Companies House API key from arg or environment.

    Tolerates the stray whitespace/quotes that can appear in .env values.
    """
    key = (
        explicit
        or os.environ.get("COMPANIES_HOUSE_API_KEY")
        or os.environ.get("Companies_House_API")
        or ""
    )
    return key.strip().strip('"').strip("'").strip()


def _score(item: dict, query: str) -> int:
    """Rank a search hit: prefer active listed (plc) companies whose title
    matches the query well."""
    score = 0
    title = (item.get("title") or "").upper()
    q = query.upper()

    if item.get("company_status") == "active":
        score += 5
    ctype = (item.get("company_type") or "").lower()
    if ctype == "plc":
        score += 4  # public limited company == the listed issuer we want

    if q and q in title:
        score += 3
    if title.startswith(q):
        score += 2
    if "PLC" in title or "P.L.C" in title:
        score += 1

    return score


def _search(query: str, key: str, items: int = 20) -> list[dict]:
    resp = requests.get(
        f"{_api_base()}/search/companies",
        params={"q": query, "items_per_page": items},
        auth=(key, ""),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("items", []) or []


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a UK listing to its Companies House company number.

    Returns a dict:
        {
          "company_number": "00445790",
          "title": "TESCO PLC",
          "matched_via": "company_name" | "ticker_hint" | "ticker",
          "candidates": [ {number,title,status,type}, ... ]   # top few, for debug
        }

    Raises LookupError if nothing usable is found.
    """
    key = _api_key(api_key)
    if not key:
        raise RuntimeError(
            "COMPANIES_HOUSE_API_KEY is not set. Add it to your .env file."
        )

    ticker = (ticker or "").strip().upper()
    company_name = (company_name or "").strip()

    # Build an ordered list of (query, matched_via) attempts.
    attempts: list[tuple[str, str]] = []
    if company_name:
        attempts.append((company_name, "company_name"))
    hint = TICKER_NAME_HINTS.get(ticker)
    if hint:
        attempts.append((hint, "ticker_hint"))
    if ticker:
        attempts.append((ticker, "ticker"))

    if not attempts:
        raise LookupError("No ticker or company name supplied to resolve.")

    last_error: Optional[Exception] = None
    for query, matched_via in attempts:
        try:
            items = _search(query, key)
        except Exception as e:  # network / auth — try next, remember error
            last_error = e
            continue

        if not items:
            continue

        ranked = sorted(items, key=lambda it: _score(it, query), reverse=True)
        best = ranked[0]
        number = best.get("company_number")
        if not number:
            continue

        candidates = [
            {
                "number": it.get("company_number"),
                "title": it.get("title"),
                "status": it.get("company_status"),
                "type": it.get("company_type"),
            }
            for it in ranked[:5]
        ]
        return {
            "company_number": number,
            "title": best.get("title"),
            "matched_via": matched_via,
            "candidates": candidates,
        }

    if last_error is not None:
        raise LookupError(
            f"Companies House search failed for {ticker or company_name!r}: {last_error}"
        )
    raise LookupError(
        f"No Companies House company found for {ticker or company_name!r}. "
        f"Try supplying the full registered company name."
    )


if __name__ == "__main__":
    # Manual smoke test:  python uk_resolve.py TSCO
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "TSCO"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(resolve_company_number(t, name))

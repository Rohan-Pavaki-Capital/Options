"""
Danish Ticker / Name → CVR Number Resolver
===========================================

The Danish equivalent of uk_resolve.py. Companies House identifies UK firms by
an 8-char company number; Denmark's Central Business Register (CVR) identifies
every entity by an 8-digit **CVR number** (e.g. 24256790 for Novo Nordisk A/S).

This module bridges "whatever the frontend collected" (a Nasdaq Copenhagen
ticker, optionally a company name) to the authoritative CVR number, using the
free, no-auth cvrapi.dk lookup service.

Resolution strategy (first hit wins):
    1. Explicit company name supplied by the caller  → search.
    2. Ticker found in TICKER_NAME_HINTS            → search by the hinted name.
    3. Raw ticker string                            → search (last resort).

Public API:
    resolve_company_number(ticker, company_name=None)
        -> {company_number, title, matched_via, candidates}
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests


_TIMEOUT = 30


def _api_base() -> str:
    """cvrapi.dk base. Free, no key. Override with CVRAPI_BASE if needed."""
    return (os.environ.get("CVRAPI_BASE") or "https://cvrapi.dk/api").rstrip("/")


def _user_agent() -> str:
    # cvrapi.dk asks callers to identify themselves with a descriptive UA.
    return os.environ.get(
        "CVRAPI_USER_AGENT",
        "Pavaki Options Extractor (contact@pavaki.local)",
    )


# ── Common Nasdaq Copenhagen ticker → company *name* hints ───────────
# Search hints only (NOT CVR numbers). They help when the user types a bare
# ticker like "NOVO-B" that the name search would not match on its own. The
# official lookup still returns the authoritative CVR number.
TICKER_NAME_HINTS: dict[str, str] = {
    "NOVO-B": "Novo Nordisk",
    "NOVO-A": "Novo Nordisk",
    "NOVO": "Novo Nordisk",
    "MAERSK-B": "A.P. Møller - Mærsk",
    "MAERSK-A": "A.P. Møller - Mærsk",
    "ORSTED": "Ørsted",
    "DSV": "DSV",
    "VWS": "Vestas Wind Systems",
    "CARL-B": "Carlsberg",
    "CARL-A": "Carlsberg",
    "COLO-B": "Coloplast",
    "GN": "GN Store Nord",
    "DEMANT": "Demant",
    "TRYG": "Tryg",
    "PNDORA": "Pandora",
    "GMAB": "Genmab",
    "ROCK-B": "Rockwool",
    "DANSKE": "Danske Bank",
    "NDA-DK": "Nordea Bank",
    "AMBU-B": "Ambu",
    "ISS": "ISS A/S",
    "FLS": "FLSmidth",
    "NETC": "Netcompany Group",
    "NZYM-B": "Novonesis",
    "NSIS-B": "Novonesis",
    "JYSK": "Jyske Bank",
    "SYDB": "Sydbank",
    "BAVA": "Bavarian Nordic",
    "ZEAL": "Zealand Pharma",
}


def _lookup(query: str) -> Optional[dict]:
    """Single cvrapi.dk lookup. Returns the match dict or None."""
    resp = requests.get(
        _api_base(),
        params={"search": query, "country": "dk"},
        headers={"User-Agent": _user_agent()},
        timeout=_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    # cvrapi returns {"error": "...", ...} when nothing is found.
    if not isinstance(data, dict) or data.get("error") or not data.get("vat"):
        return None
    return data


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a Danish listing to its CVR number.

    Returns:
        {
          "company_number": "24256790",   # CVR
          "title": "NOVO NORDISK A/S",
          "matched_via": "company_name" | "ticker_hint" | "ticker",
          "candidates": [ {number, title}, ... ]
        }

    Raises LookupError if nothing usable is found.
    """
    ticker = (ticker or "").strip().upper()
    company_name = (company_name or "").strip()

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
            match = _lookup(query)
        except Exception as e:  # network / rate-limit — try next, remember error
            last_error = e
            continue
        if not match:
            continue

        cvr = str(match.get("vat"))
        return {
            "company_number": cvr,
            "title": match.get("name") or query,
            "matched_via": matched_via,
            "candidates": [{"number": cvr, "title": match.get("name")}],
        }

    if last_error is not None:
        raise LookupError(
            f"CVR lookup failed for {ticker or company_name!r}: {last_error}"
        )
    raise LookupError(
        f"No Danish (CVR) company found for {ticker or company_name!r}. "
        f"Try supplying the full registered company name."
    )


if __name__ == "__main__":
    # Manual smoke test:  python dk_resolve.py NOVO-B
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "NOVO-B"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(resolve_company_number(t, name))

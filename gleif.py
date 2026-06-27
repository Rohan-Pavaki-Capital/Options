"""
GLEIF lookup helper  —  ISIN / name → LEI
==========================================

Thin client over the free, public GLEIF API (https://api.gleif.org, no key) used
by eu_resolve.py to broaden how an EU/EEA listing can be identified.

GLEIF is the global authority for the Legal Entity Identifier (LEI). It maps:
  * ISIN  → LEI   (via the GLEIF↔ISIN mapping; reliable, exact)
  * legal name → LEI   (fuzzy / exact text search)

NOTE: GLEIF does NOT map exchange TICKERS to LEIs — no public registry does, as
LEI reference data carries no ticker. Ticker handling therefore stays in
eu_resolve's small convenience map + filings.xbrl.org name search. This module
covers the ISIN and name paths only.

Public API:
    isin_to_lei(isin)        -> (lei, legal_name) | None
    name_to_lei(name)        -> (lei, legal_name) | None
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Optional

_GLEIF_BASE = "https://api.gleif.org/api/v1"
_HTTP_TIMEOUT = 30
_HEADERS = {
    "Accept": "application/vnd.api+json",
    "User-Agent": "Mozilla/5.0 (OptionsExtractor; +https://gleif.org)",
}

# ISIN: 2-letter country prefix + 9 alphanumeric + 1 check digit.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def is_isin(s: str) -> bool:
    return bool(_ISIN_RE.match((s or "").strip().upper()))


def _get(path: str, params: dict) -> dict:
    url = f"{_GLEIF_BASE}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _first_lei(d: dict) -> Optional[tuple[str, str]]:
    rows = d.get("data") or []
    if not rows:
        return None
    rec = rows[0]
    lei = rec.get("id")
    name = lei
    try:
        name = rec["attributes"]["entity"]["legalName"]["name"] or lei
    except Exception:
        pass
    return (lei, name) if lei else None


def isin_to_lei(isin: str) -> Optional[tuple[str, str]]:
    """Resolve an ISIN to its (LEI, legal_name) via GLEIF, or None."""
    isin = (isin or "").strip().upper()
    if not is_isin(isin):
        return None
    try:
        return _first_lei(_get("lei-records", {"filter[isin]": isin, "page[size]": "1"}))
    except Exception:
        return None


def name_to_lei(name: str) -> Optional[tuple[str, str]]:
    """Resolve a legal name to its (LEI, legal_name) via GLEIF fulltext, or None."""
    name = (name or "").strip()
    if len(name) < 2:
        return None
    try:
        return _first_lei(_get("lei-records",
                               {"filter[entity.legalName]": name, "page[size]": "1"}))
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else "NL0010273215"
    fn = isin_to_lei if is_isin(arg) else name_to_lei
    print(fn(arg))

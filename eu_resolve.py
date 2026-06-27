"""
EU/EEA Ticker / Name / LEI → LEI Resolver  (ESEF, pan-European)
================================================================

The European equivalent of br_resolve.py / kr_resolve.py, but covering the
WHOLE EU/EEA in one module. Under the EU Transparency Directive every company
with securities on an EU-regulated market must file its Annual Financial Report
in ESEF (inline-XBRL) from FY2021 onward. XBRL International aggregates these
into one free, public repository — filings.xbrl.org — whose JSON:API keys every
issuer by its **LEI** (Legal Entity Identifier, 20 alphanumeric chars), NOT by a
stock ticker.

This module bridges "whatever the frontend collected" (a local ticker, a company
name, or an LEI) to the authoritative LEI, using the filings.xbrl.org JSON:API —
no API key required.

Resolution strategy (first hit wins):
    1. LEI (20 alphanumeric chars)  → verified directly against /api/entities.
    2. Local ticker (e.g. SAP, MC) → small ticker→name convenience map, then name search.
    3. Explicit company name        → JSON:API `ilike` name search.

Among name matches we prefer a candidate that actually HAS ESEF filings in the
repository (and, when a country hint is given, one whose latest filing is from
that country), so the downstream fetch does not dead-end on an entity with no
report. filings.xbrl.org coverage is partial today; the official superset (ESMA
ESAP) opens July 2027 — keep this adapter's surface stable so it can repoint there.

Public API:
    resolve_company_number(ticker, company_name=None, country=None)
        -> {company_number, title, country, matched_via, candidates}
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any, Optional

import gleif

_API_BASE = "https://filings.xbrl.org/api"
_HTTP_TIMEOUT = 60
_HEADERS = {
    "Accept": "application/vnd.api+json",
    "User-Agent": "Mozilla/5.0 (OptionsExtractor; +https://filings.xbrl.org)",
}

# Local tickers whose root does not obviously match the registered legal name.
# Keeps common lookups accurate; everything else falls back to name search.
# (Only companies actually present in ESEF/regulated EU markets are useful here.)
_TICKER_MAP = {
    # Germany (Xetra)
    "SAP": "SAP SE", "SIE": "SIEMENS", "ALV": "ALLIANZ", "BAS": "BASF",
    "BAYN": "BAYER", "BMW": "BAYERISCHE MOTOREN WERKE", "VOW3": "VOLKSWAGEN",
    "MBG": "MERCEDES-BENZ", "DTE": "DEUTSCHE TELEKOM", "DBK": "DEUTSCHE BANK",
    "ADS": "ADIDAS", "MUV2": "MUENCHENER RUECK", "IFX": "INFINEON",
    # France (Euronext Paris)
    "MC": "LVMH", "OR": "L'OREAL", "AIR": "AIRBUS", "TTE": "TOTALENERGIES",
    "SAN": "SANOFI", "BNP": "BNP PARIBAS", "SU": "SCHNEIDER ELECTRIC",
    "AI": "AIR LIQUIDE", "DG": "VINCI", "KER": "KERING",
    # Netherlands (Euronext Amsterdam)
    "ASML": "ASML", "HEIA": "HEINEKEN", "PHIA": "KONINKLIJKE PHILIPS",
    "INGA": "ING GROEP", "AD": "KONINKLIJKE AHOLD DELHAIZE", "PRX": "PROSUS",
    # Spain (BME)
    "SAN_ES": "BANCO SANTANDER", "IBE": "IBERDROLA", "ITX": "INDUSTRIA DE DISENO TEXTIL",
    "TEF": "TELEFONICA", "BBVA": "BANCO BILBAO VIZCAYA",
    # Italy (Borsa Italiana)
    "ENI": "ENI", "ISP": "INTESA SANPAOLO", "ENEL": "ENEL", "RACE": "FERRARI",
    "UCG": "UNICREDIT", "STLAM": "STELLANTIS",
    # Nordics / Ireland
    "NOVO-B": "NOVO NORDISK", "ERIC-B": "TELEFONAKTIEBOLAGET LM ERICSSON",
    "VOLV-B": "VOLVO", "NDA-SE": "NORDEA", "RYA": "RYANAIR", "CRH": "CRH",
}

_LEI_RE = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


def _api_get(path: str, params: Optional[dict] = None) -> dict[str, Any]:
    """GET a filings.xbrl.org JSON:API resource and return the parsed body."""
    url = f"{_API_BASE}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _is_lei(s: str) -> bool:
    return bool(_LEI_RE.match((s or "").strip().upper()))


def _entity_has_filings(lei: str) -> tuple[int, Optional[str]]:
    """Return (filing_count, latest_country) for an LEI — cheap 1-row probe."""
    try:
        d = _api_get(f"entities/{lei}/filings",
                     {"page[size]": "1", "sort": "-period_end"})
    except Exception:
        return 0, None
    count = (d.get("meta") or {}).get("count", 0) or 0
    rows = d.get("data") or []
    country = rows[0]["attributes"].get("country") if rows else None
    return count, country


def _name_search(query: str, limit: int = 25) -> list[dict[str, str]]:
    """JSON:API `ilike` search over entity names. Returns [{lei, name}, ...]."""
    q = (query or "").strip()
    if not q:
        return []
    filt = json.dumps([{"name": "name", "op": "ilike", "val": f"%{q}%"}])
    try:
        d = _api_get("entities", {"filter": filt, "page[size]": str(limit)})
    except Exception:
        return []
    out: list[dict[str, str]] = []
    for x in d.get("data", []):
        a = x.get("attributes") or {}
        lei = a.get("identifier")
        if lei:
            out.append({"lei": lei, "name": a.get("name") or lei})
    return out


def _rank_candidates(cands: list[dict[str, str]], query: str) -> list[dict[str, str]]:
    """Closest names first: exact (case-insensitive) match, then shortest name
    (a shorter name containing the query is usually the parent issuer)."""
    ql = (query or "").strip().lower()

    def key(c: dict[str, str]) -> tuple[int, int]:
        name = (c.get("name") or "").lower()
        exact = 0 if name == ql else 1
        return (exact, len(name))

    return sorted(cands, key=key)


def _result(lei: str, name: str, country: Optional[str], matched_via: str,
            candidates: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "company_number": lei,           # LEI (used by eu_fetch)
        "title": name,
        "country": country,
        "matched_via": matched_via,
        "candidates": [
            {"number": c["lei"], "title": c["name"]} for c in candidates[:10]
        ] or [{"number": lei, "title": name}],
    }


def search_companies(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Autocomplete search: companies (with ESEF filings) whose name matches
    `query`. ONE JSON:API call — filters filings by the related entity name, so
    every result is guaranteed to have a downloadable report. Returns the latest
    filing per entity: [{lei, name, country, period_end}], newest period first.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    filt = json.dumps([{"name": "entity.name", "op": "ilike", "val": f"%{q}%"}])
    try:
        d = _api_get("filings", {
            "filter": filt, "include": "entity",
            "sort": "-period_end", "page[size]": str(max(limit * 3, 30)),
        })
    except Exception:
        return []

    names = {
        inc["id"]: (inc.get("attributes") or {}).get("name")
        for inc in d.get("included", []) if inc.get("type") == "entity"
    }
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for x in d.get("data", []):
        a = x.get("attributes") or {}
        rurl = a.get("report_url") or ""
        lei = rurl.split("/")[1] if rurl.startswith("/") and len(rurl.split("/")) > 1 else None
        eid = (((x.get("relationships") or {}).get("entity") or {}).get("data") or {}).get("id")
        if not lei or lei in seen:
            continue
        seen.add(lei)
        out.append({
            "lei": lei,
            "name": names.get(eid) or lei,
            "country": a.get("country"),
            "period_end": a.get("period_end"),
        })
        if len(out) >= limit:
            break
    return out


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
    country: Optional[str] = None,
    isin: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve an EU/EEA listing to its LEI via the filings.xbrl.org JSON:API,
    with GLEIF as a bridge for ISIN (and name) → LEI.

    Returns:
        {
          "company_number": "529900D6BF99LW9R2E68",   # LEI
          "title": "SAP SE",
          "country": "DE",
          "matched_via": "lei" | "isin" | "ticker" | "company_name",
          "candidates": [ {number, title}, ... ]
        }

    Raises LookupError if nothing usable is found.
    """
    ticker = (ticker or "").strip()
    company_name = (company_name or "").strip()
    isin = (isin or "").strip().upper()
    country = (country or "").strip().upper() or None
    if not ticker and not company_name and not isin:
        raise LookupError("No ticker, company name, ISIN or LEI supplied to resolve.")

    # 1) LEI supplied directly (in any field).
    for raw in (ticker, company_name, isin):
        if _is_lei(raw):
            lei = raw.strip().upper()
            count, ctry = _entity_has_filings(lei)
            name = lei
            try:
                d = _api_get(f"entities/{lei}")
                name = (d.get("data", {}).get("attributes") or {}).get("name") or lei
            except Exception:
                pass
            return _result(lei, name, ctry or country, "lei", [{"lei": lei, "name": name}])

    # 2) ISIN supplied (explicit field or as the ticker) → GLEIF → LEI.
    isin_in = isin or (ticker if gleif.is_isin(ticker) else "")
    if gleif.is_isin(isin_in):
        hit = gleif.isin_to_lei(isin_in)
        if hit:
            lei, name = hit
            _, ctry = _entity_has_filings(lei)
            return _result(lei, name, ctry or country, "isin", [{"lei": lei, "name": name}])

    # 3) Local ticker → convenience map → name search.
    matched_via = "company_name"
    queries: list[str] = []
    if ticker and not company_name:
        mapped = _TICKER_MAP.get(ticker.upper())
        if mapped:
            queries.append(mapped)
            matched_via = "ticker"
        # also try the ticker root as a name fragment (last resort)
        root = re.sub(r"[-.].*$", "", ticker).strip()
        if len(root) >= 3:
            queries.append(root)
            matched_via = matched_via if mapped else "ticker"

    # 4) Explicit company name (highest-quality query, tried first if present).
    if company_name:
        queries.insert(0, company_name)
        matched_via = "company_name"

    seen: set[str] = set()
    candidates: list[dict[str, str]] = []
    for q in queries:
        for c in _name_search(q):
            if c["lei"] not in seen:
                seen.add(c["lei"])
                candidates.append(c)
        if candidates:
            break

    # 5) Last resort: GLEIF name → LEI (then confirm it has ESEF filings).
    if not candidates:
        hit = gleif.name_to_lei(company_name or ticker)
        if hit:
            lei, name = hit
            count, ctry = _entity_has_filings(lei)
            if count > 0:
                return _result(lei, name, ctry or country, "company_name",
                               [{"lei": lei, "name": name}])

    if not candidates:
        raise LookupError(
            f"No EU/EEA (ESEF) entity found for {ticker or company_name or isin!r}. "
            f"filings.xbrl.org coverage is partial — try the registered company "
            f"name, the ISIN, or the company's LEI."
        )

    ranked = _rank_candidates(candidates, company_name or ticker)

    # Prefer a candidate that actually has ESEF filings (and, with a country
    # hint, one whose latest filing matches). Probe the top few only.
    best_with_filings: Optional[tuple[dict[str, str], Optional[str]]] = None
    for c in ranked[:6]:
        count, ctry = _entity_has_filings(c["lei"])
        if count > 0:
            if country and ctry == country:
                return _result(c["lei"], c["name"], ctry, matched_via, ranked)
            if best_with_filings is None:
                best_with_filings = (c, ctry)
    if best_with_filings is not None:
        c, ctry = best_with_filings
        return _result(c["lei"], c["name"], ctry, matched_via, ranked)

    # None probed had filings — return the closest name match anyway; the fetch
    # step will surface a clear "no ESEF filing" error if it truly has none.
    top = ranked[0]
    return _result(top["lei"], top["name"], country, matched_via, ranked)


if __name__ == "__main__":
    # Manual smoke test:  python eu_resolve.py "Novo Nordisk"   (no API key)
    import sys

    t = sys.argv[1] if len(sys.argv) > 1 else "ASML"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_company_number(t, name), ensure_ascii=False, indent=2))

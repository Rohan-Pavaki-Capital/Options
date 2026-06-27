"""
Brazilian Ticker / Name / CNPJ → CVM code Resolver
==================================================

The Brazilian equivalent of kr_resolve.py / jp_resolve.py. Brazil's CVM
(Comissão de Valores Mobiliários — the securities regulator, the "EDGAR of
Brazil") identifies every listed company (companhia aberta) by a numeric
**CVM code / código CVM** (e.g. 9512 for Petrobras, 4170 for Vale) and by its
**CNPJ** (e.g. 33.000.167/0001-01). B3 stock tickers (PETR4, VALE3) are NOT
used by CVM, so they are mapped to a company here.

This module bridges "whatever the frontend collected" (a B3 ticker, a company
name, or a CNPJ) to the authoritative CVM code, using CVM's free open-data
cadastral registry (cad_cia_aberta.csv) — no API key required.

Resolution strategy (first hit wins):
    1. CNPJ (14 digits, any punctuation)  → exact CNPJ lookup.
    2. Numeric CVM code                   → exact código-CVM lookup.
    3. B3 ticker (e.g. PETR4)             → ticker-root map, then name prefix.
    4. Explicit company name              → name match (active companies first).

Public API:
    resolve_company_number(ticker, company_name=None)
        -> {company_number, cnpj, title, matched_via, candidates}
"""

from __future__ import annotations

import csv
import io
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional

import requests

_CAD_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv"
_CACHE_FILE = Path(__file__).parent / ".cache" / "cvm_cad_cia_aberta.csv"
_CACHE_MAX_AGE = 24 * 3600          # refresh the cadastral list at most daily
_HTTP_TIMEOUT = 60

# Top B3 tickers whose root does not obviously match the registered name.
# Keeps common lookups accurate; everything else falls back to name matching.
_TICKER_MAP = {
    "PETR": "PETROBRAS", "VALE": "VALE", "ITUB": "ITAU UNIBANCO",
    "BBDC": "BRADESCO", "BBAS": "BRASIL", "ABEV": "AMBEV", "B3SA": "B3",
    "WEGE": "WEG", "RENT": "LOCALIZA", "SUZB": "SUZANO", "GGBR": "GERDAU",
    "JBSS": "JBS", "ELET": "ELETROBRAS", "RADL": "RAIA DROGASIL",
    "EQTL": "EQUATORIAL", "PRIO": "PETRORIO", "ITSA": "ITAUSA",
    "VBBR": "VIBRA", "BPAC": "BTG PACTUAL", "CSAN": "COSAN",
}

_CACHE: Optional[list[dict[str, str]]] = None   # in-process parsed cadastral rows


def _strip_accents(s: str) -> str:
    """Uppercase + accent-fold so 'PETRÓLEO' matches 'PETROLEO' (CVM data is
    latin-1 with inconsistent accents)."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s or "")
        if not unicodedata.combining(c)
    ).upper().strip()


def _load_cadastral() -> list[dict[str, str]]:
    """Fetch + parse CVM's open-data cadastral registry, with a daily disk cache."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    text: Optional[str] = None
    if _CACHE_FILE.exists() and (time.time() - _CACHE_FILE.stat().st_mtime) < _CACHE_MAX_AGE:
        text = _CACHE_FILE.read_text(encoding="latin-1")
    else:
        try:
            resp = requests.get(_CAD_URL, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = "latin-1"
            text = resp.text
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(text, encoding="latin-1")
        except Exception:
            if _CACHE_FILE.exists():           # serve a stale cache rather than fail
                text = _CACHE_FILE.read_text(encoding="latin-1")
            else:
                raise

    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    _CACHE = rows
    return rows


def _digits(s: str) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def _as_result(row: dict[str, str], matched_via: str) -> dict[str, Any]:
    cd_cvm = str(int(_digits(row.get("CD_CVM") or "0")))   # normalise "009512" → "9512"
    name = row.get("DENOM_SOCIAL") or row.get("DENOM_COMERC") or cd_cvm
    return {
        "company_number": cd_cvm,                # CVM code (used by br_fetch)
        "cnpj": row.get("CNPJ_CIA"),
        "title": name,
        "matched_via": matched_via,
        "candidates": [
            {"number": cd_cvm, "title": name, "cnpj": row.get("CNPJ_CIA")}
        ],
    }


def _name_matches(rows: list[dict[str, str]], query: str) -> list[dict[str, str]]:
    """Rows whose social or commercial name contains `query` (accent-folded),
    active companies (SIT=ATIVO) first."""
    q = _strip_accents(query)
    if not q:
        return []
    matched = [
        r for r in rows
        if q in _strip_accents(r.get("DENOM_SOCIAL", ""))
        or q in _strip_accents(r.get("DENOM_COMERC", ""))
    ]
    active = [r for r in matched if (r.get("SIT") or "").upper().startswith("ATIVO")]
    return active or matched


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a Brazilian listing to its CVM code.

    Returns:
        {
          "company_number": "9512",            # CVM code
          "cnpj": "33.000.167/0001-01",
          "title": "PETROLEO BRASILEIRO S.A. PETROBRAS",
          "matched_via": "cnpj" | "cvm_code" | "ticker" | "company_name",
          "candidates": [ {number, title, cnpj}, ... ]
        }

    Raises LookupError if nothing usable is found.
    """
    ticker = (ticker or "").strip().upper()
    company_name = (company_name or "").strip()
    if not ticker and not company_name:
        raise LookupError("No ticker, company name or CNPJ supplied to resolve.")

    rows = _load_cadastral()

    # 1) CNPJ (14 digits).
    raw = _digits(ticker)
    if len(raw) == 14:
        for r in rows:
            if _digits(r.get("CNPJ_CIA")) == raw:
                return _as_result(r, "cnpj")

    # 2) Pure numeric CVM code (cadastral codes are <= 6 digits).
    if ticker.isdigit() and 1 <= len(ticker) <= 6:
        want = str(int(ticker))
        for r in rows:
            if str(int(_digits(r.get("CD_CVM") or "0"))) == want:
                return _as_result(r, "cvm_code")

    # 3) B3 ticker → root map, then name prefix (e.g. "PETR4" → "PETR").
    if ticker and not company_name:
        root = re.sub(r"\d+$", "", ticker)             # strip the share-class digits
        mapped = _TICKER_MAP.get(root)
        if mapped:
            hits = _name_matches(rows, mapped)
            if hits:
                return _as_result(hits[0], "ticker")
        if len(root) >= 4:                              # last-resort root prefix match
            hits = _name_matches(rows, root)
            if hits:
                return _as_result(hits[0], "ticker")

    # 4) Explicit company name (or ticker as a name fallback).
    query = company_name or ticker
    hits = _name_matches(rows, query)
    if hits:
        return _as_result(hits[0], "company_name" if company_name else "ticker")

    raise LookupError(
        f"No CVM (Brazil) company found for {ticker or company_name!r}. "
        f"Try the CNPJ, the numeric CVM code, or the registered company name."
    )


if __name__ == "__main__":
    # Manual smoke test:  python br_resolve.py PETR4   (no API key needed)
    import json
    import sys

    t = sys.argv[1] if len(sys.argv) > 1 else "PETR4"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_company_number(t, name), ensure_ascii=False, indent=2))

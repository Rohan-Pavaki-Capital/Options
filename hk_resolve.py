"""
Hong Kong (HKEXnews) Ticker / Name → stockId Resolver
=====================================================

The HK equivalent of br_resolve.py / cn_resolve.py. HKEXnews (the HKEX disclosure
site) identifies every listed issuer by an internal **stockId** (e.g. 7609 for
Tencent), distinct from the public 5-digit stock code (00700). Documents are
searched by stockId, so we bridge "whatever the frontend collected" (a HK stock
code like 700 / 00700, or a company name) to that stockId.

HKEXnews's old `prefix.do` autocomplete endpoint was retired (now returns empty);
the search page instead loads a static securities master JSON
(`activestock_sehk_e.json`) client-side. We use that master directly, disk-cached
daily like CVM's cadastral CSV — no API key, no bot wall.

Each master row is {"i": stockId, "c": code, "n": name, "s": ...}.

Resolution:
    1. Numeric HK code (1-5 digits, zero-padded to 5) → exact code match.
    2. Company name → name-contains match (first hit).

Public API:
    resolve_company_number(ticker, company_name=None)
        -> {company_number(stockId), code, title, matched_via, candidates}
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import requests

_MASTER_URL = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
_CACHE_FILE = Path(__file__).parent / ".cache" / "hkex_activestock_sehk.json"
_CACHE_MAX_AGE = 24 * 3600          # refresh the securities master at most daily
_HTTP_TIMEOUT = 40
_UA = {"User-Agent": "Mozilla/5.0 (OptionsExtractor; +https://hkexnews.hk)"}

_CACHE: Optional[list[dict[str, Any]]] = None   # in-process parsed master rows


def _norm_code(s: str) -> str:
    """Zero-pad a numeric HK stock code to 5 digits (700 -> 00700)."""
    digits = "".join(c for c in str(s or "") if c.isdigit())
    return digits.zfill(5) if digits else ""


def _load_master() -> list[dict[str, Any]]:
    """Fetch + parse HKEXnews' active-securities master, with a daily disk cache."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    text: Optional[str] = None
    if _CACHE_FILE.exists() and (time.time() - _CACHE_FILE.stat().st_mtime) < _CACHE_MAX_AGE:
        text = _CACHE_FILE.read_text(encoding="utf-8")
    else:
        try:
            resp = requests.get(_MASTER_URL, headers=_UA, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            text = resp.text
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(text, encoding="utf-8")
        except Exception:
            if _CACHE_FILE.exists():           # serve a stale cache rather than fail
                text = _CACHE_FILE.read_text(encoding="utf-8")
            else:
                raise

    t = text.strip()
    if not t.startswith("["):                  # tolerate a JSONP/var wrapper
        t = t[t.find("["): t.rfind("]") + 1]
    rows = json.loads(t)
    _CACHE = rows if isinstance(rows, list) else []
    return _CACHE


def _as_result(row: dict[str, Any], matched_via: str,
               candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "company_number": str(row.get("i")),       # HKEXnews stockId (used by hk_fetch)
        "code": _norm_code(str(row.get("c"))),
        "title": row.get("n"),
        "matched_via": matched_via,
        "candidates": candidates,
    }


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a HK listing to its HKEXnews stockId.

    Returns {company_number: "<stockId>", code, title, matched_via, candidates}.
    Raises LookupError if nothing is found.
    """
    ticker = (ticker or "").strip()
    company_name = (company_name or "").strip()
    if not ticker and not company_name:
        raise LookupError("No HK stock code or company name supplied to resolve.")

    rows = _load_master()

    # 1) Numeric HK code → exact (zero-padded) code match.
    if ticker and any(ch.isdigit() for ch in ticker) and not company_name:
        code_norm = _norm_code(ticker)
        if code_norm:
            for r in rows:
                if _norm_code(str(r.get("c"))) == code_norm:
                    return _as_result(r, "code", [
                        {"number": str(r.get("i")), "code": code_norm, "title": r.get("n")}
                    ])

    # 2) Company name (or ticker as a name fallback) → name-contains.
    query = (company_name or ticker).upper()
    if query:
        hits = [r for r in rows if query in str(r.get("n") or "").upper()]
        if hits:
            hits.sort(key=lambda r: len(str(r.get("n") or "")))   # prefer the tightest match
            cands = [
                {"number": str(r.get("i")), "code": _norm_code(str(r.get("c"))),
                 "title": r.get("n")}
                for r in hits[:10]
            ]
            return _as_result(hits[0], "company_name" if company_name else "ticker", cands)

    raise LookupError(
        f"No HKEX (Hong Kong) listing found for {ticker or company_name!r}. "
        f"Try the numeric stock code (e.g. 700) or the registered company name."
    )


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "700"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_company_number(t, name), ensure_ascii=False, indent=2))

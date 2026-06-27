"""
Thailand (SEC Thailand 56-1 One Report) Ticker / Name -> file-id Resolver
=========================================================================

Thailand's listed-company annual report is the **56-1 One Report**, filed with
the SEC (Securities and Exchange Commission, Thailand) and published — free and
**un-walled** — on the SEC's iDisc service. The SET exchange portal itself is
Akamai bot-walled (Asia spike), but the regulator's listing is plain HTTP, so we
route through the SEC.

The English 56-1 listing page
    https://market.sec.or.th/public/idisc/en/Viewmore/fs-r561
is a single (un-paginated) table of EVERY company's latest One Report, each row
giving the company NAME, the report year, the filing date, and a ZIP download
id. There is NO ticker/symbol column, so resolution is by company name.

Resolution (first hit wins):
    1. Curated major-issuer ticker (PTT, KBANK, CPALL ...) -> company name.
    2. Exact / prefix match on the normalised company name.
    3. Fuzzy company-name match (rapidfuzz token_set_ratio).
Among rows for the matched company, the newest filing date wins.

The table is fetched + parsed once and disk-cached daily (like BSE's scrip
master). The resolved `company_number` is the SEC ZIP **file id**
(`dat/f56/<...>.zip`) — the key th_fetch downloads directly.

Public API:
    resolve_company_number(ticker, company_name=None)
        -> {company_number(file id), ticker, title, year, matched_via, candidates}
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from curl_cffi import requests as creq

try:
    from rapidfuzz import fuzz
    _HAVE_FUZZ = True
except Exception:                                   # pragma: no cover
    _HAVE_FUZZ = False

_LISTING_URL = "https://market.sec.or.th/public/idisc/en/Viewmore/fs-r561"
_CACHE_FILE = Path(__file__).parent / ".cache" / "th_sec_56-1_master.json"
_CACHE_MAX_AGE = 24 * 3600
_HTTP_TIMEOUT = 60

_CACHE: Optional[list[dict[str, Any]]] = None       # in-process parsed rows

# Curated major-issuer ticker -> a distinctive token of the company name. Used
# because the SEC listing carries no symbol; the name match covers the rest.
_TICKER_NAME: dict[str, str] = {
    "ptt": "PTT PUBLIC", "pttep": "PTT EXPLORATION", "pttgc": "PTT GLOBAL",
    "or": "PTT OIL AND RETAIL", "top": "THAI OIL", "irpc": "IRPC",
    "scb": "SCB X", "scbx": "SCB X", "kbank": "KASIKORNBANK",
    "bbl": "BANGKOK BANK", "ktb": "KRUNG THAI BANK", "ttb": "TMBTHANACHART",
    "scc": "SIAM CEMENT", "cpall": "CP ALL", "cpf": "CHAROEN POKPHAND FOODS",
    "cpn": "CENTRAL PATTANA", "crc": "CENTRAL RETAIL", "advanc": "ADVANCED INFO",
    "intuch": "INTOUCH", "true": "TRUE CORPORATION", "aot": "AIRPORTS OF THAILAND",
    "bdms": "BANGKOK DUSIT MEDICAL", "bh": "BUMRUNGRAD", "mint": "MINOR INTERNATIONAL",
    "hmpro": "HOME PRODUCT CENTER", "gulf": "GULF", "ea": "ENERGY ABSOLUTE",
    "delta": "DELTA ELECTRONICS", "ivl": "INDORAMA VENTURES", "banpu": "BANPU",
    "lh": "LAND AND HOUSES", "kkp": "KIATNAKIN", "tisco": "TISCO",
    "egco": "ELECTRICITY GENERATING", "ratch": "RATCH", "bgrim": "B.GRIMM",
}

_SUFFIXES = (" public company limited", " company limited", " pcl.", " pcl",
             " plc.", " plc", " limited", " co., ltd.", " co.,ltd.")


def _norm_name(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip().lower())
    if s.startswith("the "):                        # "The Siam Cement" -> "siam cement"
        s = s[4:]
    for suf in _SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
            break
    return s


def _parse_listing(html: str) -> list[dict[str, Any]]:
    """Parse the SEC 56-1 listing table into rows
    [{name, year, date, fileid}]."""
    out: list[dict[str, Any]] = []
    for tr in re.split(r"<tr", html):
        if "Download?FILEID" not in tr:
            continue
        name_m = re.search(r'RgCol_Left[^>]*>\s*([^<]+?)\s*<', tr)
        year_m = re.search(r'RgCol_Center[^>]*>\s*(\d{4})', tr)
        date_m = re.search(r'RgCol_Center[^>]*>\s*(\d{2}/\d{2}/\d{4})', tr)
        fid_m = re.search(r'FILEID=([^"\'&]+\.zip)', tr)
        if not (name_m and fid_m):
            continue
        out.append({
            "name": name_m.group(1).strip(),
            "year": int(year_m.group(1)) if year_m else 0,
            "date": date_m.group(1) if date_m else "",
            "fileid": fid_m.group(1).strip(),
        })
    return out


def _load_master() -> list[dict[str, Any]]:
    """Fetch + parse the SEC 56-1 listing, with a daily disk cache."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    rows: Optional[list[dict[str, Any]]] = None
    if _CACHE_FILE.exists() and (time.time() - _CACHE_FILE.stat().st_mtime) < _CACHE_MAX_AGE:
        try:
            rows = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            rows = None
    if rows is None:
        try:
            html = creq.get(_LISTING_URL, impersonate="chrome",
                            timeout=_HTTP_TIMEOUT).text
            rows = _parse_listing(html)
            if rows:
                _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                _CACHE_FILE.write_text(json.dumps(rows, ensure_ascii=False),
                                       encoding="utf-8")
        except Exception:
            if _CACHE_FILE.exists():                # serve stale rather than fail
                rows = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            else:
                raise
    _CACHE = rows or []
    return _CACHE


def _date_key(d: str) -> tuple[int, int, int]:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", d or "")
    return (int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else (0, 0, 0)


def _as_result(row: dict[str, Any], ticker: str, matched_via: str,
               candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "company_number": row["fileid"],            # SEC ZIP file id (th_fetch key)
        "ticker": ticker,
        "title": row["name"],
        "year": row.get("year") or None,
        "matched_via": matched_via,
        "candidates": candidates,
    }


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a Thai listing to its SEC 56-1 One Report ZIP file id.

    Returns {company_number(file id), ticker, title, year, matched_via,
    candidates}. Raises LookupError if nothing matches.
    """
    ticker = (ticker or "").strip()
    company_name = (company_name or "").strip()
    if not ticker and not company_name:
        raise LookupError("No ticker or company name supplied to resolve.")

    rows = _load_master()
    if not rows:
        raise LookupError("SEC Thailand 56-1 listing was empty/unreachable.")

    # Map a curated ticker to a distinctive name token; else use what was typed.
    query = company_name or ticker
    tkey = re.sub(r"\s+", "", ticker.lower())
    if not company_name and tkey in _TICKER_NAME:
        query = _TICKER_NAME[tkey]
    qn = _norm_name(query)

    def _best(matches: list[dict[str, Any]]) -> dict[str, Any]:
        return max(matches, key=lambda r: _date_key(r["date"]))

    # 1) exact normalised-name match.
    exact = [r for r in rows if _norm_name(r["name"]) == qn]
    if exact:
        return _as_result(_best(exact), ticker, "company_name", [])

    # 2) prefix match (e.g. "ptt" -> "ptt public company limited").
    prefix = [r for r in rows if _norm_name(r["name"]).startswith(qn + " ")
              or _norm_name(r["name"]) == qn]
    if prefix:
        prefix.sort(key=lambda r: (len(_norm_name(r["name"])), ) )
        # keep only the shortest-name company family, newest filing
        shortest = _norm_name(prefix[0]["name"])
        fam = [r for r in prefix if _norm_name(r["name"]) == shortest]
        return _as_result(_best(fam), ticker, "company_name",
                          [{"title": r["name"], "year": r.get("year")} for r in prefix[:6]])

    # 3) fuzzy match.
    if _HAVE_FUZZ:
        scored = sorted(
            rows, key=lambda r: fuzz.token_set_ratio(qn, _norm_name(r["name"])),
            reverse=True,
        )
        top = scored[0]
        if fuzz.token_set_ratio(qn, _norm_name(top["name"])) >= 80:
            cands = [{"title": r["name"], "year": r.get("year")} for r in scored[:6]]
            # newest filing among rows sharing the top company's name
            fam = [r for r in rows if _norm_name(r["name"]) == _norm_name(top["name"])]
            return _as_result(_best(fam) if fam else top, ticker, "company_name", cands)

    raise LookupError(
        f"No SEC Thailand 56-1 One Report found for {ticker or company_name!r}. "
        f"Try the full company name (the SEC listing has no ticker column)."
    )


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "PTT"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_company_number(t, name), ensure_ascii=False, indent=2))

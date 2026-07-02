"""
Israel (TASE / MAYA) Ticker / Name -> companyId Resolver
=========================================================

MAYA (maya.tase.co.il — the TASE disclosure portal) identifies every listed
company by a numeric **companyId** (e.g. 604 = Bank Leumi). The annual-report
fetch (`il_fetch`) is keyed by this companyId, so this module bridges
"whatever the analyst typed" (a companyId, a TASE English ticker, or a company
name) to it.

WHY A CURATED MAP (and not a live search):
  TASE fronts its data APIs (apicontent.tase.co.il / mayaapi) with an Incapsula
  bot wall that rejects plain HTTP, headless browsers AND raw API calls made
  through Firecrawl's stealth proxy (only fully-rendered *pages* pass — verified
  2026-06-04). The public companies directory renders just 30 of ~667 rows
  (client-side virtualised; no working page-size / filter param), so it cannot
  be harvested cheaply into a master. We therefore follow the EU adapter's
  precedent (`eu_resolve._TICKER_MAP`): a curated map of major issuers for
  name/ticker convenience, with the **numeric companyId accepted directly** as
  the universal path (every TASE company is reachable by its companyId, which is
  shown in the maya.tase.co.il/en/companies/<id> URL).

Public API:
    resolve_company_number(ticker, company_name=None)
        -> {company_number(companyId), title, matched_via, candidates}
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Curated map (TASE English ticker AND name tokens -> companyId). EVERY entry
# here is VERIFIED against the live maya.tase.co.il/en/companies/<id> page
# (2026-06-04) — unverified guesses are intentionally excluded, because a wrong
# id would silently fetch the wrong company's report. Grow as needed; the
# numeric-companyId path below covers every other TASE issuer.
_COMPANY_MAP: dict[str, str] = {
    # ticker         companyId       # company
    "lumi": "604",                   # Bank Leumi
    "poli": "662",                   # Bank Hapoalim
    "teva": "629",                   # Teva Pharmaceutical Industries
    "eslt": "1040", "elbt": "1040",  # Elbit Systems
    "adma": "1063",                  # Adama Agricultural Solutions
}

# Name token -> companyId (contains-match). Verified, same date.
_NAME_HINTS = {
    "bank leumi": "604", "leumi": "604",
    "bank hapoalim": "662", "hapoalim": "662",
    "teva": "629", "teva pharmaceutical": "629",
    "elbit systems": "1040", "elbit": "1040",
    "adama": "1063",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a TASE listing to its MAYA companyId.

    Accepts (in priority order):
      1. a numeric companyId verbatim (universal — every company has one),
      2. a curated TASE English ticker (LUMI, POLI, TEVA, ESLT, ...),
      3. a company name token present in the curated map.

    Returns {company_number, title, matched_via, candidates}.
    Raises LookupError with guidance if nothing matches.
    """
    ticker = (ticker or "").strip()
    company_name = (company_name or "").strip()
    if not ticker and not company_name:
        raise LookupError("No companyId, ticker or company name supplied to resolve.")

    # 1) numeric companyId verbatim
    if ticker.isdigit():
        return {"company_number": ticker, "title": company_name or ticker,
                "matched_via": "company_id", "candidates": []}
    if company_name.isdigit():
        return {"company_number": company_name, "title": ticker or company_name,
                "matched_via": "company_id", "candidates": []}

    # 2) curated ticker
    t = _norm(ticker).replace(".ta", "").replace(" ", "")
    if t in _COMPANY_MAP:
        return {"company_number": _COMPANY_MAP[t], "title": company_name or ticker,
                "matched_via": "ticker", "candidates": []}

    # 3) name token (contains-match against curated hints)
    for q in (_norm(company_name), _norm(ticker)):
        if not q:
            continue
        if q in _NAME_HINTS:
            return {"company_number": _NAME_HINTS[q], "title": company_name or ticker,
                    "matched_via": "company_name", "candidates": []}
        hits = [(name, cid) for name, cid in _NAME_HINTS.items() if q and q in name]
        if hits:
            name, cid = hits[0]
            return {"company_number": cid, "title": company_name or ticker,
                    "matched_via": "company_name",
                    "candidates": [{"number": c, "title": n} for n, c in hits[:6]]}

    raise LookupError(
        f"Could not resolve {ticker or company_name!r} to a TASE companyId. "
        f"TASE's data API is bot-walled, so name search is limited to major "
        f"issuers; supply the numeric companyId instead (it is shown in the "
        f"company's maya.tase.co.il/en/companies/<id> URL)."
    )


if __name__ == "__main__":
    import json
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "LUMI"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_company_number(t, name), ensure_ascii=False, indent=2))

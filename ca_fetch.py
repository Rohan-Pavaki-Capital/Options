"""
Canada (SEC EDGAR / MJDS 40-F) Filing Fetcher
=============================================

Canada's SEDAR+ has no open API and is bot-walled (Radware + hCaptcha), so a
ticker-driven SEDAR+ fetch isn't reliable. BUT most Canadian issuers that matter
to an equity analyst are cross-listed in the US and file their annual report with
the SEC under the Multijurisdictional Disclosure System (MJDS) as Form **40-F**
(or, for some, 20-F / 10-K). This module fetches that filing BY TICKER via the
edgartools library — giving Canada a ticker-only workflow despite SEDAR+.

Key wrinkle: a 40-F's *primary* document is only a short cover page (~13 pp). The
audited consolidated financial statements — where the share-based-payment note
lives — are filed as an **exhibit** (typically EX-1.2, sometimes EX-99.x). We
locate that exhibit by scoring each exhibit's text for financial-statement
markers (share-based / consolidated statements / auditor / vesting …), then render
THAT exhibit to PDF for the pipeline. 20-F and 10-K are self-contained, so their
primary document is rendered directly.

Only SEC-registered (US-cross-listed) Canadian issuers are reachable here;
TSX-only issuers must be uploaded manually (the CA tab notes this).

Public API:
    fetch_filing_as_pdf(ticker, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

import requests

import ocr_pdf
# Reuse the EDGAR helpers so rendering/identity behaviour stays identical and the
# US · EDGAR tab is left completely untouched.
from edgar_fetch import _ensure_identity, _normalize_ticker, _html_to_pdf

# MJDS 40-F first (the Canadian annual report), then FPI 20-F, then US-domestic 10-K.
_FORMS = ["40-F", "20-F", "10-K"]
_FIN_MARKERS = [
    "share-based", "stock-based compensation", "stock option",
    "consolidated statements", "independent auditor",
    "registered public accounting", "vesting", "notes to the consolidated",
]
_HTTP_TIMEOUT = 60


def _ua() -> str:
    return (os.environ.get("EDGAR_IDENTITY") or os.environ.get("SEC_USER_AGENT")
            or "Pavaki Options Extractor contact@pavaki.local")


def _pick_filing(company):
    """Return (filing, form) for the latest annual filing, trying 40-F → 20-F → 10-K."""
    for form in _FORMS:
        try:
            fs = company.get_filings(form=form)
        except Exception:
            fs = None
        if fs is not None and len(fs) > 0:
            return fs[0], form
    raise LookupError(
        f"No 40-F / 20-F / 10-K filing found on SEC EDGAR for "
        f"{getattr(company, 'name', '?')} (CIK {getattr(company, 'cik', '?')}). "
        f"This issuer may be TSX-only (not SEC-registered) — upload its annual "
        f"financial statements PDF instead."
    )


def _score(text: str) -> int:
    low = (text or "").lower()
    return sum(low.count(k) for k in _FIN_MARKERS)


def _financial_exhibit_html(filing) -> tuple[Optional[str], Optional[str]]:
    """Find the financial-statements exhibit of a 40-F and return (html, label).

    Scores each EX-1.x / EX-99.x HTML exhibit by financial-statement markers and
    picks the strongest (ties broken by length). Returns (None, None) if no
    exhibit stands out.
    """
    best = None
    best_score = 0
    best_len = -1
    for a in getattr(filing, "attachments", []) or []:
        doc = str(getattr(a, "document", "") or "")
        dt = str(getattr(a, "document_type", "") or "")
        if not doc.lower().endswith((".htm", ".html")):
            continue
        if re.match(r"R\d+\.htm", doc):          # XBRL viewer fragments
            continue
        if not (dt.startswith("EX-1") or dt.startswith("EX-99")):
            continue
        try:
            txt = a.text() or ""
        except Exception:
            txt = ""
        sc = _score(txt)
        if sc > best_score or (sc == best_score and len(txt) > best_len):
            best, best_score, best_len = a, sc, len(txt)

    if best is None or best_score == 0:
        return None, None

    url = getattr(best, "url", None)
    if not url:
        return None, None
    try:
        r = requests.get(url, headers={"User-Agent": _ua()}, timeout=_HTTP_TIMEOUT)
        if r.status_code == 200 and r.text:
            return r.text, str(getattr(best, "document_type", "EX"))
    except Exception:
        pass
    return None, None


def fetch_filing_as_pdf(
    ticker: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Fetch the latest annual filing for a Canadian (US-cross-listed) issuer by
    ticker, render the financial statements to PDF, return metadata. Signature
    mirrors the other fetchers so the pipeline branch stays uniform."""
    ticker = _normalize_ticker(ticker)
    _ensure_identity()

    from edgar import Company
    company = Company(ticker)
    if company is None:
        raise LookupError(f"No SEC EDGAR company found for ticker {ticker!r}.")

    filing, form = _pick_filing(company)

    html: Optional[str] = None
    exhibit: Optional[str] = None
    if form == "40-F":
        # The 40-F cover has no financials; pull the financial-statements exhibit.
        html, exhibit = _financial_exhibit_html(filing)
    if not html:
        # 20-F / 10-K are self-contained; also the 40-F fallback if no exhibit found.
        try:
            html = filing.html()
        except Exception:
            html = None
    if not html:
        try:
            text = filing.text() if hasattr(filing, "text") else ""
        except Exception:
            text = ""
        if not text:
            raise RuntimeError("SEC filing returned neither HTML nor text content.")
        html = ("<!doctype html><html><head><meta charset='utf-8'>"
                "<style>body{font-family:sans-serif;font-size:11pt;white-space:pre-wrap;}</style>"
                "</head><body>" + text + "</body></html>")

    out_pdf_path = Path(out_pdf_path)
    _html_to_pdf(html, out_pdf_path)

    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    filing_date = str(getattr(filing, "filing_date", "") or "")
    return {
        "ticker": ticker,
        "company": getattr(company, "name", None) or company_name or ticker,
        "category": category,
        "form": f"{form} / {exhibit}" if exhibit else form,
        "filing_date": filing_date,
        "report_period": filing_date,
        "exhibit": exhibit,
        "cik": getattr(company, "cik", None),
        "accession": getattr(filing, "accession_no", None)
                     or getattr(filing, "accession_number", None),
        "source_format": "text/html (SEC MJDS filing) -> PDF",
        "ocr": ocr_info,
        "url": getattr(filing, "filing_url", None)
               or getattr(filing, "homepage_url", None),
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    import json
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "SHOP"
    info = fetch_filing_as_pdf(t, "annual", f"_test_ca_{t}.pdf")
    print(json.dumps(info, indent=2, ensure_ascii=False))

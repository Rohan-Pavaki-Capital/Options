"""
EDGAR Filing Fetcher
====================

Given a ticker symbol, fetches the most recent filing of a given form type
(10-K by default) from SEC EDGAR via the `edgartools` library, then renders
the filing's HTML into a PDF that the existing extraction pipeline consumes.

The pipeline in `options.py` / `backend.py` expects a PDF on disk, so we
convert HTML -> PDF using headless Chromium (Playwright). This preserves
table layout for financial statements far better than CSS-only converters.

Public API:
    fetch_filing_as_pdf(ticker, form, out_pdf_path) -> dict[str, Any]
        Downloads the filing, writes a PDF to `out_pdf_path`, returns
        metadata: {accession, form, filing_date, company, cik, url}.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


# ── SEC requires a User-Agent identity for all programmatic access ──
def _ensure_identity() -> None:
    identity = os.environ.get("EDGAR_IDENTITY") or os.environ.get(
        "SEC_USER_AGENT"
    )
    if not identity:
        # Fall back to a generic identity. SEC accepts any "name email"-style
        # string; they only enforce that *something* is set.
        identity = "Pavaki Options Extractor contact@pavaki.local"
    try:
        from edgar import set_identity
        set_identity(identity)
    except Exception:
        pass


def _normalize_ticker(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t):
        raise ValueError(f"Invalid ticker symbol: {ticker!r}")
    return t


def _resolve_company(ticker: str):
    from edgar import Company
    company = Company(ticker)
    if company is None:
        raise LookupError(f"No EDGAR company found for ticker {ticker!r}")
    return company


def _latest_filing(company, form: str):
    filings = company.get_filings(form=form)
    if filings is None or len(filings) == 0:
        raise LookupError(
            f"No {form} filings found for {getattr(company, 'name', '?')} "
            f"(CIK {getattr(company, 'cik', '?')})"
        )
    # edgartools returns most-recent-first
    return filings[0]


def _filing_html(filing) -> str:
    # `Filing.html()` returns the primary document as HTML. For 10-K/20-F,
    # this is the consolidated filing body.
    try:
        html = filing.html()
    except Exception:
        html = None

    if not html:
        # Fallback: render the filing as text -> minimal HTML wrapper.
        text = filing.text() if hasattr(filing, "text") else ""
        if not text:
            raise RuntimeError("Filing returned neither HTML nor text content")
        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<style>body{font-family:sans-serif;font-size:11pt;white-space:pre-wrap;}</style>"
            "</head><body>" + text + "</body></html>"
        )
    return html


def _html_to_pdf(html: str, out_pdf_path: Path) -> None:
    """Render HTML -> PDF via headless Chromium (Playwright).

    Playwright handles complex HTML tables (which 10-K financial
    statements are full of) reliably across platforms.
    """
    from playwright.sync_api import sync_playwright

    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context()
            page = context.new_page()
            # `wait_until='load'` is enough for static EDGAR HTML.
            page.set_content(html, wait_until="load", timeout=120_000)
            page.pdf(
                path=str(out_pdf_path),
                format="Letter",
                margin={
                    "top": "0.5in",
                    "bottom": "0.5in",
                    "left": "0.4in",
                    "right": "0.4in",
                },
                print_background=True,
            )
        finally:
            browser.close()


def fetch_filing_as_pdf(
    ticker: str,
    form: str,
    out_pdf_path: str | Path,
) -> dict[str, Any]:
    """Fetch latest `form` filing for `ticker`, write PDF, return metadata."""
    ticker = _normalize_ticker(ticker)
    form = (form or "10-K").strip().upper()

    _ensure_identity()
    company = _resolve_company(ticker)
    filing = _latest_filing(company, form)

    html = _filing_html(filing)
    out_pdf_path = Path(out_pdf_path)
    _html_to_pdf(html, out_pdf_path)

    return {
        "ticker": ticker,
        "form": form,
        "accession": getattr(filing, "accession_no", None)
                     or getattr(filing, "accession_number", None),
        "filing_date": str(getattr(filing, "filing_date", "") or ""),
        "company": getattr(company, "name", None) or ticker,
        "cik": getattr(company, "cik", None),
        "url": getattr(filing, "filing_url", None)
               or getattr(filing, "homepage_url", None),
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }

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


# ── Incorporated-by-reference financials (user rule 2026-07-09) ──────
# Some filers (e.g. IBM) put the financial statements — including the
# share-based compensation note — in a SEPARATE document of the same EDGAR
# submission (an exhibit like EX-13), and only "incorporate it by
# reference" into the primary 10-K body. Rendering just the primary
# document silently drops the options data. Fix: after rendering, check
# the primary text for the SBC note; if absent, scan the filing's other
# .htm documents for it and APPEND the matching one(s) to the PDF.

# A real SBC note has BOTH a weighted-average price/fair-value phrase AND
# rollforward flow words. The Item 12 "Equity Compensation Plan
# Information" table has the price phrase but no granted/forfeited flows,
# so it alone does NOT count as the note.
_SBC_PRICE_RE = re.compile(
    r"weighted[\s-]*average\s+(exercise\s+price|grant[\s-]*date\s+fair\s+value)",
    re.IGNORECASE)
_SBC_FLOW_RE = re.compile(
    r"\bgranted\b.*\b(exercised|forfeited|vested)\b"
    r"|\b(exercised|forfeited|vested)\b.*\bgranted\b",
    re.IGNORECASE | re.DOTALL)


def _text_has_sbc_note(text: str) -> bool:
    t = re.sub(r"\s+", " ", text or "")
    return bool(_SBC_PRICE_RE.search(t)) and bool(_SBC_FLOW_RE.search(t))


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


def _append_sbc_exhibits_if_missing(filing, primary_html: str,
                                    out_pdf_path: Path) -> int:
    """If the rendered primary document lacks the share-based-comp note,
    find the submission's other .htm documents that contain it, render them
    and append their pages to the PDF. Returns how many were appended.
    Never raises — a failure here leaves the primary-only PDF intact."""
    import sys

    try:
        if _text_has_sbc_note(_strip_tags(primary_html)):
            return 0  # note already in the primary document — nothing to do

        primary_name = str(getattr(filing, "primary_document", "") or "")
        appended = 0
        scanned = 0
        extra_pdfs: list[Path] = []
        for att in getattr(filing, "attachments", None) or []:
            name = str(getattr(att, "document", "") or "")
            low = name.lower()
            if not low.endswith((".htm", ".html")):
                continue
            # Skip the primary itself and XBRL-viewer renders (R1.htm...).
            if name == primary_name or re.match(r"^r\d+\.htm", low) \
                    or "filingsummary" in low:
                continue
            if scanned >= 30 or appended >= 2:
                break
            scanned += 1
            try:
                content = att.download()
                if isinstance(content, bytes):
                    content = content.decode("utf-8", errors="replace")
            except Exception:
                continue
            if not isinstance(content, str) or not _text_has_sbc_note(
                    _strip_tags(content)):
                continue
            print(f"[edgar] SBC note missing from primary document — "
                  f"appending exhibit {name}", file=sys.stderr)
            exh_pdf = out_pdf_path.with_suffix(f".exh{appended + 1}.pdf")
            _html_to_pdf(content, exh_pdf)
            extra_pdfs.append(exh_pdf)
            appended += 1

        if extra_pdfs:
            import fitz
            doc = fitz.open(str(out_pdf_path))
            for p in extra_pdfs:
                with fitz.open(str(p)) as ex:
                    doc.insert_pdf(ex)
            tmp = out_pdf_path.with_suffix(".merged.pdf")
            doc.save(str(tmp))
            doc.close()
            tmp.replace(out_pdf_path)
            for p in extra_pdfs:
                try:
                    p.unlink()
                except Exception:
                    pass
        elif appended == 0:
            print(f"[edgar] SBC note missing from primary document and no "
                  f"matching exhibit found in the submission", file=sys.stderr)
        return appended
    except Exception as exc:
        print(f"[edgar] exhibit-append skipped ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 0


# Sentinel form value: pick the single newest filing across these form types
# (annual vs quarterly, whichever was filed most recently).
_LATEST_FORMS = ["10-K", "10-Q"]


def fetch_filing_as_pdf(
    ticker: str,
    form: str,
    out_pdf_path: str | Path,
) -> dict[str, Any]:
    """Fetch latest `form` filing for `ticker`, write PDF, return metadata.

    `form="LATEST"` fetches the most recent filing across 10-K and 10-Q;
    the returned metadata's `form` then reflects the actual filing type."""
    ticker = _normalize_ticker(ticker)
    form = (form or "10-K").strip().upper()

    _ensure_identity()
    company = _resolve_company(ticker)
    query_form = _LATEST_FORMS if form == "LATEST" else form
    filing = _latest_filing(company, query_form)
    if form == "LATEST":
        form = str(getattr(filing, "form", "") or "") or "LATEST"

    html = _filing_html(filing)
    out_pdf_path = Path(out_pdf_path)
    _html_to_pdf(html, out_pdf_path)

    # Incorporated-by-reference financials (IBM-style filers): append the
    # exhibit holding the share-based-comp note when the primary lacks it.
    _append_sbc_exhibits_if_missing(filing, html, out_pdf_path)

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

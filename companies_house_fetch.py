"""
Companies House Filing Fetcher  (UK equivalent of edgar_fetch.py)
=================================================================

Given a UK company number, fetches the most recent *accounts* filing from
Companies House via its free official REST API, then materialises it as a PDF
that the existing extraction pipeline (options.py / backend.py) consumes.

Companies House serves documents either directly as PDF or as iXBRL
(application/xhtml+xml). When we only get iXBRL we render it to PDF with
headless Chromium (Playwright) — exactly like the EDGAR path does for HTML.

Flow (all on the free REST API, HTTP Basic auth: api_key as username):
    1. GET /company/{number}/filing-history?category=accounts   -> list filings
    2. pick the most recent accounts filing
    3. GET <links.document_metadata>                            -> available formats
    4. GET <links.document_metadata>/content  (Accept: pdf|xhtml) -> bytes
    5. write PDF (converting iXBRL -> PDF if needed)

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path) -> dict
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

import requests

import ocr_pdf


_TIMEOUT = 60


# ── Config ───────────────────────────────────────────────────────────
def _api_base() -> str:
    return (
        os.environ.get("COMPANIES_HOUSE_API_BASE")
        or "https://api.company-information.service.gov.uk"
    ).rstrip("/")


def _api_key() -> str:
    key = (
        os.environ.get("COMPANIES_HOUSE_API_KEY")
        or os.environ.get("Companies_House_API")
        or ""
    )
    key = key.strip().strip('"').strip("'").strip()
    if not key:
        raise RuntimeError(
            "COMPANIES_HOUSE_API_KEY is not set. Add a LIVE Companies House "
            "API key to your .env file."
        )
    return key


def _auth():
    return (_api_key(), "")


def _normalize_number(company_number: str) -> str:
    """Companies House numbers are 8 chars, often zero-padded (e.g. '445790'
    -> '00445790'). Accept either and pad numeric-only values to 8 digits."""
    n = (company_number or "").strip().upper()
    if not n:
        raise ValueError("Empty company number")
    if n.isdigit():
        n = n.zfill(8)
    return n


# ── Step 1+2: locate the latest accounts filing ──────────────────────
def _latest_accounts_filing(company_number: str, category: str) -> dict:
    url = f"{_api_base()}/company/{company_number}/filing-history"
    resp = requests.get(
        url,
        params={"category": category, "items_per_page": 100},
        auth=_auth(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json().get("items", []) or []

    # Keep only filings that expose a downloadable document.
    docs = [it for it in items if (it.get("links") or {}).get("document_metadata")]
    if not docs:
        raise LookupError(
            f"No downloadable '{category}' filings found for company "
            f"{company_number}."
        )

    # Most recent first by filing date.
    docs.sort(key=lambda it: it.get("date") or "", reverse=True)
    return docs[0]


# ── Step 3+4: download the document bytes ─────────────────────────────
def _download_document(document_metadata_url: str) -> tuple[bytes, str]:
    """Return (content_bytes, content_kind) where kind is 'pdf' or 'xhtml'."""
    # Inspect available formats first.
    meta_resp = requests.get(document_metadata_url, auth=_auth(), timeout=_TIMEOUT)
    meta_resp.raise_for_status()
    resources = (meta_resp.json() or {}).get("resources", {}) or {}

    has_pdf = "application/pdf" in resources
    has_xhtml = "application/xhtml+xml" in resources

    # Prefer PDF; fall back to iXBRL.
    if has_pdf:
        accept = "application/pdf"
        kind = "pdf"
    elif has_xhtml:
        accept = "application/xhtml+xml"
        kind = "xhtml"
    else:
        # Last resort: ask for PDF anyway (older filings may not list resources).
        accept = "application/pdf"
        kind = "pdf"

    content_url = document_metadata_url.rstrip("/") + "/content"
    # requests follows the 302 to S3 and drops the CH auth on the cross-host
    # redirect automatically, which is exactly what S3 expects.
    doc_resp = requests.get(
        content_url,
        headers={"Accept": accept},
        auth=_auth(),
        timeout=_TIMEOUT,
        allow_redirects=True,
    )
    doc_resp.raise_for_status()

    ctype = doc_resp.headers.get("Content-Type", "")
    if "xhtml" in ctype or "html" in ctype:
        kind = "xhtml"
    elif "pdf" in ctype:
        kind = "pdf"

    return doc_resp.content, kind


# ── iXBRL/XHTML -> PDF via headless Chromium (Playwright) ─────────────
def _html_to_pdf(html: str, out_pdf_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context().new_page()
            page.set_content(html, wait_until="load", timeout=120_000)
            page.pdf(
                path=str(out_pdf_path),
                format="A4",  # UK accounts are A4
                margin={"top": "0.5in", "bottom": "0.5in",
                        "left": "0.4in", "right": "0.4in"},
                print_background=True,
            )
        finally:
            browser.close()


def _write_pdf(content: bytes, kind: str, out_pdf_path: Path) -> None:
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "pdf":
        out_pdf_path.write_bytes(content)
        return
    # iXBRL / XHTML -> decode and render to PDF.
    try:
        html = content.decode("utf-8", errors="replace")
    except Exception:
        html = content.decode("latin-1", errors="replace")
    _html_to_pdf(html, out_pdf_path)


# ── Public API ────────────────────────────────────────────────────────
def fetch_filing_as_pdf(
    company_number: str,
    category: str = "accounts",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Fetch the latest `category` filing for a UK company, write a PDF,
    return metadata. Signature mirrors edgar_fetch.fetch_filing_as_pdf.

    Companies House serves most large-company accounts as **scanned, image-only
    PDFs** (no text layer). Since the detection pipeline is text-driven, we OCR
    such PDFs into a searchable PDF before returning. `ocr_progress(done, total)`
    is invoked while OCR runs (best-effort).
    """
    company_number = _normalize_number(company_number)
    category = (category or "accounts").strip().lower()

    filing = _latest_accounts_filing(company_number, category)
    document_metadata_url = (filing.get("links") or {}).get("document_metadata")

    content, kind = _download_document(document_metadata_url)

    out_pdf_path = Path(out_pdf_path)
    _write_pdf(content, kind, out_pdf_path)

    # Ensure a text layer exists (OCR image-only filings in place).
    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        # OCR is best-effort: a failure shouldn't abort the fetch. Detection may
        # still work if the PDF happened to carry some text.
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    return {
        "company_number": company_number,
        "company": company_name or company_number,
        "category": category,
        "form": (filing.get("type") or category),
        "filing_date": str(filing.get("date") or ""),
        "description": filing.get("description"),
        "transaction_id": filing.get("transaction_id"),
        "source_format": kind,
        "ocr": ocr_info,
        "url": (
            f"https://find-and-update.company-information.service.gov.uk/"
            f"company/{company_number}/filing-history"
        ),
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python companies_house_fetch.py 00445790
    import sys, json
    num = sys.argv[1] if len(sys.argv) > 1 else "00445790"
    info = fetch_filing_as_pdf(num, "accounts", f"_test_{num}.pdf")
    print(json.dumps(info, indent=2))

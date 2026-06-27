"""
Denmark (Erhvervsstyrelsen / CVR) Filing Fetcher
=================================================

The Danish equivalent of companies_house_fetch.py. Given a CVR number, fetches
the most recent annual report (årsrapport) from the Danish Business Authority's
free, public "offentliggørelser" (published financial statements) distribution
API, then materialises it as a PDF the existing extraction pipeline consumes.

Denmark serves annual reports in three shapes:
    * application/xhtml+xml  → ESEF / inline-XBRL (large listed companies) —
      rendered to PDF with headless Chromium (Playwright), like the UK iXBRL path
    * application/pdf        → smaller companies (written straight to disk)
    * image/tiff             → old scanned filings (converted to PDF, then OCR'd)

No API key is required (the offentliggørelser index is open; resolution via
cvrapi.dk is free). Endpoint:

    POST http://distribution.virk.dk/offentliggoerelser/_search

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

import requests

import ocr_pdf


_TIMEOUT = 90

# Annual-report document types, in order of detail preference. ESEF/XHTML and
# PDF are the human-readable annual reports; we ignore interim (DELAARSRAPPORT,
# HALVAARSRAPPORT) and the raw XBRL/zip data documents.
_ANNUAL_DOC_TYPES = {"AARSRAPPORT"}
# Preferred mime types, best first.
_MIME_PRIORITY = ["application/xhtml+xml", "application/pdf", "image/tiff", "image/tif"]


def _api_base() -> str:
    return (
        os.environ.get("CVR_DISTRIBUTION_BASE")
        or "http://distribution.virk.dk"
    ).rstrip("/")


def _user_agent() -> str:
    return os.environ.get(
        "CVRAPI_USER_AGENT",
        "Pavaki Options Extractor (contact@pavaki.local)",
    )


def _normalize_cvr(company_number: str) -> int:
    n = "".join(ch for ch in str(company_number or "") if ch.isdigit())
    if not n:
        raise ValueError(f"Invalid CVR number: {company_number!r}")
    return int(n)


# ── Step 1: locate the latest annual-report publication ───────────────
def _search_publications(cvr: int) -> list[dict]:
    url = f"{_api_base()}/offentliggoerelser/_search"
    body = {
        "query": {"term": {"cvrNummer": cvr}},
        "size": 30,
        "sort": [{"offentliggoerelsesTidspunkt": "desc"}],
    }
    resp = requests.post(
        url,
        data=json.dumps(body),
        headers={"User-Agent": _user_agent(), "Content-Type": "application/json"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    hits = (resp.json().get("hits") or {}).get("hits") or []
    return [h.get("_source") or {} for h in hits]


def _select_annual_document(publications: list[dict]) -> dict:
    """Pick the newest annual-report document, preferring richer formats.

    Returns a dict: {url, mime, doc_type, period_start, period_end, published}.
    Publications arrive newest-first; we keep that order and within each
    publication choose the best available mime type.
    """
    for src in publications:
        docs = src.get("dokumenter") or []
        annual = [d for d in docs if (d.get("dokumentType") or "").upper() in _ANNUAL_DOC_TYPES]
        if not annual:
            continue
        # Choose the richest available format for this (newest) annual filing.
        annual.sort(
            key=lambda d: _MIME_PRIORITY.index(d.get("dokumentMimeType"))
            if d.get("dokumentMimeType") in _MIME_PRIORITY else len(_MIME_PRIORITY)
        )
        chosen = annual[0]
        period = (src.get("regnskab") or {}).get("regnskabsperiode") or {}
        return {
            "url": chosen.get("dokumentUrl"),
            "mime": chosen.get("dokumentMimeType"),
            "doc_type": chosen.get("dokumentType"),
            "period_start": period.get("startDato"),
            "period_end": period.get("slutDato"),
            "published": src.get("offentliggoerelsesTidspunkt"),
        }
    raise LookupError("No annual report (AARSRAPPORT) found for this company.")


# ── Step 2: download the document bytes ───────────────────────────────
def _download(url: str) -> bytes:
    resp = requests.get(
        url,
        headers={"User-Agent": _user_agent()},
        timeout=_TIMEOUT,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content


# ── XHTML / iXBRL → PDF via headless Chromium (Playwright) ────────────
def _html_to_pdf(html_bytes: bytes, out_pdf_path: Path) -> None:
    """Render a (potentially very large, ~30 MB) ESEF XHTML to PDF.

    Unlike the UK path (which uses set_content on small iXBRL), Danish ESEF
    reports are large and self-contained, so we write the XHTML to a temp file
    and navigate to it via file:// — far more robust for big documents.
    """
    from playwright.sync_api import sync_playwright

    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "report.xhtml"
        src.write_bytes(html_bytes)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_context().new_page()
                page.goto(src.as_uri(), wait_until="load", timeout=180_000)
                page.pdf(
                    path=str(out_pdf_path),
                    format="A4",
                    margin={"top": "0.5in", "bottom": "0.5in",
                            "left": "0.4in", "right": "0.4in"},
                    print_background=True,
                )
            finally:
                browser.close()


def _tiff_to_pdf(content: bytes, out_pdf_path: Path) -> None:
    """Convert a (possibly multi-page) TIFF to PDF via PyMuPDF."""
    import fitz
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(stream=content, filetype="tif") as img:
        pdfbytes = img.convert_to_pdf()
    out_pdf_path.write_bytes(pdfbytes)


def _write_pdf(content: bytes, mime: str, out_pdf_path: Path) -> None:
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    mime = (mime or "").lower()
    if "pdf" in mime:
        out_pdf_path.write_bytes(content)
    elif "tif" in mime:
        _tiff_to_pdf(content, out_pdf_path)
    else:
        # xhtml / xml / html — render to PDF
        _html_to_pdf(content, out_pdf_path)


# ── Public API ────────────────────────────────────────────────────────
def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Fetch the latest annual report for a Danish company (by CVR), write a
    PDF, return metadata. Signature mirrors companies_house_fetch.fetch_filing_as_pdf.

    Old scanned filings (TIFF) and any image-only PDFs are OCR'd into a
    searchable PDF before returning, since the detection pipeline is text-driven.
    """
    cvr = _normalize_cvr(company_number)

    publications = _search_publications(cvr)
    if not publications:
        raise LookupError(f"No published filings found for CVR {cvr}.")
    sel = _select_annual_document(publications)

    content = _download(sel["url"])
    out_pdf_path = Path(out_pdf_path)
    _write_pdf(content, sel["mime"], out_pdf_path)

    # Ensure a text layer exists (OCR scanned/image-only filings in place).
    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    period = ""
    if sel.get("period_start") or sel.get("period_end"):
        period = f"{sel.get('period_start','?')} → {sel.get('period_end','?')}"

    return {
        "company_number": str(cvr),
        "company": company_name or str(cvr),
        "category": category,
        "form": sel.get("doc_type") or "AARSRAPPORT",
        "filing_date": str(sel.get("published") or "")[:10],
        "report_period": period,
        "source_format": sel.get("mime"),
        "ocr": ocr_info,
        "url": (
            f"https://datacvr.virk.dk/enhed/virksomhed/{cvr}"
            f"?fritekst={cvr}&sideIndex=0&size=10"
        ),
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python denmark_fetch.py 24256790
    import sys
    num = sys.argv[1] if len(sys.argv) > 1 else "24256790"
    info = fetch_filing_as_pdf(num, "annual", f"_test_dk_{num}.pdf")
    print(json.dumps(info, indent=2, ensure_ascii=False))

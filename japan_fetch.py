"""
Japan (FSA / EDINET) Filing Fetcher  (library-based)
====================================================

The Japanese equivalent of edgar_fetch.py / denmark_fetch.py. Given an EDINET
code (resolved by jp_resolve.py), fetches the most recent annual securities
report (有価証券報告書, docTypeCode 120) from EDINET and writes it as a PDF the
existing extraction pipeline consumes.

This module is now backed by the **`edinet-tools`** library
(https://github.com/matthelmer/edinet-tools) instead of the hand-rolled EDINET
API v2 transport. The library handles entity lookup, the date-scanned document
listing, and the document download. Key facts that shape this module:

  * EDINET still has NO search-by-company endpoint — the library lists an
    entity's filings by scanning calendar dates backward over a look-back
    window (`Entity.documents(doc_type="120", days=...)`), newest first.
  * Downloading the report PDF (`type=2`) requires a free EDINET
    **Subscription-Key** (env EDINET_API_KEY). The library reads the same env
    var. Entity lookup / listing do NOT need a key; only the download does.

EDINET serves annual reports as text-based PDFs, so OCR is rarely needed; we
still run ocr_pdf.ensure_searchable_pdf defensively (a no-op when a text layer
already exists), matching the UK/DK paths.

Public API (unchanged):
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

# edinet-tools pulls in numpy/pandas; pin OpenBLAS to a single thread before
# that import to avoid the known allocation crash in this venv. Harmless if the
# host already set it (e.g. the backend launches with OPENBLAS_NUM_THREADS=1).
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import ocr_pdf


_TRUSTSTORE_READY = False


def _ensure_truststore() -> None:
    """Make Python's SSL use the OS (Windows) certificate store.

    edinet-tools calls EDINET via bare urllib with default cert verification and
    no CA-bundle option. Behind a TLS-inspecting proxy / AV that injects its own
    root cert, the handshake otherwise fails with CERTIFICATE_VERIFY_FAILED.
    truststore patches the default SSL context to trust whatever the OS already
    trusts. Verification stays ON. Idempotent; only invoked on the Japan path."""
    global _TRUSTSTORE_READY
    if _TRUSTSTORE_READY:
        return
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass
    _TRUSTSTORE_READY = True


_ANNUAL_DOC_TYPE = "120"      # 有価証券報告書 — Annual Securities Report
_DEFAULT_LOOKBACK_DAYS = 400  # > 1 year, so the latest annual report is always caught
_PDF_DOC_TYPE = 2             # EDINET document download `type`: 2 = submitted PDF


def _api_key() -> str:
    key = os.environ.get("EDINET_API_KEY")
    if not key or key == "your_edinet_key_here":
        raise RuntimeError(
            "EDINET_API_KEY not set in .env — required to download the EDINET "
            "report PDF (the edinet-tools library still needs a key for the "
            "document download; entity lookup is key-free). "
            "Get a free key from the EDINET site (API registration)."
        )
    return key


def _select_latest_annual(documents: list) -> Any:
    """Pick the newest annual securities report from the entity's filings.

    `Entity.documents(doc_type="120", ...)` already filters to docTypeCode 120
    and returns newest-first, but we re-verify the type code and prefer the most
    recent by filing datetime to be robust to ordering changes."""
    candidates = [
        d for d in (documents or [])
        if (getattr(d, "doc_type_code", "") or "") == _ANNUAL_DOC_TYPE
    ]
    if not candidates:
        return None

    def _key(d):
        return getattr(d, "filing_datetime", None) or getattr(d, "period_end", None)

    # Sort newest-first; documents with no date sink to the bottom.
    candidates.sort(
        key=lambda d: (_key(d) is not None, _key(d)),
        reverse=True,
    )
    return candidates[0]


# ── Public API ────────────────────────────────────────────────────────
def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    scan_progress: Optional[Callable[[int, int], None]] = None,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Fetch the latest annual report for a Japanese company (by EDINET code),
    write a PDF, return metadata. Signature mirrors denmark_fetch.fetch_filing_as_pdf.

    `company_number` is the EDINET code (e.g. "E02144"). `scan_progress(done,
    total)` reports listing progress (best-effort — the library performs the
    date scan internally, so this is a coarse 0→1 signal); `ocr_progress`
    reports any defensive OCR.
    """
    edinet_code = (company_number or "").strip().upper()
    if not edinet_code:
        raise ValueError("company_number (EDINET code) is required.")

    # Fail fast with a clear message before doing any work if the download key
    # is missing (listing is key-free, but the download below is not).
    key = _api_key()

    # Lazy import: keeps backend startup light and defers the numpy/pandas load
    # until Japan is actually used.
    _ensure_truststore()
    import edinet_tools as et

    if scan_progress:
        try:
            scan_progress(0, 1)   # listing started
        except Exception:
            pass

    entity = et.entity_by_edinet_code(edinet_code)
    if entity is None:
        raise LookupError(f"EDINET code {edinet_code!r} not found in EDINET.")

    documents = entity.documents(doc_type=_ANNUAL_DOC_TYPE, days=lookback_days)

    if scan_progress:
        try:
            scan_progress(1, 1)   # listing complete
        except Exception:
            pass

    doc = _select_latest_annual(documents)
    if doc is None:
        raise LookupError(
            f"No annual securities report (有価証券報告書) found for EDINET code "
            f"{edinet_code!r} in the last {lookback_days} days."
        )

    # Download the submitted PDF (type=2). `api.fetch_document` reads
    # EDINET_API_KEY itself; we pass it explicitly to be safe.
    pdf_bytes = et.api.fetch_document(doc.doc_id, type=_PDF_DOC_TYPE, api_key=key)
    if not pdf_bytes:
        raise RuntimeError(
            f"EDINET returned no PDF content for document {doc.doc_id!r}."
        )

    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    out_pdf_path.write_bytes(pdf_bytes)

    # Ensure a text layer exists (no-op for EDINET's native text PDFs).
    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    period_start = getattr(doc, "period_start", None)
    period_end = getattr(doc, "period_end", None)
    period = ""
    if period_start or period_end:
        ps = period_start.strftime("%Y-%m-%d") if period_start else "?"
        pe = period_end.strftime("%Y-%m-%d") if period_end else "?"
        period = f"{ps} → {pe}"

    filing_dt = getattr(doc, "filing_datetime", None)
    sec_code = (getattr(doc, "securities_code", "") or "").strip() or None

    return {
        "company_number": edinet_code,
        "company": company_name or getattr(doc, "filer_name", None) or edinet_code,
        "category": category,
        "form": getattr(doc, "doc_type_name", None) or "Annual Securities Report (有価証券報告書)",
        "filing_date": filing_dt.strftime("%Y-%m-%d") if filing_dt else "",
        "report_period": period,
        "doc_id": doc.doc_id,
        "securities_code": sec_code,
        "source_format": "application/pdf",
        "ocr": ocr_info,
        # EDINET has no stable GET-by-docID public viewer URL; document is the
        # latest annual report for this EDINET code on the disclosure site.
        "url": "https://disclosure2.edinet-fsa.go.jp/",
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python japan_fetch.py E02144   (needs EDINET_API_KEY)
    import json
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else "E02144"

    def _p(done, total):
        print(f"  listing {done}/{total}", file=sys.stderr, end="\r")

    info = fetch_filing_as_pdf(code, "annual", f"_test_jp_{code}.pdf", scan_progress=_p)
    print(json.dumps(info, indent=2, ensure_ascii=False))

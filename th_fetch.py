"""
Thailand (SEC Thailand 56-1 One Report) Filing Fetcher
======================================================

Given a SEC Thailand 56-1 ZIP **file id** (resolved by th_resolve.py), downloads
the submission ZIP and extracts the **56-1 One Report** PDF — Thailand's annual
report, carrying the share-based-payment note (TFRS 2). Served free and
un-walled off the SEC's iDisc service (the SET exchange portal is bot-walled, so
we use the regulator).

Chain (plain HTTP via curl_cffi Chrome impersonation; verified 2026-06-06):
  1. `market.sec.or.th/public/idisc/Download?FILEID=<fileid>` -> a submission ZIP
     (e.g. contains ONEREPORT<SYM>E.PDF + STRUCTURE<SYM>E.PDF).
  2. Extract the largest PDF member — the One Report itself (the STRUCTURE file
     is a small group-structure chart) — and write it out.
  3. OCR defensively.

`<fileid>` ends in "E" for the English edition (preferred by the EN listing);
some smaller issuers file Thai only, so Thai SBC keywords are in keywords.py.

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from curl_cffi import requests as creq

import ocr_pdf

_DL_URL = "https://market.sec.or.th/public/idisc/Download?FILEID={fileid}"
_HTTP_TIMEOUT = 240


def _largest_pdf(zip_bytes: bytes) -> tuple[str, bytes]:
    """Return (member_name, pdf_bytes) for the largest .pdf in the ZIP — the
    One Report (vs the small group-structure chart)."""
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    pdfs = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
    if not pdfs:
        raise LookupError("SEC Thailand submission ZIP contained no PDF.")
    # Prefer an explicit One Report file; otherwise the largest PDF.
    one = [n for n in pdfs if "onereport" in n.lower() or "one report" in n.lower()]
    if one:
        best = max(one, key=lambda n: zf.getinfo(n).file_size)
    else:
        best = max(pdfs, key=lambda n: zf.getinfo(n).file_size)
    return best, zf.read(best)


def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
    fiscal_year: Optional[int] = None,
) -> dict[str, Any]:
    """Download + extract the latest SEC Thailand 56-1 One Report PDF.
    `company_number` is the SEC ZIP file id (e.g. 'dat/f56/0646ONE...E.zip')."""
    fileid = str(company_number or "").strip()
    if not fileid.lower().endswith(".zip"):
        raise ValueError(
            f"Thailand fetch needs a SEC 56-1 ZIP file id, got {company_number!r}."
        )

    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # 1) Download the submission ZIP.
    z = creq.get(_DL_URL.format(fileid=fileid), impersonate="chrome",
                 timeout=_HTTP_TIMEOUT, allow_redirects=True)
    if z.status_code != 200 or z.content[:2] != b"PK":
        raise RuntimeError(
            f"SEC Thailand 56-1 ZIP not downloadable (status {z.status_code}) "
            f"for file id {fileid!r}."
        )

    # 2) Extract the One Report PDF.
    member, pdf_bytes = _largest_pdf(z.content)
    if pdf_bytes[:4] != b"%PDF":
        raise RuntimeError(f"Extracted member {member!r} is not a PDF.")
    out_pdf_path.write_bytes(pdf_bytes)

    # 3) OCR defensively.
    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    return {
        "company_number": fileid,
        "company": company_name or fileid,
        "category": category,
        "form": f"SEC Thailand 56-1 One Report ({member})"[:120],
        "filing_date": "",
        "report_period": f"{fiscal_year}-12-31" if fiscal_year else "",
        "fiscal_year": fiscal_year,
        "source_format": "application/pdf",
        "ocr": ocr_info,
        "url": _DL_URL.format(fileid=fileid),
        "zip_member": member,
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    import json
    import sys
    import th_resolve
    arg = sys.argv[1] if len(sys.argv) > 1 else "PTT"
    if arg.lower().endswith(".zip"):
        fid, name, yr = arg, None, None
    else:
        r = th_resolve.resolve_company_number(arg, sys.argv[2] if len(sys.argv) > 2 else None)
        fid, name, yr = r["company_number"], r["title"], r.get("year")
        print("resolved:", r["title"], "->", fid)
    info = fetch_filing_as_pdf(fid, "annual", f"_test_th.pdf", company_name=name, fiscal_year=yr)
    print(json.dumps({k: v for k, v in info.items() if k != "ocr"},
                     indent=2, ensure_ascii=False))

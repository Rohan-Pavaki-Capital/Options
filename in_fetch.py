"""
India (BSE) Filing Fetcher
==========================

The Indian equivalent of hk_fetch.py / cn_fetch.py. Given a BSE scrip code
(resolved by in_resolve.py), fetches the issuer's most recent **Annual Report**
via BSE's public AnnualReport API and downloads it. Indian annual reports carry
the Employee Stock Option / share-based-payment note (IND AS 102) and are served
as native PDFs over HTTP. No API key, no bot wall.

Flow:
  * AnnualReport_New/w?scripcode=<code> returns the issuer's annual reports as a
    JSON "Table", newest first, each with a Year and a PDFDownload URL.
  * We take the newest row and download its PDF.

Some older Indian annual reports are scanned images, so OCR is run defensively
(a no-op when a text layer already exists).

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import requests

import ocr_pdf

_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnualReport_New/w"
_HTTP_TIMEOUT = 120
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OptionsExtractor",
    "Accept": "application/json",
    "Referer": "https://www.bseindia.com/",
}


def _list_annual_reports(scrip_code: str) -> list[dict[str, Any]]:
    """Return the issuer's annual-report rows (BSE returns them newest first)."""
    resp = requests.get(
        _API_URL, params={"scripcode": scrip_code},
        headers=_HEADERS, timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        return []
    try:
        return resp.json().get("Table") or []
    except Exception:
        return []


def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Download the latest BSE annual-report PDF for a scrip code. Signature
    mirrors the other fetchers so the pipeline branch stays uniform.

    `company_number` is the BSE scrip code (e.g. "500325").
    """
    scrip_code = str(company_number or "").strip()
    if not scrip_code:
        raise ValueError("company_number (BSE scrip code) is required.")

    rows = _list_annual_reports(scrip_code)
    rows = [r for r in rows if (r.get("PDFDownload") or "").strip()]
    if not rows:
        raise LookupError(
            f"No annual report found on BSE for scrip code {scrip_code!r}."
        )

    rec = rows[0]                                       # newest first
    url = (rec.get("PDFDownload") or "").strip()

    resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    if resp.content[:4] != b"%PDF":
        raise RuntimeError(f"BSE returned a non-PDF response for scrip {scrip_code!r}.")
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    out_pdf_path.write_bytes(resp.content)

    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    year = str(rec.get("Year") or "").strip()
    return {
        "company_number": scrip_code,
        "company": company_name or rec.get("scrip_name") or scrip_code,
        "category": category,
        "form": f"Annual Report (BSE) {year}".strip(),
        "filing_date": "",
        "report_period": f"{year}-03-31" if year else "",   # Indian FY ends 31 Mar
        "fiscal_year": year,
        "source_format": "application/pdf",
        "ocr": ocr_info,
        "url": url,
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python in_fetch.py 500325   (Reliance; no API key)
    import json
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else "500325"
    info = fetch_filing_as_pdf(code, "annual", f"_test_in_{code}.pdf")
    print(json.dumps({k: v for k, v in info.items() if k != "ocr"},
                     indent=2, ensure_ascii=False))

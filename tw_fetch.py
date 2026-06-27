"""
Taiwan (TWSE / MOPS) Filing Fetcher
===================================

The Taiwanese equivalent of kr_fetch.py / japan_fetch.py. Given a TWSE stock
code (resolved by tw_resolve.py), fetches the most recent **annual consolidated
financial statements** (合併財務報告 — the audited full-year report) from the
TWSE document service and writes it as a PDF the existing extraction pipeline
consumes.

WHY the annual financial report and not the glossy 年報: the share-based-payment
note (股份基礎給付 / 員工認股權) with its numeric tables lives in the audited
financial-statement notes. The standalone 年報 reproduces the same note amid
governance/business narrative; the financial report is the cleaner, primary
source and is reliably available through the open document endpoint.

TWSE/MOPS specifics that shape this module (no API key required):
  * Financial reports live in the TWSE electronic-document service
    (doc.twse.com.tw/server-java/t57sb01). The flow is three steps:
        step=1  → list the financial-report PDFs for a company + ROC year
                  (mtype='A'); filenames look like 202504_2330_AI1.pdf, where
                  the 6-digit prefix is YYYY + quarter and AI1 = Chinese report.
        step=9  → resolve a listed filename to its timestamped /pdf/... URL.
        GET     → download the actual PDF.
    Quarter "04" is the ANNUAL (full-year, audited) consolidated report — the
    one carrying the complete notes. The `year` query parameter is the ROC year
    (西元 - 1911), which maps to the filename's calendar year (ROC+1911). We
    scan ROC years newest-first and take the latest year that has a quarter-04
    (annual) report filed.

These PDFs are text-based, so OCR is rarely needed; we still run
ocr_pdf.ensure_searchable_pdf defensively (a no-op when a text layer exists).

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

import requests

import ocr_pdf

_DOC_BASE = "https://doc.twse.com.tw"
_T57_URL = _DOC_BASE + "/server-java/t57sb01"
_HTTP_TIMEOUT = 120
_UA = {"User-Agent": "Mozilla/5.0 (OptionsExtractor)", "Referer": _DOC_BASE + "/"}
_DEFAULT_LOOKBACK_YEARS = 3       # scan current ROC year back this many years
_ANNUAL_QUARTER = "04"            # filename quarter for the full-year (annual) report

# Financial-report filename type codes, in order of preference. AI1 = Chinese
# consolidated report (always present, full notes); AIA = English edition. We
# default to the Chinese report for determinism, matching the JP/KR CJK path.
_REPORT_CODES = ("AI1", "AIA", "AI3")


def _list_report_pdfs(co_id: str, roc_year: int) -> list[str]:
    """step=1: return financial-report PDF filenames for a company + ROC year."""
    resp = requests.post(
        _T57_URL,
        data={"step": "1", "colorchg": "1", "co_id": co_id,
              "year": str(roc_year), "seamon": "", "mtype": "A"},
        timeout=_HTTP_TIMEOUT, headers=_UA,
    )
    resp.encoding = "big5"
    if resp.status_code != 200:
        return []
    # Filenames look like 202504_2330_AI1.pdf  (YYYY + quarter + code)
    return re.findall(rf"\d{{6}}_{re.escape(co_id)}_[A-Z0-9]+\.pdf", resp.text)


def _pick_annual(filenames: list[str]) -> Optional[str]:
    """Choose the preferred quarter-04 (annual, full-year) report from a listing."""
    annual = [
        f for f in set(filenames)
        if f.split("_")[0][4:6] == _ANNUAL_QUARTER
        and any(f.split("_")[-1].startswith(c) for c in _REPORT_CODES)
    ]
    if not annual:
        return None

    def sort_key(fn: str):
        year = fn.split("_")[0][:4]               # YYYY
        code = fn.split("_")[-1].replace(".pdf", "")
        pref = next((i for i, c in enumerate(_REPORT_CODES)
                     if code.startswith(c)), len(_REPORT_CODES))
        return (year, -pref)                       # newest year, then preferred code
    return max(annual, key=sort_key)


def _resolve_pdf_url(co_id: str, filename: str) -> str:
    """step=9: resolve a listed filename to its real timestamped /pdf/... URL."""
    resp = requests.post(
        _T57_URL,
        data={"step": "9", "kind": "A", "co_id": co_id, "filename": filename},
        timeout=_HTTP_TIMEOUT, headers=_UA,
    )
    resp.encoding = "big5"
    m = re.search(r"href='(/pdf/[^']+\.pdf)'", resp.text)
    if not m:
        raise RuntimeError(f"Could not resolve download URL for {filename!r}.")
    return _DOC_BASE + m.group(1)


def _find_latest_annual(co_id: str, lookback_years: int) -> tuple[str, int, str]:
    """Scan ROC years newest-first; return (filename, fiscal_year, pdf_url).

    fiscal_year is parsed from the filename prefix (the calendar reporting year),
    not the ROC query parameter — the latest year may only have interim quarters
    filed, so we fall back to the most recent year that has a quarter-04 report.
    """
    current_roc = date.today().year - 1911
    for roc in range(current_roc, current_roc - lookback_years - 1, -1):
        best = _pick_annual(_list_report_pdfs(co_id, roc))
        if best:
            fiscal_year = int(best.split("_")[0][:4])
            return best, fiscal_year, _resolve_pdf_url(co_id, best)
    raise LookupError(
        f"No annual financial report (合併財務報告) found for TWSE code "
        f"{co_id!r} in the last {lookback_years + 1} fiscal years."
    )


# ── Public API ────────────────────────────────────────────────────────
def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    lookback_years: int = _DEFAULT_LOOKBACK_YEARS,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Fetch the latest annual report for a Taiwanese company (by TWSE stock
    code), write a PDF, return metadata. Signature mirrors kr_fetch.fetch_filing_as_pdf.

    `company_number` is the TWSE stock code (e.g. "2330").
    """
    co_id = (company_number or "").strip()
    if not co_id:
        raise ValueError("company_number (TWSE stock code) is required.")

    filename, fiscal_year, pdf_url = _find_latest_annual(co_id, lookback_years)

    resp = requests.get(pdf_url, timeout=_HTTP_TIMEOUT, headers=_UA)
    resp.raise_for_status()
    if resp.content[:4] != b"%PDF":
        raise RuntimeError(f"TWSE returned a non-PDF response for {filename!r}.")
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    out_pdf_path.write_bytes(resp.content)

    # Ensure a text layer exists (no-op for TWSE's native text PDFs).
    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    return {
        "company_number": co_id,
        "company": company_name or co_id,
        "category": category,
        "form": f"合併財務報告 (Annual Consolidated Financial Statements) FY{fiscal_year}",
        "filing_date": "",
        "report_period": f"{fiscal_year}-12-31",
        "fiscal_year": fiscal_year,
        "source_format": "application/pdf",
        "ocr": ocr_info,
        "url": pdf_url,
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python tw_fetch.py 2330   (no API key needed)
    import json
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else "2330"
    info = fetch_filing_as_pdf(code, "annual", f"_test_tw_{code}.pdf")
    print(json.dumps(info, indent=2, ensure_ascii=False))

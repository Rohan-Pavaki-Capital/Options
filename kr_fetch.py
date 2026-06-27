"""
Korea (FSS / DART) Filing Fetcher
==================================

The Korean equivalent of edgar_fetch.py / japan_fetch.py. Given a DART corp_code
(resolved by kr_resolve.py), fetches the most recent annual report (사업보고서)
from DART and writes it as a PDF the existing extraction pipeline consumes.

DART specifics that shape this module:
  * Driven by the dart-fss library against the live OpenDART API (needs a free
    DART_API_KEY). Unlike EDINET, DART's `list.json` supports search by
    corp_code + date-range + report type, so there is NO slow date-scan — we
    ask directly for the latest 사업보고서 (pblntf_detail_ty = "A001").
  * DART serves no PDF through the keyed API, but each filing has an official
    downloadable PDF on the disclosure site (dart.fss.or.kr). We download that
    when present; if not, we render the report's viewer HTML to PDF with
    headless Chromium (Playwright), mirroring the EDGAR path.

Korean annual-report PDFs are text-based, so OCR is rarely needed; we still run
ocr_pdf.ensure_searchable_pdf defensively (a no-op when a text layer exists).

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import ocr_pdf


_ANNUAL_DETAIL_TYPE = "A001"   # 사업보고서 — Annual Business Report
_DEFAULT_LOOKBACK_DAYS = 455   # > 1 year + buffer for late filers (FY-end + 90d)


def _api_key() -> str:
    key = os.environ.get("DART_API_KEY")
    if not key or key == "your_dart_key_here":
        raise RuntimeError(
            "DART_API_KEY not set in .env — required for OpenDART (Korea). "
            "Get a free key instantly at https://opendart.fss.or.kr (API registration)."
        )
    return key


# ── Step 1: find the latest annual report (사업보고서) ────────────────
def _latest_annual_report(corp_code: str, lookback_days: int):
    import dart_fss as dart

    dart.enable_spinner(False)   # avoid yaspin spinner crashing on non-UTF8 consoles
    dart.set_api_key(_api_key())

    end = date.today()
    bgn = end - timedelta(days=lookback_days)
    results = dart.filings.search(
        corp_code=corp_code,
        bgn_de=bgn.strftime("%Y%m%d"),
        end_de=end.strftime("%Y%m%d"),
        pblntf_detail_ty=_ANNUAL_DETAIL_TYPE,
        last_reprt_at="Y",     # final (amended) report only
        sort="date",
        sort_mth="desc",       # newest first
        page_count=10,
    )
    if results is None or len(results) == 0:
        raise LookupError(
            f"No annual report (사업보고서) found for corp_code {corp_code!r} "
            f"in the last {lookback_days} days."
        )
    return results[0]


# ── Step 2: materialise the report as a PDF ───────────────────────────
def _download_official_pdf(report, out_pdf_path: Path) -> bool:
    """Try DART's official PDF download for the report. Returns True on success."""
    try:
        pdfs = [
            f for f in (report.attached_files or [])
            if (f.filename or "").lower().endswith(".pdf")
        ]
    except Exception:
        return False
    if not pdfs:
        return False

    # Prefer the main business report; else the first PDF.
    chosen = next((f for f in pdfs if "사업보고서" in (f.filename or "")), pdfs[0])

    with tempfile.TemporaryDirectory() as tmp:
        try:
            res = chosen.download(path=tmp)
        except Exception:
            return False
        # dart-fss returns either a dict ({'full_path': ...}) or a path string.
        src = res.get("full_path") if isinstance(res, dict) else res
        if not src or not Path(src).exists():
            return False
        out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out_pdf_path)
    return out_pdf_path.exists() and out_pdf_path.stat().st_size > 0


def _render_pages_to_pdf(report, out_pdf_path: Path) -> None:
    """Fallback: concatenate the report's viewer pages (HTML) and render to PDF
    with headless Chromium — same approach as edgar_fetch."""
    from playwright.sync_api import sync_playwright

    parts: list[str] = []
    for page in report.pages:
        try:
            html = page.html
        except Exception:
            html = None
        if html:
            parts.append(f"<section>{html}</section>")
    if not parts:
        raise RuntimeError("DART report exposed no downloadable PDF and no HTML pages.")

    combined = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>body{font-family:'Malgun Gothic',sans-serif;font-size:10pt;}"
        "table{border-collapse:collapse;} td,th{border:1px solid #999;padding:2px;}"
        "section{page-break-after:always;}</style></head><body>"
        + "".join(parts) + "</body></html>"
    )

    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context().new_page()
            page.set_content(combined, wait_until="load", timeout=180_000)
            page.pdf(
                path=str(out_pdf_path),
                format="A4",
                margin={"top": "0.5in", "bottom": "0.5in",
                        "left": "0.4in", "right": "0.4in"},
                print_background=True,
            )
        finally:
            browser.close()


# ── Public API ────────────────────────────────────────────────────────
def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Fetch the latest annual report for a Korean company (by DART corp_code),
    write a PDF, return metadata. Signature mirrors denmark_fetch.fetch_filing_as_pdf.

    `company_number` is the DART corp_code (e.g. "00126380").
    """
    corp_code = (company_number or "").strip()
    if not corp_code:
        raise ValueError("company_number (DART corp_code) is required.")

    report = _latest_annual_report(corp_code, lookback_days)

    out_pdf_path = Path(out_pdf_path)
    if not _download_official_pdf(report, out_pdf_path):
        _render_pages_to_pdf(report, out_pdf_path)

    # Ensure a text layer exists (no-op for DART's native text PDFs).
    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    rcept_dt = getattr(report, "rcept_dt", "") or ""
    filing_date = (
        f"{rcept_dt[0:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}"
        if len(rcept_dt) == 8 else rcept_dt
    )
    rcept_no = getattr(report, "rcept_no", None)

    return {
        "company_number": corp_code,
        "company": company_name or getattr(report, "corp_name", None) or corp_code,
        "category": category,
        "form": getattr(report, "report_nm", None) or "사업보고서 (Annual Report)",
        "filing_date": filing_date,
        "report_period": "",
        "rcept_no": rcept_no,
        "stock_code": getattr(report, "stock_code", None) or None,
        "source_format": "application/pdf",
        "ocr": ocr_info,
        "url": (
            f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
            if rcept_no else "https://dart.fss.or.kr/"
        ),
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python kr_fetch.py 00126380   (needs DART_API_KEY)
    import json
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else "00126380"
    info = fetch_filing_as_pdf(code, "annual", f"_test_kr_{code}.pdf")
    print(json.dumps(info, indent=2, ensure_ascii=False))

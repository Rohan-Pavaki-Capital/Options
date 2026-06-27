"""
Hong Kong (HKEXnews) Filing Fetcher
===================================

The HK equivalent of br_fetch.py / kr_fetch.py. Given a HKEXnews stockId
(resolved by hk_resolve.py), finds the issuer's most recent **Annual Report** via
HKEXnews's public document-search servlet and downloads it. HK annual reports are
served as native, text-based PDFs, so — unlike EU/ESEF or DART — NO rendering is
needed; we download the PDF directly. No API key, no bot wall.

Flow:
  * titleSearchServlet.do?stockId=<id>&title=Annual%20Report&searchType=1 returns a
    JSON list of the issuer's annual-report filings, newest first, each with a
    FILE_LINK to the PDF under /listedco/listconews/...
  * We take the newest true "ANNUAL REPORT" (excluding summaries/interims) and
    download its PDF.

OCR is run defensively (no-op for HK's native text PDFs).

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

import ocr_pdf

_SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
_BASE = "https://www1.hkexnews.hk"
_UA = {"User-Agent": "Mozilla/5.0 (OptionsExtractor; +https://hkexnews.hk)"}
_TIMEOUT = 120


def _search_annual_reports(stock_id: str) -> list[dict[str, Any]]:
    """Return the issuer's annual-report filings (server-side title-filtered)."""
    params = {
        "sortDir": "0", "sortByOptions": "DateTime", "category": "0",
        "market": "SEHK", "stockId": str(stock_id), "documentType": "-1",
        "fromDate": "", "toDate": "", "title": "Annual Report",
        "searchType": "1", "t": "1", "lang": "en",
    }
    url = _SEARCH_URL + "?" + urllib.parse.urlencode(params)
    raw = urllib.request.urlopen(
        urllib.request.Request(url, headers=_UA), timeout=_TIMEOUT
    ).read().decode("utf-8", "replace")
    d = json.loads(raw)
    res = d.get("result")
    if isinstance(res, str):
        res = json.loads(res)
    return res or []


def _is_annual_report(title: str) -> bool:
    t = (title or "").lower()
    return ("annual report" in t
            and "summary" not in t
            and "interim" not in t
            and "circular" not in t)


def _year_from_title(title: str) -> str:
    m = re.search(r"(19|20)\d{2}", title or "")
    return m.group(0) if m else ""


def _latest_annual(stock_id: str) -> dict[str, Any]:
    rows = _search_annual_reports(stock_id)
    ars = [r for r in rows if _is_annual_report(r.get("TITLE"))]
    if not ars:
        raise LookupError(
            f"No Annual Report found on HKEXnews for stockId {stock_id!r}."
        )
    # titleSearchServlet returns newest-first (sortByOptions=DateTime, sortDir=0).
    return ars[0]


def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Download the latest HKEX annual report PDF for a stockId. Signature mirrors
    the other fetchers so the pipeline branch stays uniform.

    `company_number` is the HKEXnews stockId (e.g. "7609").
    """
    stock_id = str(company_number or "").strip()
    if not stock_id:
        raise ValueError("company_number (HKEXnews stockId) is required.")

    rec = _latest_annual(stock_id)
    link = (rec.get("FILE_LINK") or "").strip()
    if not link:
        raise RuntimeError("HKEXnews annual-report record had no FILE_LINK.")
    url = link if link.startswith("http") else _BASE + ("" if link.startswith("/") else "/") + link

    out_pdf_path = Path(out_pdf_path)
    data = urllib.request.urlopen(
        urllib.request.Request(url, headers=_UA), timeout=_TIMEOUT
    ).read()
    if data[:4] != b"%PDF":
        raise RuntimeError("HKEXnews download was not a PDF.")
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    out_pdf_path.write_bytes(data)

    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    title = rec.get("TITLE") or ""
    return {
        "company_number": stock_id,
        "company": company_name or rec.get("STOCK_NAME") or rec.get("LONG_TEXT") or stock_id,
        "category": category,
        "form": "Annual Report (HKEX)",
        "filing_date": rec.get("DATE_TIME", "") or "",
        "report_period": _year_from_title(title),
        "title": title,
        "source_format": "application/pdf",
        "ocr": ocr_info,
        "url": url,
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python hk_fetch.py 7609   (Tencent; no API key)
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "7609"
    info = fetch_filing_as_pdf(code, "annual", f"_test_hk_{code}.pdf")
    print(json.dumps(info, indent=2, ensure_ascii=False))

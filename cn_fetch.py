"""
China (CNINFO) Filing Fetcher
=============================

The Chinese equivalent of tw_fetch.py / hk_fetch.py. Given a 6-digit stock code
(resolved by cn_resolve.py), fetches the most recent **annual report**
(年度报告 — the audited full-year report carrying the share-based-payment /
股份支付 note) from CNINFO's public announcement service and writes it as a PDF
the existing extraction pipeline consumes. No API key, no bot wall.

Flow (CNINFO public endpoints):
  * topSearch (via cn_resolve.lookup_org_id) turns the stock code into the
    orgId the announcement query requires.
  * hisAnnouncement/query with category=category_ndbg_szsh; returns the
    issuer's annual-report announcements, newest first, each with an adjunctUrl
    pointing to a PDF under static.cninfo.com.cn.
  * We take the newest TRUE full annual report — excluding 摘要 (summary),
    取消/更正 (cancelled/corrected) and 英文版 (English edition; the Chinese
    full report is the primary source, matching our CJK keyword/translation path).

CNINFO PDFs are native text, so OCR is rarely needed; we still run
ocr_pdf.ensure_searchable_pdf defensively (a no-op when a text layer exists).

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

import requests

import ocr_pdf
from cn_resolve import lookup_org_id, _plate_column

_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_STATIC_BASE = "http://static.cninfo.com.cn/"
_ANNUAL_CATEGORY = "category_ndbg_szsh;"      # 年度报告 (annual report) category
_HTTP_TIMEOUT = 90
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OptionsExtractor",
    "Accept": "*/*",
    "Referer": "http://www.cninfo.com.cn/",
    "X-Requested-With": "XMLHttpRequest",
}


def _query_annual_reports(code: str, org_id: str) -> list[dict[str, Any]]:
    """Return the issuer's annual-report announcements (newest first)."""
    plate, column = _plate_column(code)
    resp = requests.post(
        _QUERY_URL,
        data={
            "pageNum": "1", "pageSize": "30", "column": column, "tabName": "fulltext",
            "plate": plate, "stock": f"{code},{org_id}", "searchkey": "",
            "category": _ANNUAL_CATEGORY, "seDate": "", "isHLtitle": "true",
        },
        headers=_HEADERS, timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        return []
    try:
        return resp.json().get("announcements") or []
    except Exception:
        return []


def _is_full_annual(title: str) -> bool:
    """True for a real full annual report; rejects summaries, corrections, English."""
    t = title or ""
    if "年度报告" not in t:
        return False
    bad = ("摘要", "取消", "更正", "补充", "英文", "(英文版)", "（英文版）", "已取消")
    return not any(b in t for b in bad)


def _year_from_title(title: str) -> str:
    m = re.search(r"(20\d{2})\s*年", title or "") or re.search(r"(20\d{2})", title or "")
    return m.group(1) if m else ""


def _pick_latest(anns: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Newest full annual report. The list is newest-first; fall back to any
    report whose title mentions 年度报告 if the strict filter finds nothing."""
    full = [a for a in anns if _is_full_annual(a.get("announcementTitle"))]
    pool = full or [a for a in anns if "年度报告" in (a.get("announcementTitle") or "")]
    return pool[0] if pool else None


def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    org_id: Optional[str] = None,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Download the latest CNINFO annual-report PDF for a stock code. Signature
    mirrors the other fetchers so the pipeline branch stays uniform.

    `company_number` is the 6-digit stock code (e.g. "600519"). `org_id` is the
    CNINFO orgId; if omitted it is resolved from the code (keeps the fetcher
    self-contained, like tw_fetch).
    """
    code = str(company_number or "").strip()
    if not code:
        raise ValueError("company_number (6-digit CNINFO stock code) is required.")

    org_id = (org_id or "").strip() or lookup_org_id(code)
    if not org_id:
        raise LookupError(f"Could not resolve a CNINFO orgId for stock code {code!r}.")

    anns = _query_annual_reports(code, org_id)
    rec = _pick_latest(anns)
    if rec is None:
        raise LookupError(
            f"No annual report (年度报告) found on CNINFO for stock code {code!r}."
        )

    adj = (rec.get("adjunctUrl") or "").strip()
    if not adj:
        raise RuntimeError("CNINFO annual-report record had no adjunctUrl.")
    url = adj if adj.startswith("http") else _STATIC_BASE + adj

    resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    if resp.content[:4] != b"%PDF":
        raise RuntimeError(f"CNINFO returned a non-PDF response for {adj!r}.")
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    out_pdf_path.write_bytes(resp.content)

    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    title = rec.get("announcementTitle") or ""
    fiscal_year = _year_from_title(title)
    return {
        "company_number": code,
        "company": company_name or rec.get("secName") or code,
        "category": category,
        "form": f"年度报告 (Annual Report) {fiscal_year}".strip(),
        "filing_date": rec.get("announcementTime", "") or "",
        "report_period": f"{fiscal_year}-12-31" if fiscal_year else "",
        "title": title,
        "source_format": "application/pdf",
        "ocr": ocr_info,
        "url": url,
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python cn_fetch.py 600519   (no API key needed)
    import json
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    info = fetch_filing_as_pdf(code, "annual", f"_test_cn_{code}.pdf")
    print(json.dumps({k: v for k, v in info.items() if k != "ocr"},
                     indent=2, ensure_ascii=False))

"""
Malaysia (Bursa Malaysia) Filing Fetcher
========================================

The Malaysian equivalent of in_fetch.py. Given a Bursa stock code (resolved by
my_resolve.py), fetches the issuer's most recent **Annual Report** filing and
downloads the PDF carrying the share-based-payment note (MFRS 2 — Bursa reports
are in English, so no extra keywords are needed). No API key, no bot wall.

Chain (all plain HTTP via curl_cffi Chrome impersonation; verified 2026-06-06):
  1. Announcement search
     `/api/v1/announcements/search?ann_type=company&company=<code>
      &keyword=annual report&category=AR,ARCO&sort_dir=desc`
     -> newest-first rows, each with an `ann_id` and a title
     (e.g. "Annual Report & CG Report - 2025").
  2. Attachment listing
     `disclosure.bursamalaysia.com/FileAccess/viewHtml?e=<ann_id>`
     -> the filing's attached files as
     `/FileAccess/apbursaweb/download?id=<fileid>&name=EA_DS_ATTACHMENTS`
     with their original filenames.
  3. Pick the best PDF: prefer the audited "Financial Statements" file (where
     the SBC note's roll-forward / valuation lives), else the Integrated Annual
     Report; reject CG / Sustainability / ESG attachments.
  4. Direct-download the binary off disclosure.bursamalaysia.com (NOT walled);
     OCR defensively.

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from curl_cffi import requests as creq

import ocr_pdf

_SEARCH_URL = ("https://www.bursamalaysia.com/api/v1/announcements/search"
               "?ann_type=company&company={code}&keyword={kw}"
               "&category=AR,ARCO&per_page=30&page=1&sort_dir=desc")
_VIEW_URL = "https://disclosure.bursamalaysia.com/FileAccess/viewHtml?e={ann_id}"
_DL_URL = ("https://disclosure.bursamalaysia.com/FileAccess/apbursaweb/download"
           "?id={fid}&name=EA_DS_ATTACHMENTS")
_HTTP_TIMEOUT = 180

# Attachment-filename scoring: the audited financial statements carry the SBC
# note (roll-forward + valuation); the glossy/integrated AR is a valid fallback;
# CG / sustainability / ESG attachments never carry the note.
_REJECT = ("corporate governance", "cg report", "sustainability", "environmental",
           "esg", "circular", "notice of", "proxy", "agm", "minutes")
_FS_HINTS = ("financial statements", "financial statement", "audited",
             "consolidated financial")
_AR_HINTS = ("annual report", "integrated ar", "integrated annual", "annual & ")


def _list_annual_announcements(code: str) -> list[tuple[str, str]]:
    """Return (ann_id, title) for the issuer's annual-report announcements,
    newest first. Empty list on failure."""
    url = _SEARCH_URL.format(code=code, kw=quote("annual report"))
    r = creq.get(url, impersonate="chrome", timeout=_HTTP_TIMEOUT)
    try:
        rows = (r.json() or {}).get("data") or []
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for row in rows:
        cell = row[3] if len(row) > 3 else ""
        m = re.search(r"ann_id=(\d+)[^>]*>\s*([^<]+?)\s*<", cell)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def _list_attachments(ann_id: str) -> list[tuple[str, str]]:
    """Return (fileid, filename) attachments for an announcement, in page
    order, from the FileAccess viewer."""
    html = creq.get(_VIEW_URL.format(ann_id=ann_id),
                    impersonate="chrome", timeout=_HTTP_TIMEOUT).text
    pairs = re.findall(
        r"download\?id=(\d+)&name=EA_DS_ATTACHMENTS[\"'][^>]*>([^<]*)<", html
    )
    return [(fid, name.strip()) for fid, name in pairs]


def _score_attachment(name: str) -> int:
    n = name.lower()
    if not n.endswith(".pdf"):
        return -1000
    if any(r in n for r in _REJECT):
        return -100
    s = 0
    if any(h in n for h in _FS_HINTS):
        s += 60                                     # audited statements: best
    if any(h in n for h in _AR_HINTS):
        s += 30                                     # integrated/annual report
    if "part 1" in n or "part i" in n:
        s += 5                                      # prefer the first volume
    return s


def _year_from_title(title: str) -> Optional[int]:
    yrs = [int(y) for y in re.findall(r"20\d{2}", title)]
    return max(yrs) if yrs else None


def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Download the latest Bursa Malaysia annual-report PDF for a stock code.
    Signature mirrors the other fetchers. `company_number` is the stock code."""
    code = str(company_number or "").strip()
    if not code.isdigit():
        raise ValueError(
            f"Malaysia fetch needs a numeric Bursa stock code, got {company_number!r}."
        )

    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # 1) Newest annual-report announcement.
    anns = _list_annual_announcements(code)
    if not anns:
        raise LookupError(
            f"No annual report announcement found on Bursa for stock code {code!r}."
        )

    # 2) Walk announcements newest-first until one yields a scoreable PDF.
    chosen: Optional[tuple[str, str, str, int]] = None     # (fileid, filename, title, year)
    for ann_id, title in anns:
        atts = _list_attachments(ann_id)
        scored = [(fid, fn, _score_attachment(fn)) for fid, fn in atts]
        scored = [s for s in scored if s[2] > -100]
        if not scored:
            continue
        scored.sort(key=lambda s: s[2], reverse=True)
        fid, fn, _ = scored[0]
        chosen = (fid, fn, title, _year_from_title(title) or 0)
        break

    if not chosen:
        raise LookupError(
            f"Found annual-report announcements for stock code {code!r} but no "
            f"downloadable financial/annual-report PDF attachment."
        )

    fileid, filename, ann_title, year = chosen

    # 3) Direct-download the PDF (disclosure.bursamalaysia.com is NOT walled).
    dl = creq.get(_DL_URL.format(fid=fileid), impersonate="chrome",
                  timeout=_HTTP_TIMEOUT, allow_redirects=True)
    if dl.status_code != 200 or dl.content[:4] != b"%PDF":
        raise RuntimeError(
            f"Bursa attachment not downloadable (status {dl.status_code}) for "
            f"stock {code!r}, file {filename!r}."
        )
    out_pdf_path.write_bytes(dl.content)

    # 4) OCR defensively.
    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    return {
        "company_number": code,
        "company": company_name or code,
        "category": category,
        "form": f"Bursa Malaysia Annual Report ({filename})"[:120],
        "filing_date": "",
        "report_period": f"{year}-12-31" if year else "",
        "fiscal_year": year or None,
        "source_format": "application/pdf",
        "ocr": ocr_info,
        "url": _DL_URL.format(fid=fileid),
        "attachment": filename,
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    import json
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "1155"
    info = fetch_filing_as_pdf(code, "annual", f"_test_my_{code}.pdf")
    print(json.dumps({k: v for k, v in info.items() if k != "ocr"},
                     indent=2, ensure_ascii=False))

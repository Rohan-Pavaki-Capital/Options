"""
Israel (TASE / MAYA) Filing Fetcher
====================================

Given a MAYA **companyId** (see il_resolve), pulls the latest **annual /
periodic financial statements** PDF — the report carrying the share-based-
payment note — and writes a PDF the existing extraction pipeline consumes.

WHY FIRECRAWL: TASE (maya.tase.co.il) fronts its disclosure data with an
Incapsula bot wall that rejects plain HTTP and headless browsers (Asia spike,
2026-06-03). Firecrawl's stealth proxy renders the listing *page* (verified
2026-06-04). The report **PDFs themselves live on mayafiles.tase.co.il, which
is NOT walled** — so we Firecrawl only the listing, then download the PDF
directly (fast, free of Firecrawl credits).

Chain:
    1. Firecrawl `companies/<id>/reports?eventsFamilyIds[]=100`  (family 100 =
       Financial Statements) -> list of [reportId, title], newest first.
    2. Pick the newest ANNUAL / PERIODIC statement (skip quarterly/interim/
       presentations/notices).
    3. The reportId IS the mayafiles document id; build the PDF URL
       `https://mayafiles.tase.co.il/rpdf/<lo>-<hi>/P<reportId>-00.pdf`
       (verified: reportId 1725859 -> P1725859-00.pdf, 7.4 MB).
    4. Direct-download the binary, OCR defensively.

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

import requests

import fc_client
import ocr_pdf

_LISTING = ("https://maya.tase.co.il/en/companies/{cid}/reports"
            "?eventsFamilyIds%5B%5D=100")
_MAYAFILES = "https://mayafiles.tase.co.il/rpdf/{lo}-{hi}/P{rid}-00.pdf"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# Title scoring: which report is the full-year statements (where the SBC note
# lives), not a quarterly/interim/ancillary doc.
_ANNUAL_HINTS = ("annual", "periodic", "yearly", "year ended", "31.12", "f20", "20-f", "10-k")
_FS_HINTS = ("financial statements", "consolidated financial", "financial report")
_REJECT = ("q1", "q2", "q3", "1st quarter", "2nd quarter", "3rd quarter",
           "quarterly", "interim", "semiannual", "semi-annual", "presentation",
           "conference", "notice", "immediate report", "webcast", "esg",
           "press release", "summary")


def _range(rid: int) -> tuple[int, int]:
    lo = ((rid - 1) // 1000) * 1000 + 1
    return lo, lo + 999


def _parse_reports(markdown: str) -> list[tuple[int, str]]:
    """Extract [(reportId, title)] in document order (newest first) from the
    Firecrawl markdown. Title links point at /reports/<id> WITHOUT an
    attachmentType query (those are the per-file links)."""
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for m in re.finditer(
        r"\[([^\]]+)\]\(https://maya\.tase\.co\.il/en/reports/(\d+)\)", markdown
    ):
        title, rid = m.group(1).strip(), int(m.group(2))
        if title.lower().startswith("a file of type"):
            continue
        if rid in seen:
            continue
        seen.add(rid)
        out.append((rid, title))
    return out


def _score(title: str) -> int:
    t = title.lower()
    if any(r in t for r in _REJECT):
        return -100
    s = 0
    if any(h in t for h in _ANNUAL_HINTS):
        s += 60
    if any(h in t for h in _FS_HINTS):
        s += 40
    # a 4-digit year present is a mild positive (dated statement)
    if re.search(r"20\d{2}", t):
        s += 10
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
    """Download the latest TASE/MAYA annual financial statements for a companyId.
    Signature mirrors the other fetchers. `company_number` is the MAYA companyId."""
    cid = str(company_number or "").strip()
    if not cid.isdigit():
        raise ValueError(
            f"Israel fetch needs a numeric MAYA companyId, got {company_number!r}."
        )

    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # 1) Firecrawl the financial-statements listing.
    data = fc_client.scrape(_LISTING.format(cid=cid),
                            formats=("markdown", "links"), wait_ms=8000)
    reports = _parse_reports(data.get("markdown") or "")
    if not reports:
        raise LookupError(
            f"No financial reports found on MAYA for companyId {cid!r} "
            f"(check the id at maya.tase.co.il/en/companies/{cid})."
        )

    # 2) Pick the best ANNUAL/PERIODIC statement. Listing is newest-first, so a
    #    stable max on (score, year) prefers a high-scoring recent annual report.
    scored = [(rid, title, _score(title), _year_from_title(title) or 0)
              for rid, title in reports]
    annual = [r for r in scored if r[2] > 0]
    if not annual:
        # fall back to the newest financial-statements-ish report at all
        annual = [r for r in scored if r[2] > -100] or scored
    annual.sort(key=lambda r: (r[3], r[2]), reverse=True)  # newest year, best score
    rid, title, _, year = annual[0]

    # 3) Build the mayafiles PDF URL and download directly (not walled).
    lo, hi = _range(rid)
    pdf_url = _MAYAFILES.format(lo=lo, hi=hi, rid=rid)
    r = requests.get(pdf_url, headers={"User-Agent": _UA,
                                       "Accept": "application/pdf,*/*"}, timeout=120)
    if r.status_code != 200 or r.content[:4] != b"%PDF":
        raise RuntimeError(
            f"MAYA report PDF not downloadable at {pdf_url} "
            f"(status {r.status_code}); reportId={rid} title={title!r}."
        )
    out_pdf_path.write_bytes(r.content)

    # 4) OCR defensively.
    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    return {
        "company_number": cid,
        "company": company_name or cid,
        "category": category,
        "form": f"TASE/MAYA Financial Statements ({title})"[:120],
        "filing_date": "",
        "report_period": f"{year}-12-31" if year else "",
        "fiscal_year": year or None,
        "source_format": "application/pdf",
        "ocr": ocr_info,
        "url": pdf_url,
        "report_id": rid,
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    import json
    import os
    import sys
    from pathlib import Path as _P
    for line in _P(".env").read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
    cid = sys.argv[1] if len(sys.argv) > 1 else "604"
    info = fetch_filing_as_pdf(cid, "annual", f"_test_il_{cid}.pdf")
    print(json.dumps({k: v for k, v in info.items() if k != "ocr"},
                     indent=2, ensure_ascii=False))

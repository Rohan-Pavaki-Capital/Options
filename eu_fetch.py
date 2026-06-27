"""
EU/EEA (ESEF) Filing Fetcher  —  pan-European, via filings.xbrl.org
====================================================================

The European equivalent of br_fetch.py / kr_fetch.py. Given an LEI (resolved by
eu_resolve.py), fetches the most recent ESEF Annual Financial Report from the
free filings.xbrl.org repository and writes it as a PDF the existing extraction
pipeline consumes.

ESEF specifics that shape this module (no API key required):
  * The filings.xbrl.org JSON:API lists every issuer's filings at
    /api/entities/<LEI>/filings. Each ESEF filing IS an Annual Financial Report
    (that is what the format mandates), so the latest by `period_end` is the
    latest annual report. Each row carries a `report_url` — the inline-XBRL
    (iXBRL) report document, a styled XHTML that renders as the full annual
    report (the share-based-payment note lives inside it).
  * ESEF reports are inline-XBRL (XHTML), NOT PDF. We therefore RENDER the report
    document to PDF with headless Chromium — the same approach edgar_fetch and
    kr_fetch already use for HTML filings — so the downstream Stage-1 keyword
    detection and Claude extraction run unchanged. We render the live report URL
    (so its package-relative CSS/images resolve over HTTP); if that fails we fall
    back to downloading the report package ZIP and rendering the extracted XHTML
    from disk via file://.

Rendered ESEF PDFs carry a text layer, so OCR is rarely needed; we still run
ocr_pdf.ensure_searchable_pdf defensively (a no-op when a text layer exists).

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import io
import json
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

import ocr_pdf

_SITE_BASE = "https://filings.xbrl.org"
_API_BASE = f"{_SITE_BASE}/api"
_HTTP_TIMEOUT = 180
_HEADERS = {
    "Accept": "application/vnd.api+json",
    "User-Agent": "Mozilla/5.0 (OptionsExtractor; +https://filings.xbrl.org)",
}


def _api_get(path: str, params: Optional[dict] = None) -> dict[str, Any]:
    url = f"{_API_BASE}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _abs_url(rel: str) -> str:
    """Resolve a JSON:API relative path (e.g. '/<LEI>/.../x.xhtml') to a URL."""
    rel = (rel or "").strip()
    if not rel:
        return ""
    if rel.startswith("http://") or rel.startswith("https://"):
        return rel.replace("http://", "https://")
    return f"{_SITE_BASE}/{rel.lstrip('/')}"


# ── Step 1: find the latest annual ESEF filing for an LEI ─────────────
def _latest_filing(lei: str) -> dict[str, Any]:
    """Return the attributes of the most recent ESEF filing for `lei`."""
    d = _api_get(f"entities/{lei}/filings",
                 {"page[size]": "50", "sort": "-period_end"})
    rows = d.get("data") or []
    if not rows:
        raise LookupError(
            f"No ESEF filing found on filings.xbrl.org for LEI {lei!r}. "
            f"The repository's coverage is partial; this issuer may not be "
            f"collected yet (the official ESAP superset opens July 2027)."
        )
    # Newest reporting period first; among the same period prefer the most
    # recently added, then the cleanest (fewest validation errors).
    def key(r: dict[str, Any]) -> tuple:
        a = r.get("attributes") or {}
        return (
            a.get("period_end") or "",
            a.get("date_added") or "",
            -int(a.get("error_count") or 0),
        )

    best = max(rows, key=key)
    return best.get("attributes") or {}


# ── Step 2: render the iXBRL report document to PDF ───────────────────
def _render_url_to_pdf(url: str, out_pdf_path: Path) -> None:
    """Render a live (X)HTML report URL to PDF with headless Chromium — same
    approach as edgar_fetch / kr_fetch._render_pages_to_pdf."""
    from playwright.sync_api import sync_playwright

    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context().new_page()
            page.goto(url, wait_until="load", timeout=_HTTP_TIMEOUT * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass  # large reports may never fully idle; the DOM is enough
            page.emulate_media(media="print")
            page.pdf(
                path=str(out_pdf_path),
                format="A4",
                margin={"top": "0.5in", "bottom": "0.5in",
                        "left": "0.4in", "right": "0.4in"},
                print_background=True,
            )
        finally:
            browser.close()


def _render_package_to_pdf(package_url: str, out_pdf_path: Path) -> None:
    """Fallback: download the ESEF report package ZIP, extract the main report
    (X)HTML, and render it from disk so package-relative assets resolve."""
    url = _abs_url(package_url)
    if not url:
        raise RuntimeError("ESEF filing has no report_url and no package_url.")
    # NB: the document/asset hosts reject the JSON:API Accept header (HTTP 406);
    # only the /api/* JSON endpoints want it. Use a plain Accept for downloads.
    dl_headers = {"Accept": "*/*", "User-Agent": _HEADERS["User-Agent"]}
    req = urllib.request.Request(url, headers=dl_headers)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        content = resp.read()

    zf = zipfile.ZipFile(io.BytesIO(content))
    # The inline-XBRL report lives under .../reports/<name>.xhtml|.html.
    reports = [
        n for n in zf.namelist()
        if "/reports/" in n.lower()
        and n.lower().endswith((".xhtml", ".html"))
        and "viewer" not in n.lower()
    ]
    if not reports:
        reports = [n for n in zf.namelist()
                   if n.lower().endswith((".xhtml", ".html"))
                   and "viewer" not in n.lower()]
    if not reports:
        raise RuntimeError("ESEF package contained no inline-XBRL report document.")

    main = max(reports, key=lambda n: zf.getinfo(n).file_size)
    extract_dir = out_pdf_path.parent / (out_pdf_path.stem + "_esef_pkg")
    zf.extractall(extract_dir)
    report_path = extract_dir / main
    _render_url_to_pdf(report_path.as_uri(), out_pdf_path)


# ── Public API ────────────────────────────────────────────────────────
def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Fetch the latest ESEF annual report for an EU/EEA company (by LEI), write a
    PDF, return metadata. Signature mirrors br_fetch.fetch_filing_as_pdf.

    `company_number` is the LEI (e.g. "529900D6BF99LW9R2E68").
    """
    lei = (company_number or "").strip().upper()
    if not lei:
        raise ValueError("company_number (LEI) is required.")

    filing = _latest_filing(lei)

    out_pdf_path = Path(out_pdf_path)
    report_url = _abs_url(filing.get("report_url", ""))
    try:
        if not report_url:
            raise RuntimeError("filing has no report_url")
        _render_url_to_pdf(report_url, out_pdf_path)
        if not (out_pdf_path.exists() and out_pdf_path.stat().st_size > 0):
            raise RuntimeError("rendered PDF was empty")
    except Exception:
        # Fall back to the downloadable report package.
        _render_package_to_pdf(filing.get("package_url", ""), out_pdf_path)

    # Ensure a text layer exists (no-op for rendered text PDFs).
    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    period = filing.get("period_end", "") or ""
    return {
        "company_number": lei,
        "company": company_name or lei,
        "category": category,
        "form": "ESEF Annual Financial Report",
        "filing_date": filing.get("date_added", "") or "",
        "report_period": period,
        "country": filing.get("country"),
        "lei": lei,
        "source_format": "application/xhtml+xml (inline XBRL) -> PDF",
        "ocr": ocr_info,
        "url": report_url or _abs_url(filing.get("package_url", "")),
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python eu_fetch.py 529900D6BF99LW9R2E68   (no API key)
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else "529900D6BF99LW9R2E68"
    info = fetch_filing_as_pdf(code, "annual", f"_test_eu_{code}.pdf")
    print(json.dumps(info, indent=2, ensure_ascii=False))

"""
Indonesia (IDX) Filing Fetcher
==============================

The Indonesian equivalent of kr_fetch.py — a **Playwright-based** fetcher,
because IDX (idx.co.id) fronts its open data with a Cloudflare challenge that
plain HTTP cannot pass but a real headless browser can (verified in the Asia
discovery spike). Given an IDX ticker code (kodeEmiten, e.g. "BBCA"), it pulls
the latest **audited annual financial statements** — the Laporan Keuangan
carrying the share-based-payment note (pembayaran berbasis saham) — and writes a
PDF the existing extraction pipeline consumes. No API key.

IDX is keyed by the ticker code directly (kodeEmiten), so NO separate resolver
is needed. The `GetFinancialReport` endpoint returns, per company-year, a set of
attachments. We prefer, in order:
    1. the English audited financial statements PDF (single file),
    2. the Indonesian audited financial statements PDF,
    3. the FinancialStatement-<year>-Tahunan-<code>.pdf,
    4. failing a single statements PDF, the annual-report PDF parts merged.

We scan recent years newest-first (periode=audit = full-year audited).

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

import fitz  # PyMuPDF, already a pipeline dependency

import ocr_pdf

_BASE = "https://www.idx.co.id"
_HOME = _BASE + "/en"
_REPORT_URL = (
    _BASE + "/primary/ListedCompany/GetFinancialReport"
    "?indexFrom=1&pageSize=12&year={year}&reportType=rdf&periode=audit"
    "&kodeEmiten={code}&lang=en"
)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_DEFAULT_LOOKBACK_YEARS = 3
_NAV_TIMEOUT = 60000
_CF_WAIT = 5            # seconds to let the Cloudflare challenge resolve


def _current_year() -> int:
    # Date.now()/datetime.now() are fine in normal modules; only workflow scripts
    # forbid them. We scan back from the most recently filed audited year.
    import datetime
    return datetime.date.today().year


def _read_json(page, url: str) -> dict[str, Any]:
    page.goto(url, timeout=_NAV_TIMEOUT)
    content = page.content()
    body = page.inner_text("pre") if "<pre" in content else page.evaluate("document.body.innerText")
    try:
        return json.loads(body)
    except Exception:
        return {}


def _score_pdf(name: str) -> int:
    """Rank a .pdf attachment: higher = more likely the COMPLETE audited
    financial statements (where the detailed share-based-payment note + its
    roll-forward table live).

    Key lesson (GOTO): IDX publishes a templated, XBRL-rendered
    `FinancialStatement-<year>-Tahunan-<code>.pdf` that is often CONDENSED and
    missing the detailed notes. The issuer's own complete audited statements —
    named freely, e.g. `LK <NAME> <date>.pdf` (Laporan Keuangan) or
    `Audited financial statements ... (English).pdf` — carry the full note and
    must be preferred over the template.
    """
    n = (name or "").lower()
    if "esg" in n:
        return -100
    # Cover letters / change-explanation memos, not the statements themselves.
    if n.startswith("surat") or "pengantar" in n or "penjelasan" in n:
        return -50

    is_template = bool(re.search(r"financialstatement-\d{4}-tahunan", n))
    score = 0
    if "audited" in n:
        score += 60
    if "laporan keuangan" in n or n.startswith("lk ") or " lk " in n:
        score += 55          # issuer's own complete audited statements (Laporan Keuangan)
    if "financial statement" in n and not is_template:
        score += 50          # a real audited-FS title (spaced), not the template
    if "(english)" in n or " english" in n:
        score += 15          # prefer the English edition when present
    if is_template:
        score += 25          # valid fallback, but ranked BELOW any complete audited set
    if re.search(r"annualreport", n):
        score += 5           # full annual report also carries the note (often split)
    return score


def _pick_attachments(atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the attachment(s) to download. A single complete statements PDF if
    one exists; otherwise the annual-report PDF parts (att1..N) to be merged."""
    pdfs = [a for a in atts if str(a.get("File_Type", "")).lower() == ".pdf"
            and str(a.get("File_Path", "")).strip()]
    if not pdfs:
        return []
    scored = sorted(pdfs, key=lambda a: _score_pdf(a.get("File_Name", "")), reverse=True)
    best = scored[0]
    # Use the single statements PDF when one looks like a statements doc at all
    # (complete audited set, or — failing that — the IDX template, score >= 25).
    if _score_pdf(best.get("File_Name", "")) >= 25:
        return [best]
    # Otherwise gather the AnnualReport parts (att1, att2, ...) in order, to merge.
    parts = [a for a in pdfs if re.search(r"annualreport", a.get("File_Name", ""), re.I)]
    if parts:
        def _part_no(a):
            m = re.search(r"att(\d+)", a.get("File_Name", ""), re.I)
            return int(m.group(1)) if m else 0
        return sorted(parts, key=_part_no)
    return [best]


def _abs_url(path: str) -> str:
    if path.startswith("http"):
        return path
    from urllib.parse import quote
    return _BASE + quote(path, safe="/():.,_-+ ")


def _find_latest(page, code: str, lookback: int) -> tuple[int, list[dict[str, Any]], str]:
    """Scan recent years newest-first; return (year, attachments, company_name)."""
    cur = _current_year()
    for year in range(cur, cur - lookback - 1, -1):
        data = _read_json(page, _REPORT_URL.format(year=year, code=code))
        results = data.get("Results") or []
        if results:
            r0 = results[0]
            atts = r0.get("Attachments") or []
            if any(str(a.get("File_Type", "")).lower() == ".pdf" for a in atts):
                return year, atts, (r0.get("NamaEmiten") or code)
    raise LookupError(
        f"No audited annual financial statements found on IDX for ticker {code!r} "
        f"in the last {lookback + 1} years."
    )


def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    lookback_years: int = _DEFAULT_LOOKBACK_YEARS,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Download the latest IDX audited annual financial statements for a ticker
    code (kodeEmiten). Signature mirrors the other fetchers.

    `company_number` is the IDX ticker code (e.g. "BBCA").
    """
    from playwright.sync_api import sync_playwright

    code = str(company_number or "").strip().upper()
    if not code:
        raise ValueError("company_number (IDX ticker code, e.g. BBCA) is required.")

    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=_UA, accept_downloads=True)
        page = ctx.new_page()
        try:
            # Warm the homepage so Cloudflare issues a clearance cookie.
            page.goto(_HOME, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
            time.sleep(_CF_WAIT)

            year, atts, name = _find_latest(page, code, lookback_years)
            picks = _pick_attachments(atts)
            if not picks:
                raise LookupError(f"No downloadable PDF attachment for IDX ticker {code!r}.")

            pdf_bytes: list[bytes] = []
            src_urls: list[str] = []
            for a in picks:
                url = _abs_url(a["File_Path"])
                resp = ctx.request.get(url, timeout=_NAV_TIMEOUT)
                data = resp.body()
                if data[:4] != b"%PDF":
                    continue
                pdf_bytes.append(data)
                src_urls.append(url)
            if not pdf_bytes:
                raise RuntimeError(f"IDX returned no valid PDF for ticker {code!r}.")
        finally:
            browser.close()

    # One part → write directly; multiple parts → merge into a single PDF.
    if len(pdf_bytes) == 1:
        out_pdf_path.write_bytes(pdf_bytes[0])
    else:
        merged = fitz.open()
        for data in pdf_bytes:
            with fitz.open(stream=data, filetype="pdf") as d:
                merged.insert_pdf(d)
        merged.save(str(out_pdf_path))
        merged.close()

    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    return {
        "company_number": code,
        "company": company_name or name or code,
        "category": category,
        "form": f"Audited Annual Financial Statements (IDX) FY{year}",
        "filing_date": "",
        "report_period": f"{year}-12-31",
        "fiscal_year": year,
        "source_format": "application/pdf",
        "ocr": ocr_info,
        "url": src_urls[0] if src_urls else "",
        "parts": len(pdf_bytes),
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python id_fetch.py BBCA   (no API key needed)
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "BBCA"
    info = fetch_filing_as_pdf(code, "annual", f"_test_id_{code}.pdf")
    print(json.dumps({k: v for k, v in info.items() if k != "ocr"},
                     indent=2, ensure_ascii=False))

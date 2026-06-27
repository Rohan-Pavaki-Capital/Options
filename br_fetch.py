"""
Brazil (CVM) Filing Fetcher
============================

The Brazilian equivalent of kr_fetch.py / japan_fetch.py. Given a CVM code
(resolved by br_resolve.py), fetches the most recent annual financial-statements
filing — the **DFP** (Demonstrações Financeiras Padronizadas) — from CVM's free
open-data service and writes it as a PDF the existing extraction pipeline
consumes.

CVM specifics that shape this module (no API key required):
  * The annual DFP filings are indexed in CVM's open-data ZIP
    `dfp_cia_aberta_<year>.zip` (the head CSV `dfp_cia_aberta_<year>.csv` lists
    every filing with its CD_CVM, DT_REFER, VERSAO and a LINK_DOC download URL).
    The ZIP is named by the **fiscal year** (DT_REFER year), so the FY2025
    report lives in the 2025 ZIP. We scan from the current year backwards and
    take the latest filing (newest DT_REFER, highest VERSAO).
  * LINK_DOC points at CVM's RAD repository (frmDownloadDocumento), which serves
    the full submission **package** as a ZIP. Modern packages embed the complete
    report as a single PDF (the `<seq>_<cdcvm>_<ts>.pdf` member) including the
    notas explicativas — which is where the share-based-payment note lives. We
    extract that PDF directly; no XBRL rendering needed.

DFP PDFs are text-based, so OCR is rarely needed; we still run
ocr_pdf.ensure_searchable_pdf defensively (a no-op when a text layer exists).

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, ...) -> dict
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

import requests

import ocr_pdf

_DFP_ZIP_URL = (
    "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{year}.zip"
)
_CACHE_DIR = Path(__file__).parent / ".cache"
_HTTP_TIMEOUT = 120
_UA = {"User-Agent": "Mozilla/5.0 (OptionsExtractor; +https://cvm.gov.br)"}
_DEFAULT_LOOKBACK_YEARS = 3      # scan current FY back this many years for a DFP


def _digits(s: str) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


# ── Step 1: find the latest annual DFP filing for a CVM code ──────────
def _dfp_index_for_year(year: int) -> list[dict[str, str]]:
    """Download (disk-cached) the DFP index ZIP for a fiscal year and return the
    parsed head-CSV rows. Empty list if that year has no dataset yet."""
    cache = _CACHE_DIR / f"dfp_cia_aberta_{year}.csv"
    if cache.exists():
        text = cache.read_text(encoding="latin-1")
    else:
        try:
            resp = requests.get(_DFP_ZIP_URL.format(year=year),
                                timeout=_HTTP_TIMEOUT, headers=_UA)
        except Exception:
            return []
        if resp.status_code != 200 or not resp.content:
            return []
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
            text = zf.read(f"dfp_cia_aberta_{year}.csv").decode("latin-1")
        except Exception:
            return []
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="latin-1")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


def _latest_dfp_filing(cvm_code: str, lookback_years: int) -> dict[str, str]:
    """Return the most recent DFP head-row for `cvm_code` across recent years."""
    want = str(int(_digits(cvm_code) or "0"))
    best: Optional[dict[str, str]] = None
    this_year = date.today().year
    for year in range(this_year, this_year - lookback_years - 1, -1):
        rows = _dfp_index_for_year(year)
        mine = [r for r in rows
                if str(int(_digits(r.get("CD_CVM") or "0"))) == want]
        if not mine:
            continue
        # newest fiscal reference date, then highest amendment version
        cand = max(mine, key=lambda r: (r.get("DT_REFER", ""),
                                        int(_digits(r.get("VERSAO")) or "0")))
        if best is None or (cand.get("DT_REFER", ""), int(_digits(cand.get("VERSAO")) or "0")) > \
                           (best.get("DT_REFER", ""), int(_digits(best.get("VERSAO")) or "0")):
            best = cand
        # the first (newest) year with a filing wins — stop scanning older years
        break
    if best is None:
        raise LookupError(
            f"No annual DFP filing found for CVM code {cvm_code!r} in the last "
            f"{lookback_years + 1} fiscal years."
        )
    return best


# ── Step 2: materialise the filing as a PDF ───────────────────────────
def _download_report_pdf(link_doc: str, out_pdf_path: Path) -> None:
    """Download the RAD submission package and extract its embedded report PDF."""
    url = (link_doc or "").replace("http://", "https://").strip()
    if not url:
        raise RuntimeError("DFP filing has no LINK_DOC download URL.")
    resp = requests.get(url, timeout=_HTTP_TIMEOUT, headers=_UA)
    resp.raise_for_status()

    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
    except zipfile.BadZipFile:
        # Some packages stream the PDF directly rather than a ZIP.
        if resp.content[:4] == b"%PDF":
            out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
            out_pdf_path.write_bytes(resp.content)
            return
        raise RuntimeError("CVM document download was neither a ZIP nor a PDF.")

    pdfs = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
    if not pdfs:
        raise RuntimeError(
            "CVM DFP package contained no PDF (older XBRL-only filing); "
            "share-based-comp notes are not extractable from this filing."
        )
    # Prefer the largest PDF (the full report, not a cover sheet).
    chosen = max(pdfs, key=lambda n: zf.getinfo(n).file_size)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    out_pdf_path.write_bytes(zf.read(chosen))


# ── Public API ────────────────────────────────────────────────────────
def fetch_filing_as_pdf(
    company_number: str,
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    lookback_years: int = _DEFAULT_LOOKBACK_YEARS,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Fetch the latest annual DFP for a Brazilian company (by CVM code), write a
    PDF, return metadata. Signature mirrors kr_fetch.fetch_filing_as_pdf.

    `company_number` is the CVM code (e.g. "9512").
    """
    cvm_code = (company_number or "").strip()
    if not cvm_code:
        raise ValueError("company_number (CVM code) is required.")

    filing = _latest_dfp_filing(cvm_code, lookback_years)

    out_pdf_path = Path(out_pdf_path)
    _download_report_pdf(filing.get("LINK_DOC", ""), out_pdf_path)

    # Ensure a text layer exists (no-op for CVM's native text PDFs).
    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    dt_refer = filing.get("DT_REFER", "") or ""
    return {
        "company_number": cvm_code,
        "company": company_name or filing.get("DENOM_CIA") or cvm_code,
        "category": category,
        "form": "DFP (Demonstrações Financeiras Padronizadas — Annual Report)",
        "filing_date": filing.get("DT_RECEB", "") or "",
        "report_period": dt_refer,
        "cnpj": filing.get("CNPJ_CIA"),
        "version": filing.get("VERSAO"),
        "source_format": "application/pdf",
        "ocr": ocr_info,
        "url": filing.get("LINK_DOC", "").replace("http://", "https://"),
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python br_fetch.py 9512   (no API key needed)
    import json
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else "9512"
    info = fetch_filing_as_pdf(code, "annual", f"_test_br_{code}.pdf")
    print(json.dumps(info, indent=2, ensure_ascii=False))

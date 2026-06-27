"""
FastAPI Backend for Pavaki Options Extractor
==================================================

Wires the existing extraction pipeline (options.py, Anthropic/, format/,
database/) behind an HTTP API used by the React frontend.

Endpoints:
    POST   /api/extract            - Upload PDF, return job_id, start async run
    GET    /api/job/{job_id}       - Poll job status + progress
    GET    /api/result/{job_id}    - Final JSON result
    GET    /api/download/{job_id}/excel  - Download Excel file
    DELETE /api/job/{job_id}       - Cancel/delete a job
    GET    /api/health             - Health check
    GET    /api/jobs               - List jobs (debug)

Run:
    uvicorn backend:app --reload --port 8000
"""

import json
import os
import re
import shutil
import sys
import time
import uuid
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import cache

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Pipeline modules (live at project root) ──────────────────────────
from options import (
    detect_relevant_pages,
    extract_text_from_pages,
    rasterize_pages,
    CostTracker,
)
from Anthropic import (
    extract_with_claude,
    validate_all_plans,
    validate_final_output,
    merge_results,
    set_verbose as _set_anthropic_verbose,
)
from format.json_to_excel import build_workbook
from database.storage import save_extraction
from edgar_fetch import fetch_filing_as_pdf
from companies_house_fetch import fetch_filing_as_pdf as fetch_uk_filing_as_pdf
from uk_resolve import resolve_company_number
from denmark_fetch import fetch_filing_as_pdf as fetch_dk_filing_as_pdf
from dk_resolve import resolve_company_number as resolve_dk_company_number
from japan_fetch import fetch_filing_as_pdf as fetch_jp_filing_as_pdf
from jp_resolve import resolve_company_number as resolve_jp_company_number
from kr_fetch import fetch_filing_as_pdf as fetch_kr_filing_as_pdf
from kr_resolve import resolve_company_number as resolve_kr_company_number
from br_fetch import fetch_filing_as_pdf as fetch_br_filing_as_pdf
from br_resolve import resolve_company_number as resolve_br_company_number
from tw_fetch import fetch_filing_as_pdf as fetch_tw_filing_as_pdf
from tw_resolve import resolve_company_number as resolve_tw_company_number
from eu_fetch import fetch_filing_as_pdf as fetch_eu_filing_as_pdf
from eu_resolve import (
    resolve_company_number as resolve_eu_company_number,
    search_companies as search_eu_companies,
)
from ca_fetch import fetch_filing_as_pdf as fetch_ca_filing_as_pdf
from cn_fetch import fetch_filing_as_pdf as fetch_cn_filing_as_pdf
from cn_resolve import resolve_company_number as resolve_cn_company_number
from in_fetch import fetch_filing_as_pdf as fetch_in_filing_as_pdf
from in_resolve import resolve_company_number as resolve_in_company_number
from hk_fetch import fetch_filing_as_pdf as fetch_hk_filing_as_pdf
from hk_resolve import resolve_company_number as resolve_hk_company_number
from id_fetch import fetch_filing_as_pdf as fetch_id_filing_as_pdf
from il_fetch import fetch_filing_as_pdf as fetch_il_filing_as_pdf
from il_resolve import resolve_company_number as resolve_il_company_number
from my_fetch import fetch_filing_as_pdf as fetch_my_filing_as_pdf
from my_resolve import resolve_company_number as resolve_my_company_number
from th_fetch import fetch_filing_as_pdf as fetch_th_filing_as_pdf
from th_resolve import resolve_company_number as resolve_th_company_number
import diamond_route
import fc_client
import gurufocus

import anthropic
from openai import OpenAI


# ═════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB

# EU/EEA tab: try the universal IR scraper first, but give up on it after this
# many seconds and fall back to the authoritative ESEF (filings.xbrl.org) path.
EU_IR_SCRAPER_TIMEOUT_SEC = 100

JOBS: dict[str, dict] = {}


# ═════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═════════════════════════════════════════════════════════════════════

class JobStatus(BaseModel):
    job_id: str
    status: str
    filename: str
    file_size: int
    created_at: str
    updated_at: str
    progress: int
    current_stage: Optional[str] = None
    stages: dict = {}
    elapsed_seconds: float = 0
    estimated_remaining: Optional[float] = None
    cost_so_far: float = 0
    error: Optional[str] = None
    result_available: bool = False
    extraction_id: Optional[int] = None


# ═════════════════════════════════════════════════════════════════════
# JOB MANAGEMENT
# ═════════════════════════════════════════════════════════════════════

def create_job(
    filename: str,
    file_size: int,
    source: str = "upload",
    source_meta: Optional[dict] = None,
) -> str:
    job_id = str(uuid.uuid4())[:8]
    now = datetime.utcnow().isoformat()

    # Neutralize characters Windows forbids in filenames — notably the ":" in
    # exchange-prefixed tickers (e.g. "XTER:BMW" from a GuruFocus link). An
    # unsanitized colon makes NTFS write the PDF/xlsx into an alternate data
    # stream ("XTER" + ":BMW…"), which the download glob can't see → the Excel
    # download 404s ("Excel file not found"). Preserve the extension/structure;
    # only replace the illegal chars (any source, including uploads).
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", filename)

    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    stages: dict[str, dict] = {}
    if source == "edgar":
        stages["edgar_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "companies_house":
        stages["ch_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "denmark":
        stages["dk_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "japan":
        stages["jp_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "korea":
        stages["kr_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "brazil":
        stages["br_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "taiwan":
        stages["tw_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "eu":
        stages["eu_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "germany":
        stages["de_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "canada":
        stages["ca_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "china":
        stages["cn_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "india":
        stages["in_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "hongkong":
        stages["hk_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "indonesia":
        stages["id_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "israel":
        stages["il_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "malaysia":
        stages["my_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "thailand":
        stages["th_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "diamond":
        stages["diamond_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    elif source == "scrape_test":
        # Scraper-only TESTING job: fetch the PDF and stop — no LLM stages.
        stages["scrape_fetch"] = {"status": "pending", "duration": None, "cost": 0}
    else:
        stages["upload"] = {"status": "completed", "duration": 0, "cost": 0}
    if source != "scrape_test":
        stages.update({
            "stage1_keywords": {"status": "pending", "duration": None, "cost": 0},
            "stage2_classifier": {"status": "pending", "duration": None, "cost": 0},
            "stage3_extraction": {"status": "pending", "duration": None, "cost": 0},
            "validation": {"status": "pending", "duration": None, "cost": 0},
            "excel_generation": {"status": "pending", "duration": None, "cost": 0},
        })

    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "file_size": file_size,
        "created_at": now,
        "updated_at": now,
        "progress": 0,
        "current_stage": None,
        "source": source,
        "source_meta": source_meta or {},
        "stages": stages,
        "elapsed_seconds": 0,
        "estimated_remaining": 20.0,
        "cost_so_far": 0,
        "start_time": time.time(),
        "result_available": False,
        "extraction_id": None,
    }
    return job_id


def update_job(job_id: str, **updates):
    if job_id not in JOBS:
        return
    JOBS[job_id].update(updates)
    JOBS[job_id]["updated_at"] = datetime.utcnow().isoformat()
    JOBS[job_id]["elapsed_seconds"] = time.time() - JOBS[job_id]["start_time"]


def get_job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _safe_filename_part(text: str, limit: int = 40) -> str:
    """Make a string safe to use as part of a filename on all platforms.
    Tickers can arrive exchange-prefixed (e.g. "OTCPK:SUBCY"); on Windows the
    colon would create an NTFS alternate data stream instead of a real file,
    leaving the PDF/xlsx invisible to the download glob. Replace every char
    Windows forbids ( < > : " / \\ | ? * ) plus whitespace and control chars."""
    cleaned = re.sub(r'[<>:"/\\|?*\s]+', "-", text).strip("-. ")
    return cleaned[:limit] or "report"


def _mark_stage(job_id: str, stage_key: str, duration: float, cost: float, details: str):
    JOBS[job_id]["stages"][stage_key] = {
        "status": "completed",
        "duration": duration,
        "cost": cost,
        "details": details,
    }


def classify_failure(job: dict, exc: Exception) -> tuple[str, dict]:
    """Map a raw pipeline exception to a stable, user-neutral error code + the
    minimal context the frontend needs to show a friendly message. The raw text is
    NEVER shown to the user — only logged. Codes:
      NO_PAGES  — report processed but contains no share-based-payment note
      CONFIG    — API key / env / config problem (-> "contact your developer")
      NOT_FOUND — ticker/company not found in an exchange registry
      NO_REPORT — could not locate/scrape an annual report for the company
      UNKNOWN   — anything else (generic friendly message)
    Context carries {company, ticker, year} for the message (any may be empty)."""
    msg = str(exc or "")
    low = msg.lower()
    meta = (job.get("source_meta") or {}) if isinstance(job, dict) else {}

    company = (meta.get("company") or meta.get("company_name") or "").strip()
    ticker = (meta.get("ticker") or "").strip()
    if ":" in ticker:                       # drop an exchange prefix (e.g. "SGX:Z77")
        ticker = ticker.split(":")[-1].strip()
    year = ""
    for k in ("report_period", "fiscal_year", "report_year", "year", "period"):
        v = meta.get(k)
        if v:
            year = str(v)
            break
    ctx = {"company": company, "ticker": ticker, "year": year}

    if "no relevant pages" in low:
        code = "NO_PAGES"
    elif ("anthropic_api_key" in low or "together_api_key" in low
          or "api key" in low or "api_key" in low):
        code = "CONFIG"
    elif "could not find a" in low and "company for" in low:
        # registry miss: "Could not find a <Market> company for '<input>'" (note the
        # distinct "company for" — avoids matching "could not find an annual report
        # on this company's IR site", which is a NO_REPORT case below).
        code = "NOT_FOUND"
    elif ("could not confidently identify" in low or "could not resolve an ir site" in low
          or "no gate-passing annual report" in low or "could not find an annual report" in low
          or "diamond could not fetch" in low
          or "fetch failed" in low or "resolve failed" in low):
        code = "NO_REPORT"
    else:
        code = "UNKNOWN"
    return code, ctx


# ═════════════════════════════════════════════════════════════════════
# EXTRACTION WORKER
# ═════════════════════════════════════════════════════════════════════

def _detect_report_year(pdf_path) -> str:
    """Best-effort fiscal year from the report's cover / first pages — used to enrich
    the "no options data available for <company> in FY<year>" message when the fetch
    metadata didn't carry a year (e.g. the EDGAR path, or a manual upload). Returns a
    4-digit year string, or "" if undetermined. Never raises."""
    try:
        import datetime as _dt
        n = _pdf_page_count(Path(pdf_path))
        if not n:
            return ""
        pages = [p for p in (1, 2, 3, 4, 5) if p <= n]
        texts = extract_text_from_pages(str(pdf_path), pages) or {}
        blob = "\n".join(texts.get(p, "") for p in pages)
        if not blob.strip():
            return ""
        low = blob.lower()
        cur = _dt.datetime.utcnow().year
        lo, hi = 2015, cur + 1
        # 1) explicit fiscal-year phrasing on the cover
        for pat in (r"year ended[^0-9]{0,20}(20\d{2})",
                    r"for the year[^0-9]{0,20}(20\d{2})",
                    r"fiscal year[^0-9]{0,12}(20\d{2})",
                    r"(20\d{2})\s+annual report",
                    r"annual report[^0-9]{0,12}(20\d{2})"):
            m = re.search(pat, low)
            if m and lo <= int(m.group(1)) <= hi:
                return m.group(1)
        # 2) fallback: most recent plausible 4-digit year on the cover pages
        yrs = [int(y) for y in re.findall(r"\b(20\d{2})\b", blob) if lo <= int(y) <= hi]
        if yrs:
            return str(max(yrs))
    except Exception:
        pass
    return ""


def _serve_cached_result(job_id: str, final: dict) -> None:
    """Finish a job from a previously-stored extraction — no fetch / render / LLM.
    Writes this job's extraction.json + Excel from the cached result and marks the
    job complete at zero cost. Used by the results cache (e.g. a repeat EU search
    for the same company + fiscal period)."""
    job = JOBS[job_id]
    job_dir = get_job_dir(job_id)
    pdf_stem = Path(job["filename"]).stem

    final = {**final}
    final["_meta"] = {**(final.get("_meta") or {}), "served_from_cache": True}

    json_path = job_dir / "extraction.json"
    excel_path = job_dir / f"{pdf_stem}_options.xlsx"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)
    build_workbook(str(json_path), str(excel_path))

    extraction_id = None
    try:
        extraction_id = save_extraction(final, excel_path.read_bytes(), excel_path.name)
    except Exception as e:
        print(f"WARNING: NeonDB save failed: {e}", file=sys.stderr)

    # Mark every stage complete so the timeline reads cleanly.
    for k in list(job.get("stages", {}).keys()):
        job["stages"][k] = {"status": "completed", "duration": 0, "cost": 0,
                            "details": "Reused from cache"}

    update_job(job_id, status="completed", progress=100, current_stage=None,
               result_available=True, cost_so_far=0, extraction_id=extraction_id)


_US_FORM_RE = re.compile(
    r"(?<![A-Za-z0-9])(10[\s_-]?[KQ]|20[\s_-]?F|40[\s_-]?F)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _is_us_job(job: dict) -> bool:
    """True when this job is a US filing → bypass the extraction disk cache.

    Detected via: EDGAR-sourced jobs, US country metadata, or an uploaded file
    whose name carries a US SEC form type (10-K / 10-Q / 20-F / 40-F).
    """
    if (job.get("source") or "") == "edgar":
        return True
    meta = job.get("source_meta") or {}
    country = (meta.get("country") or "").strip().lower()
    if country in ("us", "usa", "united states", "united states of america"):
        return True
    if _US_FORM_RE.search(job.get("filename") or ""):
        return True
    return False


def _ir_scraper_fetch_annual(out_pdf_path, name, ticker, country, ocr_cb,
                             timeout_sec=EU_IR_SCRAPER_TIMEOUT_SEC):
    """Run the universal IR scraper for the latest ANNUAL report only, capped at
    `timeout_sec` wall-clock. Writes the PDF to out_pdf_path and returns the scraper
    info dict on success, else None. A timed-out scraper is abandoned (its orphan
    thread keeps running but only ever writes out_pdf_path, so it cannot race a
    subsequent fallback that writes a different file). Shared by the EU / Germany /
    Japan 'IR scraper first' branches (EU itself uses the dual annual+interim path)."""
    import concurrent.futures
    if not ((name or "").strip() or (ticker or "").strip()):
        return None
    out_pdf_path = Path(out_pdf_path)
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(diamond_route._attempt_irscraper,
                        name or "", ticker or "", out_pdf_path, "annual",
                        ocr_cb, country or "")
        info = fut.result(timeout=timeout_sec)
        if info and out_pdf_path.exists() and out_pdf_path.stat().st_size > 0:
            return info
        return None
    except concurrent.futures.TimeoutError:
        return None
    except Exception:
        return None
    finally:
        ex.shutdown(wait=False)


def run_extraction_pipeline(job_id: str):
    """Mirror of options.py main() flow, instrumented with progress updates."""
    job = JOBS[job_id]
    job_dir = get_job_dir(job_id)
    pdf_path = job_dir / job["filename"]
    pdf_stem = Path(job["filename"]).stem

    # Results-cache key for this run, set by a source branch once the filing
    # identity (e.g. EU LEI + fiscal period) is known. Stored on completion so a
    # later identical request can be served instantly without re-extracting.
    results_key = None

    try:
        _set_anthropic_verbose(False)

        # ── Optional Stage 0: EDGAR fetch ──────────────────────────
        if job.get("source") == "edgar":
            update_job(job_id, status="processing",
                       current_stage="edgar_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}
            try:
                info = fetch_filing_as_pdf(
                    ticker=meta.get("ticker"),
                    form=meta.get("form", "10-K"),
                    out_pdf_path=pdf_path,
                )
            except Exception as e:
                raise RuntimeError(f"EDGAR fetch failed: {e}") from e

            # Record on-disk size now that the PDF exists
            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            # Stash filing metadata so it ends up in the result's _meta
            JOBS[job_id]["source_meta"] = {**meta, **info}

            _mark_stage(job_id, "edgar_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"filed {info.get('filing_date','?')}"
                        ))

        # ── Optional Stage 0 (UK): IR scraper first, then Companies House ──
        if job.get("source") == "companies_house":
            update_job(job_id, status="processing",
                       current_stage="ch_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}
            _uk_name = (meta.get("company_name") or meta.get("title") or "").strip()
            _uk_ticker = (meta.get("ticker") or "").strip()

            def _ocr_cb(done: int, total: int):
                # Map OCR progress into the 2→8% band so the UI moves while
                # a large scanned filing is being OCR'd (can take ~1 min).
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            ch_ok = False
            # Step A: universal IR scraper (annual report only), capped at 100s —
            # same strategy as the EU / Germany / Japan tabs. The issuer's own IR
            # site often carries a richer annual report than the Companies House
            # statutory accounts. Writes its own _ir.pdf so a timed-out orphan can't
            # race the Companies House fallback PDF.
            _uk_ir = job_dir / f"{pdf_stem}_ir.pdf"
            _uk_info = _ir_scraper_fetch_annual(_uk_ir, _uk_name, _uk_ticker,
                                                "United Kingdom", _ocr_cb)
            if _uk_info:
                JOBS[job_id]["filename"] = _uk_ir.name
                pdf_path = _uk_ir
                pdf_stem = _uk_ir.stem
                try:
                    JOBS[job_id]["file_size"] = pdf_path.stat().st_size
                except Exception:
                    pass
                meta = {**meta, **_uk_info,
                        "company": _uk_name or meta.get("company_name"),
                        "uk_path": "ir_scraper", "form": "Annual Report"}
                JOBS[job_id]["source_meta"] = meta
                _mark_stage(job_id, "ch_fetch",
                            duration=time.time() - stage_start, cost=0,
                            details=(f"{meta.get('company','?')} · IR scraper · "
                                     f"{_uk_info.get('ir_url','?')}"))
                ch_ok = True

            # Step B: Companies House fallback — the official statutory accounts
            # filing (authoritative; the endpoint already resolved the company
            # number, so this path is always available).
            if not ch_ok:
                try:
                    info = fetch_uk_filing_as_pdf(
                        company_number=meta.get("company_number"),
                        category=meta.get("category", "accounts"),
                        out_pdf_path=pdf_path,
                        company_name=meta.get("company_name") or meta.get("title"),
                        ocr_progress=_ocr_cb,
                    )
                except Exception as e:
                    raise RuntimeError(f"Companies House fetch failed: {e}") from e

                try:
                    JOBS[job_id]["file_size"] = pdf_path.stat().st_size
                except Exception:
                    pass

                JOBS[job_id]["source_meta"] = {**meta, **info, "uk_path": "companies_house"}

                ocr_meta = info.get("ocr") or {}
                ocr_note = (
                    f" · OCR {ocr_meta.get('pages')}p"
                    if ocr_meta.get("ocr") else ""
                )
                _mark_stage(job_id, "ch_fetch",
                            duration=time.time() - stage_start,
                            cost=0,
                            details=(
                                f"{info.get('company','?')} · {info.get('form','?')} · "
                                f"filed {info.get('filing_date','?')}{ocr_note}"
                            ))

        # ── Optional Stage 0 (DK): Denmark / CVR fetch + OCR ───────
        if job.get("source") == "denmark":
            update_job(job_id, status="processing",
                       current_stage="dk_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            def _ocr_cb_dk(done: int, total: int):
                # Map OCR progress into the 2→8% band (old scanned DK filings).
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_dk_filing_as_pdf(
                    company_number=meta.get("company_number"),
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name") or meta.get("title"),
                    ocr_progress=_ocr_cb_dk,
                )
            except Exception as e:
                raise RuntimeError(f"Denmark (CVR) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            JOBS[job_id]["source_meta"] = {**meta, **info}

            ocr_meta = info.get("ocr") or {}
            ocr_note = (
                f" · OCR {ocr_meta.get('pages')}p"
                if ocr_meta.get("ocr") else ""
            )
            _mark_stage(job_id, "dk_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"filed {info.get('filing_date','?')}{ocr_note}"
                        ))

        # ── Optional Stage 0 (JP): Japan / EDINET fetch + OCR ──────
        # ── Optional Stage 0 (JP): IR scraper first, then EDINET fallback ──
        if job.get("source") == "japan":
            update_job(job_id, status="processing",
                       current_stage="jp_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}
            _jp_name = (meta.get("company_name") or meta.get("title") or "").strip()
            _jp_ticker = (meta.get("ticker") or "").strip()

            def _ocr_cb_jp(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            jp_ok = False
            # Step A: universal IR scraper (annual report only), capped at 100s —
            # same strategy as the EU tab. Writes its own _ir.pdf so a timed-out
            # orphan can't race the EDINET fallback PDF.
            _jp_ir = job_dir / f"{pdf_stem}_ir.pdf"
            _jp_info = _ir_scraper_fetch_annual(_jp_ir, _jp_name, _jp_ticker,
                                                "Japan", _ocr_cb_jp)
            if _jp_info:
                JOBS[job_id]["filename"] = _jp_ir.name
                pdf_path = _jp_ir
                pdf_stem = _jp_ir.stem
                try:
                    JOBS[job_id]["file_size"] = pdf_path.stat().st_size
                except Exception:
                    pass
                meta = {**meta, **_jp_info,
                        "company": _jp_name or meta.get("company_name"),
                        "jp_path": "ir_scraper", "form": "Annual Report"}
                JOBS[job_id]["source_meta"] = meta
                _mark_stage(job_id, "jp_fetch",
                            duration=time.time() - stage_start, cost=0,
                            details=(f"{meta.get('company','?')} · IR scraper · "
                                     f"{_jp_info.get('ir_url','?')}"))
                jp_ok = True

            # Step B: EDINET fallback — only when an EDINET_API_KEY is configured
            # (EDINET's listing + PDF download both need it) AND an EDINET code was
            # resolved (best-effort, by the endpoint). Without a key it's skipped.
            if not jp_ok:
                _edinet_key = os.environ.get("EDINET_API_KEY", "").strip()
                _edinet_code = meta.get("edinet_code") or meta.get("company_number")
                if (_edinet_key and _edinet_key != "your_edinet_key_here"
                        and _edinet_code):
                    def _scan_cb_jp(done: int, total: int):
                        if total:
                            update_job(job_id, progress=2 + int(6 * done / total))
                    try:
                        info = fetch_jp_filing_as_pdf(
                            company_number=_edinet_code,
                            category=meta.get("category", "annual"),
                            out_pdf_path=pdf_path,
                            company_name=_jp_name or meta.get("title"),
                            scan_progress=_scan_cb_jp,
                        )
                        try:
                            JOBS[job_id]["file_size"] = pdf_path.stat().st_size
                        except Exception:
                            pass
                        meta = {**meta, **info, "jp_path": "edinet"}
                        JOBS[job_id]["source_meta"] = meta
                        ocr_meta = info.get("ocr") or {}
                        ocr_note = (f" · OCR {ocr_meta.get('pages')}p"
                                    if ocr_meta.get("ocr") else "")
                        _mark_stage(job_id, "jp_fetch",
                                    duration=time.time() - stage_start, cost=0,
                                    details=(f"{info.get('company','?')} · EDINET · "
                                             f"{info.get('form','?')} · filed "
                                             f"{info.get('filing_date','?')}{ocr_note}"))
                        jp_ok = True
                    except Exception:
                        pass
                if not jp_ok:
                    raise RuntimeError(
                        "Could not find an annual report for this Japanese company "
                        "on its investor-relations site. Please use the Upload tab "
                        "to submit the PDF directly.")

        # ── Optional Stage 0 (KR): Korea / DART fetch + OCR ────────
        if job.get("source") == "korea":
            update_job(job_id, status="processing",
                       current_stage="kr_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            # Resolve ticker/name → DART corp_code here (deferred from the
            # request handler so the job_id returns instantly; see endpoint).
            corp_code = meta.get("corp_code") or meta.get("company_number")
            if not corp_code:
                try:
                    resolved = resolve_kr_company_number(
                        (meta.get("ticker") or "").strip().upper(),
                        (meta.get("company_name") or "").strip() or None,
                    )
                except Exception as e:
                    raise RuntimeError(f"Korea (DART) resolve failed: {e}") from e

                corp_code = resolved["company_number"]
                meta = {
                    **meta,
                    "company_name": resolved.get("title") or meta.get("company_name"),
                    "corp_code": corp_code,
                    "company_number": corp_code,
                    "stock_code": resolved.get("stock_code"),
                    "matched_via": resolved.get("matched_via"),
                }
                JOBS[job_id]["source_meta"] = meta

                # Now that the corp_code is known, give the PDF a stable name
                # (matches the prior "{ticker or corp_code}_KR_..." scheme).
                kr_label = (meta.get("ticker") or "").strip().upper() or corp_code
                new_filename = f"{kr_label}_KR_{meta.get('category', 'annual')}.pdf"
                JOBS[job_id]["filename"] = new_filename
                pdf_path = job_dir / new_filename
                pdf_stem = Path(new_filename).stem

            def _ocr_cb_kr(done: int, total: int):
                # Map OCR progress into the 2→8% band (rare for DART text PDFs).
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_kr_filing_as_pdf(
                    company_number=meta.get("corp_code") or meta.get("company_number"),
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name") or meta.get("title"),
                    ocr_progress=_ocr_cb_kr,
                )
            except Exception as e:
                raise RuntimeError(f"Korea (DART) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            JOBS[job_id]["source_meta"] = {**meta, **info}

            ocr_meta = info.get("ocr") or {}
            ocr_note = (
                f" · OCR {ocr_meta.get('pages')}p"
                if ocr_meta.get("ocr") else ""
            )
            _mark_stage(job_id, "kr_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"filed {info.get('filing_date','?')}{ocr_note}"
                        ))

        # ── Optional Stage 0 (BR): Brazil / CVM fetch + OCR ────────
        if job.get("source") == "brazil":
            update_job(job_id, status="processing",
                       current_stage="br_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            # Resolve ticker/name/CNPJ → CVM code here (deferred from the
            # request handler so the job_id returns instantly; see endpoint).
            cvm_code = meta.get("cvm_code") or meta.get("company_number")
            if not cvm_code:
                try:
                    resolved = resolve_br_company_number(
                        (meta.get("ticker") or "").strip().upper(),
                        (meta.get("company_name") or "").strip() or None,
                    )
                except Exception as e:
                    raise RuntimeError(f"Brazil (CVM) resolve failed: {e}") from e

                cvm_code = resolved["company_number"]
                meta = {
                    **meta,
                    "company_name": resolved.get("title") or meta.get("company_name"),
                    "cvm_code": cvm_code,
                    "company_number": cvm_code,
                    "cnpj": resolved.get("cnpj"),
                    "matched_via": resolved.get("matched_via"),
                }
                JOBS[job_id]["source_meta"] = meta

                # Now that the CVM code is known, give the PDF a stable name.
                br_label = (meta.get("ticker") or "").strip().upper() or cvm_code
                new_filename = f"{br_label}_BR_{meta.get('category', 'annual')}.pdf"
                JOBS[job_id]["filename"] = new_filename
                pdf_path = job_dir / new_filename
                pdf_stem = Path(new_filename).stem

            def _ocr_cb_br(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_br_filing_as_pdf(
                    company_number=meta.get("cvm_code") or meta.get("company_number"),
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name") or meta.get("title"),
                    ocr_progress=_ocr_cb_br,
                )
            except Exception as e:
                raise RuntimeError(f"Brazil (CVM) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            JOBS[job_id]["source_meta"] = {**meta, **info}

            ocr_meta = info.get("ocr") or {}
            ocr_note = (
                f" · OCR {ocr_meta.get('pages')}p"
                if ocr_meta.get("ocr") else ""
            )
            _mark_stage(job_id, "br_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"period {info.get('report_period','?')}{ocr_note}"
                        ))

        # ── Optional Stage 0 (TW): Taiwan / TWSE fetch + OCR ───────
        if job.get("source") == "taiwan":
            update_job(job_id, status="processing",
                       current_stage="tw_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            # Resolve ticker/name → TWSE stock code here (deferred from the
            # request handler so the job_id returns instantly; see endpoint).
            stock_code = meta.get("stock_code") or meta.get("company_number")
            if not stock_code:
                try:
                    resolved = resolve_tw_company_number(
                        (meta.get("ticker") or "").strip(),
                        (meta.get("company_name") or "").strip() or None,
                    )
                except Exception as e:
                    raise RuntimeError(f"Taiwan (TWSE) resolve failed: {e}") from e

                stock_code = resolved["company_number"]
                meta = {
                    **meta,
                    "company_name": resolved.get("title") or meta.get("company_name"),
                    "stock_code": stock_code,
                    "company_number": stock_code,
                    "name_en": resolved.get("name_en"),
                    "matched_via": resolved.get("matched_via"),
                }
                JOBS[job_id]["source_meta"] = meta

                # Now that the stock code is known, give the PDF a stable name.
                tw_label = (meta.get("ticker") or "").strip() or stock_code
                new_filename = f"{tw_label}_TW_{meta.get('category', 'annual')}.pdf"
                JOBS[job_id]["filename"] = new_filename
                pdf_path = job_dir / new_filename
                pdf_stem = Path(new_filename).stem

            def _ocr_cb_tw(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_tw_filing_as_pdf(
                    company_number=meta.get("stock_code") or meta.get("company_number"),
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name") or meta.get("title"),
                    ocr_progress=_ocr_cb_tw,
                )
            except Exception as e:
                raise RuntimeError(f"Taiwan (TWSE) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            JOBS[job_id]["source_meta"] = {**meta, **info}

            ocr_meta = info.get("ocr") or {}
            ocr_note = (
                f" · OCR {ocr_meta.get('pages')}p"
                if ocr_meta.get("ocr") else ""
            )
            _mark_stage(job_id, "tw_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"period {info.get('report_period','?')}{ocr_note}"
                        ))

        # ── Optional Stage 0 (EU): IR scraper first, then ESEF fallback ──
        if job.get("source") == "eu":
            update_job(job_id, status="processing",
                       current_stage="eu_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            def _ocr_cb_eu(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            # ── Step A: universal IR scraper FIRST (capped at 100s) ──
            # Reuses the SAME scraper the Testing tab uses (ir_resolve_proto +
            # ir_fetch_proto). The company name + ticker + market/country the EU tab
            # collected feed the resolver. From ONE IR-page crawl we capture BOTH the
            # latest annual report AND the latest recent interim/quarterly report:
            #   - annual found        -> run the annual; keep the interim as a fallback
            #                            the user can opt into if the annual has no data.
            #   - only interim found  -> run the interim directly (no annual to prefer).
            #   - nothing within 100s -> abandon the scraper (its orphan thread only
            #                            writes its own _annual/_interim PDFs, never the
            #                            ESEF/pipeline PDF) and fall through to ESEF.
            ir_ok = False
            _ir_name = (meta.get("company_name") or meta.get("title") or "").strip()
            _ir_ticker = (meta.get("ticker") or "").strip()
            _ir_country = (meta.get("country") or "").strip()
            if _ir_name or _ir_ticker:
                import concurrent.futures
                _annual_pdf = job_dir / f"{pdf_stem}_annual.pdf"
                _interim_pdf = job_dir / f"{pdf_stem}_interim.pdf"

                def _eu_scrape_both():
                    import ir_resolve_proto as _R
                    import ir_fetch_proto as _F
                    _res = _R.resolve(_ir_name or "", _ir_ticker or "", "",
                                      _ir_country or "")
                    _url = _res.get("chosen_url")
                    if not _url:
                        raise RuntimeError("IR-scraper: could not resolve an IR site")
                    _out = _F.fetch_reports(
                        _url, allow_fc=True,
                        annual_path=str(_annual_pdf), interim_path=str(_interim_pdf),
                        name=_ir_name or "")
                    _out["ir_url"] = _url
                    _out["resolver_confidence"] = _res.get("confidence")
                    return _out

                _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    _scr = _ex.submit(_eu_scrape_both)
                    _r = _scr.result(timeout=EU_IR_SCRAPER_TIMEOUT_SEC)
                    _ann = (_r or {}).get("annual")
                    _intm = (_r or {}).get("interim")

                    # Pick the primary doc: prefer the annual; else the interim.
                    _primary = None        # (path, kind, fiscal_year)
                    _alt = None            # interim kept as the opt-in fallback
                    if _ann and _annual_pdf.exists() and _annual_pdf.stat().st_size > 0:
                        _primary = (_annual_pdf, "annual", _ann.get("fiscal_year"))
                        if _intm and _interim_pdf.exists() and _interim_pdf.stat().st_size > 0:
                            _alt = (_interim_pdf, "interim", _intm.get("fiscal_year"))
                    elif _intm and _interim_pdf.exists() and _interim_pdf.stat().st_size > 0:
                        _primary = (_interim_pdf, "interim", _intm.get("fiscal_year"))

                    if _primary:
                        _ppath, _pkind, _pyear = _primary
                        JOBS[job_id]["filename"] = _ppath.name
                        pdf_path = _ppath
                        pdf_stem = _ppath.stem
                        try:
                            JOBS[job_id]["file_size"] = pdf_path.stat().st_size
                        except Exception:
                            pass
                        meta = {
                            **meta,
                            "company": meta.get("company_name") or _ir_name,
                            "eu_path": "ir_scraper",
                            "ir_url": (_r or {}).get("ir_url"),
                            "form": ("Annual Report" if _pkind == "annual"
                                     else "Interim/Quarterly Report"),
                            "report_period": _pyear,
                            "report_year": _pyear,
                        }
                        if _alt:
                            _apath, _akind, _ayear = _alt
                            meta["alt_report_path"] = str(_apath)
                            meta["alt_report_kind"] = _akind
                            meta["alt_report_year"] = _ayear
                        JOBS[job_id]["source_meta"] = meta
                        _mark_stage(
                            job_id, "eu_fetch",
                            duration=time.time() - stage_start, cost=0,
                            details=(f"{meta.get('company', '?')} · IR scraper · "
                                     f"{meta['form']}"
                                     + (" (+interim available)" if _alt else "")))
                        ir_ok = True
                except concurrent.futures.TimeoutError:
                    pass  # scraper too slow; fall back to ESEF
                except Exception:
                    pass  # scraper failed; fall back to ESEF
                finally:
                    # Do NOT wait — a timed-out scraper keeps running in its own
                    # thread but only ever writes its _annual/_interim PDFs, so it
                    # can't race the ESEF/pipeline PDF.
                    _ex.shutdown(wait=False)

            # ── Step B: authoritative ESEF fallback (filings.xbrl.org) ──
            if not ir_ok:
                # Resolve ticker/name → LEI here (deferred from the request handler
                # so the job_id returns instantly; the filings.xbrl.org index lookup
                # can be slow on a cold call — same 524-avoidance pattern as Korea).
                lei = meta.get("lei") or meta.get("company_number")
                if not lei:
                    try:
                        resolved = resolve_eu_company_number(
                            (meta.get("ticker") or "").strip(),
                            (meta.get("company_name") or "").strip() or None,
                            (meta.get("country") or "").strip() or None,
                            (meta.get("isin") or "").strip() or None,
                        )
                    except Exception as e:
                        raise RuntimeError(f"EU (ESEF) resolve failed: {e}") from e

                    lei = resolved["company_number"]
                    meta = {
                        **meta,
                        "company_name": resolved.get("title") or meta.get("company_name"),
                        "lei": lei,
                        "company_number": lei,
                        "country": resolved.get("country") or meta.get("country"),
                        "matched_via": resolved.get("matched_via"),
                    }
                    JOBS[job_id]["source_meta"] = meta

                    # Now that the LEI is known, give the PDF a stable name.
                    eu_label = (meta.get("ticker") or "").strip() or lei
                    new_filename = f"{eu_label}_EU_{meta.get('category', 'annual')}.pdf"
                    JOBS[job_id]["filename"] = new_filename
                    pdf_path = job_dir / new_filename
                    pdf_stem = Path(new_filename).stem

                # Results cache: if this exact filing (LEI + fiscal period) was already
                # extracted, reuse the stored result — skip the render + Stage 1/2/3 +
                # Claude entirely. The period comes from a cheap index lookup (no render),
                # so a newer fiscal year naturally misses the cache and re-extracts.
                try:
                    from eu_fetch import _latest_filing as _eu_latest_filing
                    _period = (_eu_latest_filing(lei).get("period_end") or "").strip()
                except Exception:
                    _period = ""
                if _period:
                    results_key = ("eu", lei, _period, meta.get("category", "annual"))
                    _cached_final = cache.get("results", *results_key)
                    if _cached_final is not None:
                        _mark_stage(job_id, "eu_fetch",
                                    duration=time.time() - stage_start, cost=0,
                                    details=(f"{meta.get('company_name', '?')} · "
                                             f"reused from cache (period {_period})"))
                        _serve_cached_result(job_id, _cached_final)
                        return

                try:
                    info = fetch_eu_filing_as_pdf(
                        company_number=meta.get("lei") or meta.get("company_number"),
                        category=meta.get("category", "annual"),
                        out_pdf_path=pdf_path,
                        company_name=meta.get("company_name") or meta.get("title"),
                        ocr_progress=_ocr_cb_eu,
                    )
                except Exception as e:
                    raise RuntimeError(f"EU (ESEF) fetch failed: {e}") from e

                try:
                    JOBS[job_id]["file_size"] = pdf_path.stat().st_size
                except Exception:
                    pass

                JOBS[job_id]["source_meta"] = {**meta, **info}

                ocr_meta = info.get("ocr") or {}
                ocr_note = (
                    f" · OCR {ocr_meta.get('pages')}p"
                    if ocr_meta.get("ocr") else ""
                )
                _mark_stage(job_id, "eu_fetch",
                            duration=time.time() - stage_start,
                            cost=0,
                            details=(
                                f"{info.get('company','?')} · {info.get('form','?')} · "
                                f"{info.get('country','?')} · period "
                                f"{info.get('report_period','?')}{ocr_note}"
                            ))

        # ── Optional Stage 0 (DE): IR scraper first, then SEC EDGAR, then upload ──
        if job.get("source") == "germany":
            update_job(job_id, status="processing",
                       current_stage="de_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}
            _de_name = (meta.get("company_name") or "").strip()
            _de_ticker = (meta.get("ticker") or "").strip()

            def _ocr_cb_de(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            de_ok = False
            # Step A: universal IR scraper (annual report only), capped at 100s —
            # same strategy as the EU tab. Germany has no working official data API
            # (Bundesanzeiger has no API; DE issuers have ~0 ESEF filings), so the
            # scraper is the primary path. Writes its own _ir.pdf (orphan-safe).
            _de_ir = job_dir / f"{pdf_stem}_ir.pdf"
            _de_info = _ir_scraper_fetch_annual(_de_ir, _de_name, _de_ticker,
                                                "Germany", _ocr_cb_de)
            if _de_info:
                JOBS[job_id]["filename"] = _de_ir.name
                pdf_path = _de_ir
                pdf_stem = _de_ir.stem
                try:
                    JOBS[job_id]["file_size"] = pdf_path.stat().st_size
                except Exception:
                    pass
                meta = {**meta, **_de_info,
                        "company": _de_name or meta.get("company_name"),
                        "de_path": "ir_scraper", "form": "Annual Report"}
                JOBS[job_id]["source_meta"] = meta
                _mark_stage(job_id, "de_fetch",
                            duration=time.time() - stage_start, cost=0,
                            details=(f"{meta.get('company','?')} · IR scraper · "
                                     f"{_de_info.get('ir_url','?')}"))
                de_ok = True

            # Step B: SEC EDGAR fallback — many German blue-chips file a 20-F
            # (e.g. SAP). Name-verified inside _attempt_edgar (rejects wrong entity).
            if not de_ok:
                try:
                    info = diamond_route._attempt_edgar(
                        _de_name, _de_ticker, pdf_path,
                        meta.get("category", "annual"), _ocr_cb_de)
                    if info and pdf_path.exists() and pdf_path.stat().st_size > 0:
                        try:
                            JOBS[job_id]["file_size"] = pdf_path.stat().st_size
                        except Exception:
                            pass
                        meta = {**meta, **info, "de_path": "edgar"}
                        JOBS[job_id]["source_meta"] = meta
                        _mark_stage(job_id, "de_fetch",
                                    duration=time.time() - stage_start, cost=0,
                                    details=(f"{info.get('company','?')} · SEC EDGAR · "
                                             f"{info.get('form','?')}"))
                        de_ok = True
                except Exception:
                    pass
                if not de_ok:
                    raise RuntimeError(
                        "Could not find an annual report for this German company on "
                        "its investor-relations site or SEC EDGAR. Please use the "
                        "Upload tab to submit the PDF directly.")

        # ── Optional Stage 0 (CA): Canada via SEC EDGAR (MJDS 40-F) ─
        if job.get("source") == "canada":
            update_job(job_id, status="processing",
                       current_stage="ca_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            def _ocr_cb_ca(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_ca_filing_as_pdf(
                    ticker=meta.get("ticker"),
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name"),
                    ocr_progress=_ocr_cb_ca,
                )
            except Exception as e:
                raise RuntimeError(f"Canada (SEC MJDS) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            JOBS[job_id]["source_meta"] = {**meta, **info}

            ocr_meta = info.get("ocr") or {}
            ocr_note = (
                f" · OCR {ocr_meta.get('pages')}p"
                if ocr_meta.get("ocr") else ""
            )
            _mark_stage(job_id, "ca_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"filed {info.get('filing_date','?')}{ocr_note}"
                        ))

        # ── Optional Stage 0 (CN): China / CNINFO fetch + OCR ──────
        if job.get("source") == "china":
            update_job(job_id, status="processing",
                       current_stage="cn_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            # Resolve code/name → CNINFO stock code + orgId here (deferred from
            # the request handler so the job_id returns instantly; the topSearch
            # call can be slow on a cold start — same pattern as Korea/Taiwan).
            stock_code = meta.get("stock_code") or meta.get("company_number")
            org_id = meta.get("org_id")
            if not stock_code or not org_id:
                try:
                    resolved = resolve_cn_company_number(
                        (meta.get("ticker") or "").strip(),
                        (meta.get("company_name") or "").strip() or None,
                    )
                except Exception as e:
                    raise RuntimeError(f"China (CNINFO) resolve failed: {e}") from e

                stock_code = resolved["company_number"]
                org_id = resolved.get("org_id")
                meta = {
                    **meta,
                    "company_name": resolved.get("title") or meta.get("company_name"),
                    "stock_code": stock_code,
                    "company_number": stock_code,
                    "org_id": org_id,
                    "matched_via": resolved.get("matched_via"),
                }
                JOBS[job_id]["source_meta"] = meta

                # Now that the stock code is known, give the PDF a stable name.
                cn_label = (meta.get("ticker") or "").strip() or stock_code
                new_filename = f"{cn_label}_CN_{meta.get('category', 'annual')}.pdf"
                JOBS[job_id]["filename"] = new_filename
                pdf_path = job_dir / new_filename
                pdf_stem = Path(new_filename).stem

            def _ocr_cb_cn(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_cn_filing_as_pdf(
                    company_number=stock_code,
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name") or meta.get("title"),
                    org_id=org_id,
                    ocr_progress=_ocr_cb_cn,
                )
            except Exception as e:
                raise RuntimeError(f"China (CNINFO) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            JOBS[job_id]["source_meta"] = {**meta, **info}

            ocr_meta = info.get("ocr") or {}
            ocr_note = (
                f" · OCR {ocr_meta.get('pages')}p"
                if ocr_meta.get("ocr") else ""
            )
            _mark_stage(job_id, "cn_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"period {info.get('report_period','?')}{ocr_note}"
                        ))

        # ── Optional Stage 0 (IN): India / BSE fetch + OCR ─────────
        if job.get("source") == "india":
            update_job(job_id, status="processing",
                       current_stage="in_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            # Resolve ticker/name/ISIN → BSE scrip code here (deferred from the
            # request handler so the job_id returns instantly; the scrip-master
            # download can be slow on a cold start — same pattern as Korea).
            scrip_code = meta.get("scrip_code") or meta.get("company_number")
            if not scrip_code:
                try:
                    resolved = resolve_in_company_number(
                        (meta.get("ticker") or "").strip(),
                        (meta.get("company_name") or "").strip() or None,
                    )
                except Exception as e:
                    raise RuntimeError(f"India (BSE) resolve failed: {e}") from e

                scrip_code = resolved["company_number"]
                meta = {
                    **meta,
                    "company_name": resolved.get("title") or meta.get("company_name"),
                    "scrip_code": scrip_code,
                    "company_number": scrip_code,
                    "isin": resolved.get("isin"),
                    "ticker": resolved.get("ticker") or meta.get("ticker"),
                    "matched_via": resolved.get("matched_via"),
                }
                JOBS[job_id]["source_meta"] = meta

                # Now that the scrip code is known, give the PDF a stable name.
                in_label = (meta.get("ticker") or "").strip() or scrip_code
                new_filename = f"{in_label}_IN_{meta.get('category', 'annual')}.pdf"
                JOBS[job_id]["filename"] = new_filename
                pdf_path = job_dir / new_filename
                pdf_stem = Path(new_filename).stem

            def _ocr_cb_in(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_in_filing_as_pdf(
                    company_number=scrip_code,
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name") or meta.get("title"),
                    ocr_progress=_ocr_cb_in,
                )
            except Exception as e:
                raise RuntimeError(f"India (BSE) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            JOBS[job_id]["source_meta"] = {**meta, **info}

            ocr_meta = info.get("ocr") or {}
            ocr_note = (
                f" · OCR {ocr_meta.get('pages')}p"
                if ocr_meta.get("ocr") else ""
            )
            _mark_stage(job_id, "in_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"period {info.get('report_period','?')}{ocr_note}"
                        ))

        # ── Optional Stage 0 (HK): Hong Kong / HKEXnews fetch + OCR ─
        if job.get("source") == "hongkong":
            update_job(job_id, status="processing",
                       current_stage="hk_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            # Resolve code/name → HKEXnews stockId here (deferred from the request
            # handler so the job_id returns instantly; the securities-master
            # download can be slow on a cold start — same pattern as Korea).
            stock_id = meta.get("stock_id") or meta.get("company_number")
            if not stock_id:
                try:
                    resolved = resolve_hk_company_number(
                        (meta.get("ticker") or "").strip(),
                        (meta.get("company_name") or "").strip() or None,
                    )
                except Exception as e:
                    raise RuntimeError(f"Hong Kong (HKEXnews) resolve failed: {e}") from e

                stock_id = resolved["company_number"]
                meta = {
                    **meta,
                    "company_name": resolved.get("title") or meta.get("company_name"),
                    "stock_id": stock_id,
                    "company_number": stock_id,
                    "code": resolved.get("code"),
                    "matched_via": resolved.get("matched_via"),
                }
                JOBS[job_id]["source_meta"] = meta

                # Now that the stockId is known, give the PDF a stable name.
                hk_label = (meta.get("ticker") or "").strip() or resolved.get("code") or stock_id
                new_filename = f"{hk_label}_HK_{meta.get('category', 'annual')}.pdf"
                JOBS[job_id]["filename"] = new_filename
                pdf_path = job_dir / new_filename
                pdf_stem = Path(new_filename).stem

            def _ocr_cb_hk(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_hk_filing_as_pdf(
                    company_number=stock_id,
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name") or meta.get("title"),
                    ocr_progress=_ocr_cb_hk,
                )
            except Exception as e:
                raise RuntimeError(f"Hong Kong (HKEXnews) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            JOBS[job_id]["source_meta"] = {**meta, **info}

            ocr_meta = info.get("ocr") or {}
            ocr_note = (
                f" · OCR {ocr_meta.get('pages')}p"
                if ocr_meta.get("ocr") else ""
            )
            _mark_stage(job_id, "hk_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"period {info.get('report_period','?')}{ocr_note}"
                        ))

        # ── Optional Stage 0 (ID): Indonesia / IDX fetch + OCR ─────
        # IDX is keyed by the ticker code directly (no resolver). The fetch uses
        # a headless browser to pass IDX's Cloudflare challenge (Asia spike).
        if job.get("source") == "indonesia":
            update_job(job_id, status="processing",
                       current_stage="id_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            def _ocr_cb_id(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_id_filing_as_pdf(
                    company_number=meta.get("ticker") or meta.get("company_number"),
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name"),
                    ocr_progress=_ocr_cb_id,
                )
            except Exception as e:
                raise RuntimeError(f"Indonesia (IDX) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            JOBS[job_id]["source_meta"] = {**meta, **info}

            ocr_meta = info.get("ocr") or {}
            ocr_note = (
                f" · OCR {ocr_meta.get('pages')}p"
                if ocr_meta.get("ocr") else ""
            )
            _mark_stage(job_id, "id_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"period {info.get('report_period','?')}{ocr_note}"
                        ))

        # ── Optional Stage 0 (MY): Malaysia / Bursa fetch + OCR ──
        # Resolve ticker/name -> Bursa stock code in the background (deferred,
        # like China/Israel). Plain HTTP — no Firecrawl (Bursa's announcement
        # API and the disclosure CDN are reachable directly).
        if job.get("source") == "malaysia":
            update_job(job_id, status="processing",
                       current_stage="my_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            stock_code = meta.get("company_number")
            if not stock_code:
                try:
                    resolved = resolve_my_company_number(
                        (meta.get("ticker") or "").strip(),
                        (meta.get("company_name") or "").strip() or None,
                    )
                except Exception as e:
                    raise RuntimeError(f"Malaysia (Bursa) resolve failed: {e}") from e
                stock_code = resolved["company_number"]
                meta = {
                    **meta,
                    "company_name": resolved.get("title") or meta.get("company_name"),
                    "company_number": stock_code,
                    "matched_via": resolved.get("matched_via"),
                }
                JOBS[job_id]["source_meta"] = meta
                my_label = (meta.get("ticker") or "").strip() or stock_code
                new_filename = f"{my_label}_MY_{meta.get('category', 'annual')}.pdf"
                JOBS[job_id]["filename"] = new_filename
                pdf_path = job_dir / new_filename
                pdf_stem = Path(new_filename).stem

            def _ocr_cb_my(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_my_filing_as_pdf(
                    company_number=stock_code,
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name"),
                    ocr_progress=_ocr_cb_my,
                )
            except Exception as e:
                raise RuntimeError(f"Malaysia (Bursa) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass
            JOBS[job_id]["source_meta"] = {**meta, **info}
            ocr_meta = info.get("ocr") or {}
            ocr_note = (f" · OCR {ocr_meta.get('pages')}p"
                        if ocr_meta.get("ocr") else "")
            _mark_stage(job_id, "my_fetch",
                        duration=time.time() - stage_start, cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"period {info.get('report_period','?')}{ocr_note}"
                        ))

        # ── Optional Stage 0 (TH): Thailand / SEC 56-1 One Report fetch + OCR ──
        # Resolve ticker/name -> SEC 56-1 ZIP file id in the background. Plain
        # HTTP via the regulator (SEC iDisc) — the SET exchange portal is
        # Akamai-walled, but market.sec.or.th is not.
        if job.get("source") == "thailand":
            update_job(job_id, status="processing",
                       current_stage="th_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            fileid = meta.get("company_number")
            fiscal_year = meta.get("fiscal_year")
            if not fileid:
                try:
                    resolved = resolve_th_company_number(
                        (meta.get("ticker") or "").strip(),
                        (meta.get("company_name") or "").strip() or None,
                    )
                except Exception as e:
                    raise RuntimeError(f"Thailand (SEC) resolve failed: {e}") from e
                fileid = resolved["company_number"]
                fiscal_year = resolved.get("year")
                meta = {
                    **meta,
                    "company_name": resolved.get("title") or meta.get("company_name"),
                    "company_number": fileid,
                    "fiscal_year": fiscal_year,
                    "matched_via": resolved.get("matched_via"),
                }
                JOBS[job_id]["source_meta"] = meta
                th_label = (meta.get("ticker") or "").strip() or "company"
                new_filename = f"{th_label}_TH_{meta.get('category', 'annual')}.pdf"
                JOBS[job_id]["filename"] = new_filename
                pdf_path = job_dir / new_filename
                pdf_stem = Path(new_filename).stem

            def _ocr_cb_th(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_th_filing_as_pdf(
                    company_number=fileid,
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name"),
                    ocr_progress=_ocr_cb_th,
                    fiscal_year=fiscal_year,
                )
            except Exception as e:
                raise RuntimeError(f"Thailand (SEC) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass
            JOBS[job_id]["source_meta"] = {**meta, **info}
            ocr_meta = info.get("ocr") or {}
            ocr_note = (f" · OCR {ocr_meta.get('pages')}p"
                        if ocr_meta.get("ocr") else "")
            _mark_stage(job_id, "th_fetch",
                        duration=time.time() - stage_start, cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"period {info.get('report_period','?')}{ocr_note}"
                        ))

        # ── Optional Stage 0 (IL): Israel / TASE-MAYA fetch + OCR ──
        # Resolve ticker/name -> MAYA companyId in the background (deferred from
        # the request handler, like China). The fetch routes through Firecrawl's
        # stealth proxy (TASE is Incapsula-walled); the report PDF itself comes
        # straight off mayafiles.tase.co.il (not walled).
        if job.get("source") == "israel":
            update_job(job_id, status="processing",
                       current_stage="il_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            company_id = meta.get("company_id") or meta.get("company_number")
            if not company_id:
                try:
                    resolved = resolve_il_company_number(
                        (meta.get("ticker") or "").strip(),
                        (meta.get("company_name") or "").strip() or None,
                    )
                except Exception as e:
                    raise RuntimeError(f"Israel (MAYA) resolve failed: {e}") from e
                company_id = resolved["company_number"]
                meta = {
                    **meta,
                    "company_name": resolved.get("title") or meta.get("company_name"),
                    "company_id": company_id,
                    "company_number": company_id,
                    "matched_via": resolved.get("matched_via"),
                }
                JOBS[job_id]["source_meta"] = meta
                il_label = (meta.get("ticker") or "").strip() or company_id
                new_filename = f"{il_label}_IL_{meta.get('category', 'annual')}.pdf"
                JOBS[job_id]["filename"] = new_filename
                pdf_path = job_dir / new_filename
                pdf_stem = Path(new_filename).stem

            def _ocr_cb_il(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = fetch_il_filing_as_pdf(
                    company_number=company_id,
                    category=meta.get("category", "annual"),
                    out_pdf_path=pdf_path,
                    company_name=meta.get("company_name"),
                    ocr_progress=_ocr_cb_il,
                )
            except Exception as e:
                raise RuntimeError(f"Israel (MAYA) fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            JOBS[job_id]["source_meta"] = {**meta, **info}

            ocr_meta = info.get("ocr") or {}
            ocr_note = (
                f" · OCR {ocr_meta.get('pages')}p"
                if ocr_meta.get("ocr") else ""
            )
            _mark_stage(job_id, "il_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company','?')} · {info.get('form','?')} · "
                            f"period {info.get('report_period','?')}{ocr_note}"
                        ))

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if not anthropic_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in .env")

        together_key = os.environ.get("TOGETHER_API_KEY")
        together_model = os.environ.get(
            "TOGETHER_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo"
        )

        cost_tracker = CostTracker(together_model=together_model)

        # ── Optional Stage 0 (DIAMOND): market-agnostic router ─────
        # Flagship: company name + ticker, ANY market. Tries the country's
        # dedicated integration → EDGAR → universal IR-scraper. First valid
        # PDF wins. (diamond_route reuses the same fetchers as the other tabs.)
        if job.get("source") == "diamond":
            update_job(job_id, status="processing",
                       current_stage="diamond_fetch", progress=2)
            stage_start = time.time()
            meta = job.get("source_meta") or {}

            def _ocr_cb_dia(done: int, total: int):
                if total:
                    update_job(job_id, progress=2 + int(6 * done / total))

            try:
                info = diamond_route.fetch_for_diamond(
                    company_name=(meta.get("company_name") or "").strip(),
                    ticker=(meta.get("ticker") or "").strip(),
                    out_pdf_path=pdf_path,
                    category=meta.get("category", "annual"),
                    progress=_ocr_cb_dia,
                    log=lambda m: print(f"[diamond {job_id}] {m}", file=sys.stderr),
                    country=(meta.get("country") or "").strip(),
                    allow_edgar_fallback=not meta.get("no_edgar_fallback", False),
                )
            except Exception as e:
                raise RuntimeError(f"Diamond fetch failed: {e}") from e

            try:
                JOBS[job_id]["file_size"] = pdf_path.stat().st_size
            except Exception:
                pass

            JOBS[job_id]["source_meta"] = {**meta, **info}

            _mark_stage(job_id, "diamond_fetch",
                        duration=time.time() - stage_start,
                        cost=0,
                        details=(
                            f"{info.get('company') or meta.get('company_name','?')} · "
                            f"via {info.get('diamond_source','?')} · "
                            f"{info.get('form','?')} · period {info.get('report_period','?')}"
                        ))

        # ── Stage 1 + 2: page detection ────────────────────────────
        update_job(job_id, status="processing",
                   current_stage="stage1_keywords", progress=5)

        together_client = None
        if together_key and together_key != "your_together_key_here":
            try:
                together_client = OpenAI(
                    api_key=together_key,
                    base_url="https://api.together.xyz/v1",
                )
            except Exception:
                together_client = None

        stage_start = time.time()
        target_pages, classifications = detect_relevant_pages(
            str(pdf_path),
            together_client=together_client,
            together_model=together_model,
            skip_llm=together_client is None,
            debug=False,
            cost_tracker=cost_tracker,
        )
        detect_duration = time.time() - stage_start

        _mark_stage(job_id, "stage1_keywords",
                    duration=min(detect_duration * 0.2, 3.0),
                    cost=0,
                    details=f"Scanned PDF, {len(target_pages)} candidate page(s) found")

        stage2_cost = cost_tracker.together_cost()
        update_job(job_id, current_stage="stage2_classifier", progress=20,
                   cost_so_far=stage2_cost)

        _mark_stage(job_id, "stage2_classifier",
                    duration=detect_duration * 0.8,
                    cost=round(stage2_cost, 4),
                    details=f"{len(target_pages)} page(s) confirmed")

        if not target_pages:
            # Enrich the failure context with the report's fiscal year so the user
            # message can read "…for <company> in FY<year>". Only when the fetch
            # metadata didn't already carry a year (scraper/Diamond set report_period).
            _meta = JOBS[job_id].get("source_meta") or {}
            if not any(_meta.get(k) for k in
                       ("report_period", "fiscal_year", "report_year", "year", "period")):
                _yr = _detect_report_year(pdf_path)
                if _yr:
                    JOBS[job_id]["source_meta"] = {**_meta, "report_year": _yr}
            raise RuntimeError("No relevant pages detected in PDF")

        # ── Stage 3: Claude extraction ─────────────────────────────
        update_job(job_id, current_stage="stage3_extraction", progress=30)
        stage_start = time.time()

        texts = extract_text_from_pages(str(pdf_path), target_pages)
        images = rasterize_pages(str(pdf_path), target_pages)

        # US filings: bypass the extraction disk cache (always re-extract fresh).
        # Cache stays enabled for every other market. US = EDGAR-sourced jobs,
        # country metadata says US, or an uploaded US SEC form type (10-K/10-Q/
        # 20-F/40-F) recognizable from the filename.
        _us_job = _is_us_job(job)
        if _us_job:
            log_line = f"[{job_id}] US filing detected — extraction cache bypassed"
            print(log_line, flush=True)

        client = anthropic.Anthropic(api_key=anthropic_key)
        batch_size = 12
        all_results = []
        for i in range(0, len(target_pages), batch_size):
            batch = target_pages[i:i + batch_size]
            bt = {pg: texts[pg] for pg in batch if pg in texts}
            bi = {pg: images[pg] for pg in batch if pg in images}
            result = extract_with_claude(
                client, bt, bi, "claude-sonnet-4-6",
                use_vision=True,
                skip_validation=False,
                cost_tracker=cost_tracker,
                use_cache=not _us_job,
            )
            all_results.append(result)

        if not all_results:
            final = {"company_name": None, "report_period": None,
                     "currency": None, "plans": []}
        elif len(all_results) == 1:
            final = all_results[0]
        else:
            final = merge_results(all_results)

        stage3_cost = cost_tracker.anthropic_cost()
        _mark_stage(job_id, "stage3_extraction",
                    duration=time.time() - stage_start,
                    cost=round(stage3_cost, 4),
                    details=f"{len(final.get('plans', []))} plan(s) extracted")

        update_job(job_id, current_stage="validation", progress=80,
                   cost_so_far=stage2_cost + stage3_cost)

        # ── Validation ─────────────────────────────────────────────
        stage_start = time.time()
        final = validate_all_plans(final)
        final = validate_final_output(final)
        _mark_stage(job_id, "validation",
                    duration=time.time() - stage_start,
                    cost=0,
                    details="Roll-forward math validated")

        # ── Meta block ─────────────────────────────────────────────
        final["_meta"] = {
            "source_pdf": job["filename"],
            "source": job.get("source", "upload"),
            "source_meta": job.get("source_meta") or {},
            "total_pdf_pages": _pdf_page_count(pdf_path),
            "pages_processed": target_pages,
            "mode": "vision+text",
            "model": "claude-sonnet-4-6",
            "validation_pass": True,
            "detection": {
                "stage2_classifier": together_model if together_client else "skipped",
                "classifications": {
                    str(pg): {
                        "decision": classifications[pg].get("decision"),
                        "confidence": classifications[pg].get("confidence"),
                        "reason": classifications[pg].get("reason"),
                    }
                    for pg in target_pages if pg in classifications
                },
            },
            "cost": cost_tracker.summary(),
        }

        # Store this extraction in the results cache so an identical later request
        # (same filing identity) is served instantly without re-extracting.
        if results_key is not None:
            try:
                cache.set("results", final, *results_key)
            except Exception:
                pass

        # ── Excel + DB save ────────────────────────────────────────
        update_job(job_id, current_stage="excel_generation", progress=90)
        stage_start = time.time()

        json_path = job_dir / "extraction.json"
        excel_path = job_dir / f"{pdf_stem}_options.xlsx"

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(final, f, indent=2, ensure_ascii=False)

        build_workbook(str(json_path), str(excel_path))
        xlsx_bytes = excel_path.read_bytes()

        extraction_id = None
        try:
            extraction_id = save_extraction(final, xlsx_bytes, excel_path.name)
        except Exception as e:
            print(f"WARNING: NeonDB save failed: {e}", file=sys.stderr)

        _mark_stage(job_id, "excel_generation",
                    duration=time.time() - stage_start,
                    cost=0,
                    details="Excel workbook generated")

        total_cost = cost_tracker.total_cost()
        update_job(
            job_id,
            status="completed",
            progress=100,
            current_stage=None,
            result_available=True,
            cost_so_far=round(total_cost, 4),
            extraction_id=extraction_id,
        )

    except Exception as e:
        traceback.print_exc()
        code, ctx = classify_failure(JOBS.get(job_id, {}), e)
        # EU tab: if the annual report had no option data but the scraper also saved a
        # recent interim/quarterly, advertise it so the frontend can offer a retry.
        # Only EU jobs ever set `alt_report_path`, so this stays naturally EU-scoped.
        extra: dict = {}
        _m = (JOBS.get(job_id, {}).get("source_meta") or {})
        _alt = _m.get("alt_report_path")
        if code == "NO_PAGES" and _alt and Path(_alt).exists():
            extra = {
                "alt_report_available": True,
                "alt_report_kind": _m.get("alt_report_kind", "interim"),
                "alt_report_year": _m.get("alt_report_year"),
            }
        update_job(job_id, status="failed", error=str(e)[:500],
                   error_code=code, error_context=ctx, **extra)


def run_scrape_test(job_id: str):
    """TESTING worker — scraper only, NO LLM. Routes company name + ticker through
    the same Diamond fetcher to download the latest filing PDF, then stops.
    Reports Firecrawl credits used (derived count + live ledger delta) and the
    wall-clock time. Writes a `scrape_test`-shaped extraction.json the frontend
    reads."""
    job = JOBS[job_id]
    job_dir = get_job_dir(job_id)
    pdf_path = job_dir / job["filename"]
    meta = job.get("source_meta") or {}

    try:
        update_job(job_id, status="processing",
                   current_stage="scrape_fetch", progress=5)

        # Reset this thread's scrape counter and snapshot the live ledger so we
        # can report both a derived estimate and the real billed delta (1c).
        fc_client.reset_tracking()
        ledger_before = fc_client.credit_usage()

        def _prog(done: int, total: int):
            if total:
                update_job(job_id, progress=5 + int(85 * done / total))

        stage_start = time.time()
        try:
            info = diamond_route.fetch_for_diamond(
                company_name=(meta.get("company_name") or "").strip(),
                ticker=(meta.get("ticker") or "").strip(),
                out_pdf_path=pdf_path,
                category=meta.get("category", "annual"),
                progress=_prog,
                log=lambda m: print(f"[scrape-test {job_id}] {m}", file=sys.stderr),
                country=(meta.get("country") or "").strip(),
            )
        except Exception as e:
            raise RuntimeError(f"Scrape failed: {e}") from e

        elapsed = time.time() - stage_start
        tracking = fc_client.get_tracking()
        ledger_after = fc_client.credit_usage()
        ledger_delta = None
        if ledger_before is not None and ledger_after is not None:
            ledger_delta = max(0, ledger_before - ledger_after)

        try:
            size = pdf_path.stat().st_size
        except Exception:
            size = 0
        JOBS[job_id]["file_size"] = size

        result = {
            "mode": "scrape_test",
            "company": info.get("company") or meta.get("company_name") or meta.get("ticker"),
            "ticker": meta.get("ticker") or "",
            "diamond_source": info.get("diamond_source"),
            "form": info.get("form"),
            "report_period": info.get("report_period"),
            "url": info.get("url"),
            "pdf_filename": job["filename"],
            "pdf_size": size,
            "elapsed_seconds": round(elapsed, 2),
            "firecrawl": {
                "scrapes": tracking["scrapes"],
                "credits_derived": tracking["credits"],
                "ledger_delta": ledger_delta,
            },
        }

        json_path = job_dir / "extraction.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        fc = result["firecrawl"]
        ledger_note = (
            f" · ledger Δ {fc['ledger_delta']}"
            if fc["ledger_delta"] is not None else ""
        )
        _mark_stage(job_id, "scrape_fetch",
                    duration=elapsed,
                    cost=0,
                    details=(
                        f"{result['company']} · via {info.get('diamond_source','?')} · "
                        f"{fc['scrapes']} scrape(s) → ~{fc['credits_derived']} credits{ledger_note}"
                    ))

        update_job(job_id, status="completed", progress=100,
                   current_stage=None, result_available=True)

    except Exception as e:
        traceback.print_exc()
        code, ctx = classify_failure(JOBS.get(job_id, {}), e)
        update_job(job_id, status="failed", error=str(e)[:500],
                   error_code=code, error_context=ctx)


def _pdf_page_count(pdf_path: Path) -> int:
    try:
        import fitz
        with fitz.open(pdf_path) as doc:
            return len(doc)
    except Exception:
        return 0


# ═════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ═════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Pavaki Options Extractor API",
    description="Extract share-based compensation data from annual reports",
    version="1.0.0",
)

_default_origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
_extra_origins = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_default_origins + _extra_origins,
    allow_origin_regex=os.environ.get("CORS_ORIGIN_REGEX") or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"status": "healthy", "active_jobs": len(JOBS)}


@app.post("/api/extract")
async def extract_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {MAX_FILE_SIZE // 1024 // 1024} MB)",
        )

    job_id = create_job(file.filename, len(contents))
    job_dir = get_job_dir(job_id)
    pdf_path = job_dir / file.filename
    with open(pdf_path, "wb") as f:
        f.write(contents)

    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": file.filename,
        "file_size": len(contents),
    }


class EdgarExtractRequest(BaseModel):
    ticker: str
    company_name: Optional[str] = None
    form: str = "10-K"


@app.post("/api/extract-from-edgar")
async def extract_from_edgar(
    payload: EdgarExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Fetch the latest filing for a US-listed ticker and run the
    standard extraction pipeline against the resulting PDF."""
    ticker = (payload.ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")

    form = (payload.form or "10-K").strip().upper()
    filename = f"{ticker}_{form.replace('/', '-')}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="edgar",
        source_meta={
            "ticker": ticker,
            "company_name": payload.company_name,
            "form": form,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "edgar",
        "ticker": ticker,
        "form": form,
    }


class DiamondExtractRequest(BaseModel):
    # Optional so a JSON null (e.g. an empty country dropdown) doesn't 422 — the
    # handler coerces None -> "" via `(payload.x or "").strip()`.
    company_name: Optional[str] = ""
    ticker: Optional[str] = ""
    category: Optional[str] = "annual"
    country: Optional[str] = ""


@app.post("/api/extract-from-diamond")
async def extract_from_diamond(
    payload: DiamondExtractRequest,
    background_tasks: BackgroundTasks,
):
    """💎 Diamond (flagship): company name + ticker, ANY market. Routes to the
    country's dedicated integration → EDGAR → universal IR-scraper, then runs the
    standard extraction pipeline against whatever report it finds."""
    company_name = (payload.company_name or "").strip()
    ticker = (payload.ticker or "").strip()
    if not company_name and not ticker:
        raise HTTPException(
            status_code=400, detail="company_name or ticker is required"
        )

    label = _safe_filename_part(ticker or company_name)
    filename = f"{label}_DIAMOND.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="diamond",
        source_meta={
            "company_name": company_name,
            "ticker": ticker,
            "category": payload.category or "annual",
            "country": (payload.country or "").strip(),
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "diamond",
        "company_name": company_name,
        "ticker": ticker,
    }


class SingaporeExtractRequest(BaseModel):
    # Singapore (SGX): company name + ticker. Country is forced to "Singapore"
    # server-side; the ticker is auto-prefixed with "SGX:" for the scraper.
    company_name: Optional[str] = ""
    ticker: Optional[str] = ""
    category: Optional[str] = "annual"


@app.post("/api/extract-from-singapore")
async def extract_from_singapore(
    payload: SingaporeExtractRequest,
    background_tasks: BackgroundTasks,
):
    """🇸🇬 Singapore (SGX): company name + ticker. Uses the SAME scraper framework
    as Diamond (universal IR-scraper) but locked to Singapore — country is fixed to
    "Singapore" and the ticker is auto-prefixed with "SGX:" for scraping/resolution
    (e.g. the user enters Z77 -> internally SGX:Z77)."""
    company_name = (payload.company_name or "").strip()
    raw_ticker = (payload.ticker or "").strip()
    if not company_name and not raw_ticker:
        raise HTTPException(
            status_code=400, detail="company_name or ticker is required"
        )

    # Prefix the SGX exchange code for the scraper (Z77 -> SGX:Z77), unless the
    # user already typed an SGX: prefix.
    if raw_ticker and not raw_ticker.upper().startswith("SGX:"):
        ticker = f"SGX:{raw_ticker}"
    else:
        ticker = raw_ticker

    label = _safe_filename_part(ticker or company_name)
    filename = f"{label}_SINGAPORE.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="diamond",
        source_meta={
            "company_name": company_name,
            "ticker": ticker,
            "category": payload.category or "annual",
            "country": "Singapore",
            # Locked to SGX issuers — never fall back to US SEC EDGAR (a name match
            # there would fetch an unrelated US filer's report).
            "no_edgar_fallback": True,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "diamond",
        "company_name": company_name,
        "ticker": ticker,
    }


class MexicoExtractRequest(BaseModel):
    # Mexico (BMV): company name + ticker. Country is forced to "Mexico" server-side;
    # the ticker is auto-prefixed with "BMV:" for the scraper.
    company_name: Optional[str] = ""
    ticker: Optional[str] = ""
    category: Optional[str] = "annual"


@app.post("/api/extract-from-mexico")
async def extract_from_mexico(
    payload: MexicoExtractRequest,
    background_tasks: BackgroundTasks,
):
    """🇲🇽 Mexico (BMV): company name + ticker. Uses the SAME scraper framework as
    Diamond (universal IR-scraper) but locked to Mexico — country is fixed to "Mexico"
    and the ticker is auto-prefixed with "BMV:" for scraping/resolution (e.g. the user
    enters WALMEX -> internally BMV:WALMEX). Mirrors the Singapore tab; the US-EDGAR
    fallback is disabled so a name match never returns an unrelated US filer."""
    company_name = (payload.company_name or "").strip()
    raw_ticker = (payload.ticker or "").strip()
    if not company_name and not raw_ticker:
        raise HTTPException(
            status_code=400, detail="company_name or ticker is required"
        )

    # Prefix the BMV exchange code for the scraper (WALMEX -> BMV:WALMEX), unless the
    # user already typed a BMV: prefix.
    if raw_ticker and not raw_ticker.upper().startswith("BMV:"):
        ticker = f"BMV:{raw_ticker}"
    else:
        ticker = raw_ticker

    label = _safe_filename_part(ticker or company_name)
    filename = f"{label}_MEXICO.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="diamond",
        source_meta={
            "company_name": company_name,
            "ticker": ticker,
            "category": payload.category or "annual",
            "country": "Mexico",
            # Mirror Singapore: locked to BMV issuers — never fall back to US SEC EDGAR.
            "no_edgar_fallback": True,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "diamond",
        "company_name": company_name,
        "ticker": ticker,
    }


class ScrapeTestRequest(BaseModel):
    # TESTING tab: company name + ticker only (country optional, used for routing).
    company_name: Optional[str] = ""
    ticker: Optional[str] = ""
    country: Optional[str] = ""
    category: Optional[str] = "annual"


@app.post("/api/scrape-test")
async def scrape_test(
    payload: ScrapeTestRequest,
    background_tasks: BackgroundTasks,
):
    """🧪 TESTING (scraper only): company name + ticker → fetch the latest filing
    PDF via the Diamond router and STOP. No LLM/extraction. Returns Firecrawl
    credits used and time taken; the PDF is downloadable via /api/download/{id}/pdf."""
    company_name = (payload.company_name or "").strip()
    ticker = (payload.ticker or "").strip()
    if not company_name and not ticker:
        raise HTTPException(
            status_code=400, detail="company_name or ticker is required"
        )

    label = _safe_filename_part(ticker or company_name)
    filename = f"{label}_TEST.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="scrape_test",
        source_meta={
            "company_name": company_name,
            "ticker": ticker,
            "category": payload.category or "annual",
            "country": (payload.country or "").strip(),
        },
    )
    background_tasks.add_task(run_scrape_test, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "scrape_test",
        "company_name": company_name,
        "ticker": ticker,
    }


class UkExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "accounts"


@app.post("/api/extract-from-uk")
async def extract_from_uk(
    payload: UkExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve a UK-listed ticker (or company name) to its Companies House
    company number, fetch the latest accounts filing, OCR it if scanned, and
    run the standard extraction pipeline."""
    ticker = (payload.ticker or "").strip().upper()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )

    # Resolve to a Companies House company number (fail fast with a clear error).
    try:
        resolved = resolve_company_number(ticker, company_name or None)
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Could not find a UK company for "
                f"{ticker or company_name!r}: {e}"
            ),
        )

    company_number = resolved["company_number"]
    title = resolved.get("title") or company_name or ticker
    category = (payload.category or "accounts").strip().lower()

    label = ticker or company_number
    filename = f"{label}_UK_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="companies_house",
        source_meta={
            "ticker": ticker,
            "company_name": title,
            "company_number": company_number,
            "category": category,
            "matched_via": resolved.get("matched_via"),
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "companies_house",
        "ticker": ticker,
        "company_name": title,
        "company_number": company_number,
        "category": category,
    }


class DkExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-denmark")
async def extract_from_denmark(
    payload: DkExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve a Danish ticker (or company name) to its CVR number, fetch the
    latest annual report (ESEF/iXBRL rendered to PDF, or a scanned PDF OCR'd),
    and run the standard extraction pipeline."""
    ticker = (payload.ticker or "").strip().upper()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )

    try:
        resolved = resolve_dk_company_number(ticker, company_name or None)
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Could not find a Danish company for "
                f"{ticker or company_name!r}: {e}"
            ),
        )

    company_number = resolved["company_number"]
    title = resolved.get("title") or company_name or ticker
    category = (payload.category or "annual").strip().lower()

    label = ticker or company_number
    filename = f"{label}_DK_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="denmark",
        source_meta={
            "ticker": ticker,
            "company_name": title,
            "company_number": company_number,
            "category": category,
            "matched_via": resolved.get("matched_via"),
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "denmark",
        "ticker": ticker,
        "company_name": title,
        "company_number": company_number,
        "category": category,
    }


class JpExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-japan")
async def extract_from_japan(
    payload: JpExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve a Japanese ticker (4-digit TSE securities code) or company name to
    its EDINET code, fetch the latest annual securities report (有価証券報告書)
    PDF from EDINET, and run the standard extraction pipeline."""
    ticker = (payload.ticker or "").strip().upper()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )

    # Resolve the EDINET code BEST-EFFORT only — the IR scraper runs first and
    # doesn't need it; EDINET is just the fallback (used only when a key is set).
    # A company that isn't in EDINET's list must NOT block the scraper path.
    try:
        resolved = resolve_jp_company_number(ticker, company_name or None)
    except Exception:
        resolved = {}

    edinet_code = resolved.get("company_number")
    title = resolved.get("title") or company_name or ticker
    category = (payload.category or "annual").strip().lower()

    label = ticker or edinet_code or "company"
    filename = f"{label}_JP_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="japan",
        source_meta={
            "ticker": ticker,
            "company_name": title,
            "edinet_code": edinet_code,
            "company_number": edinet_code,
            "securities_code": resolved.get("securities_code"),
            "category": category,
            "matched_via": resolved.get("matched_via"),
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "japan",
        "ticker": ticker,
        "company_name": title,
        "edinet_code": edinet_code,
        "securities_code": resolved.get("securities_code"),
        "category": category,
    }


class KrExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-korea")
async def extract_from_korea(
    payload: KrExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve a Korean ticker (6-digit KRX code) or company name to its DART
    corp_code, fetch the latest annual report (사업보고서) PDF from DART, and run
    the standard extraction pipeline."""
    ticker = (payload.ticker or "").strip().upper()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )

    category = (payload.category or "annual").strip().lower()

    # NOTE: resolving the ticker/name → DART corp_code can be slow on a cold
    # start (the full DART corp-code list is downloaded the first time). It is
    # therefore deferred to the background pipeline rather than run here, so
    # this request returns the job_id instantly and never trips the proxy/edge
    # timeout (which surfaced as a 524 through the Cloudflare tunnel).
    label = ticker or "company"
    filename = f"{label}_KR_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="korea",
        source_meta={
            "ticker": ticker,
            "company_name": company_name,
            "category": category,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "korea",
        "ticker": ticker,
        "company_name": company_name,
        "category": category,
    }


class BrExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-brazil")
async def extract_from_brazil(
    payload: BrExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve a Brazilian B3 ticker (e.g. PETR4), CNPJ, or company name to its
    CVM code, fetch the latest annual financial statements (DFP) PDF from CVM's
    open data, and run the standard extraction pipeline."""
    ticker = (payload.ticker or "").strip().upper()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )

    category = (payload.category or "annual").strip().lower()

    # Resolve is deferred to the background pipeline (the cadastral list download
    # can be slow on a cold start) so this request returns the job_id instantly
    # and never trips the proxy/edge timeout — same pattern as Korea.
    label = ticker or "company"
    filename = f"{label}_BR_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="brazil",
        source_meta={
            "ticker": ticker,
            "company_name": company_name,
            "category": category,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "brazil",
        "ticker": ticker,
        "company_name": company_name,
        "category": category,
    }


class TwExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-taiwan")
async def extract_from_taiwan(
    payload: TwExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve a Taiwanese 4-digit stock code (e.g. 2330) or company name to its
    TWSE code, fetch the latest annual consolidated financial statements PDF from
    the TWSE document service, and run the standard extraction pipeline."""
    ticker = (payload.ticker or "").strip()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )

    category = (payload.category or "annual").strip().lower()

    label = ticker or "company"
    filename = f"{label}_TW_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="taiwan",
        source_meta={
            "ticker": ticker,
            "company_name": company_name,
            "category": category,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "taiwan",
        "ticker": ticker,
        "company_name": company_name,
        "category": category,
    }


class CnExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-china")
async def extract_from_china(
    payload: CnExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve a Chinese 6-digit stock code (e.g. 600519) or company name to its
    CNINFO orgId, fetch the latest annual report (年度报告) PDF from CNINFO, and
    run the standard extraction pipeline."""
    ticker = (payload.ticker or "").strip()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )

    category = (payload.category or "annual").strip().lower()

    label = ticker or "company"
    filename = f"{label}_CN_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="china",
        source_meta={
            "ticker": ticker,
            "company_name": company_name,
            "category": category,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "china",
        "ticker": ticker,
        "company_name": company_name,
        "category": category,
    }


class InExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-india")
async def extract_from_india(
    payload: InExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve an Indian ticker (e.g. RELIANCE), BSE scrip code, ISIN or company
    name to its BSE scrip code, fetch the latest annual-report PDF from BSE, and
    run the standard extraction pipeline."""
    ticker = (payload.ticker or "").strip()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )

    category = (payload.category or "annual").strip().lower()

    label = ticker or "company"
    filename = f"{label}_IN_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="india",
        source_meta={
            "ticker": ticker,
            "company_name": company_name,
            "category": category,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "india",
        "ticker": ticker,
        "company_name": company_name,
        "category": category,
    }


class HkExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-hongkong")
async def extract_from_hongkong(
    payload: HkExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve a Hong Kong stock code (e.g. 700) or company name to its HKEXnews
    stockId, fetch the latest annual-report PDF from HKEXnews, and run the
    standard extraction pipeline."""
    ticker = (payload.ticker or "").strip()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )

    category = (payload.category or "annual").strip().lower()

    label = ticker or "company"
    filename = f"{label}_HK_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="hongkong",
        source_meta={
            "ticker": ticker,
            "company_name": company_name,
            "category": category,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "hongkong",
        "ticker": ticker,
        "company_name": company_name,
        "category": category,
    }


class IdExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-indonesia")
async def extract_from_indonesia(
    payload: IdExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Fetch the latest audited annual financial statements PDF from IDX for an
    Indonesian ticker code (kodeEmiten, e.g. BBCA / GOTO) and run the standard
    extraction pipeline. IDX is keyed by ticker directly (no resolver)."""
    ticker = (payload.ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(
            status_code=400, detail="ticker (IDX code, e.g. BBCA) is required"
        )

    category = (payload.category or "annual").strip().lower()
    filename = f"{ticker}_ID_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="indonesia",
        source_meta={
            "ticker": ticker,
            "company_name": (payload.company_name or "").strip(),
            "category": category,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "indonesia",
        "ticker": ticker,
        "category": category,
    }


class IlExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-israel")
async def extract_from_israel(
    payload: IlExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve a TASE listing (numeric MAYA companyId, English ticker, or major
    issuer name) to its companyId, fetch the latest annual/periodic financial
    statements PDF from TASE-MAYA (via Firecrawl stealth; PDF off mayafiles), and
    run the standard extraction pipeline. NOTE: TASE's data API is bot-walled, so
    name/ticker resolution is limited to major issuers — the numeric companyId
    (shown in the maya.tase.co.il/en/companies/<id> URL) works for any company."""
    ticker = (payload.ticker or "").strip()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker, companyId or company_name is required"
        )

    category = (payload.category or "annual").strip().lower()
    label = ticker or "company"
    filename = f"{label}_IL_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="israel",
        source_meta={
            "ticker": ticker,
            "company_name": company_name,
            "category": category,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "israel",
        "ticker": ticker,
        "company_name": company_name,
        "category": category,
    }


class MyExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-malaysia")
async def extract_from_malaysia(
    payload: MyExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve a Bursa Malaysia listing (stock code, short-name ticker, or company
    name) to its stock code, fetch the latest annual-report financial statements
    PDF off Bursa (plain HTTP — no bot wall), and run the standard pipeline."""
    ticker = (payload.ticker or "").strip()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker, stock code or company_name is required"
        )
    category = (payload.category or "annual").strip().lower()
    label = ticker or "company"
    filename = f"{label}_MY_{category}.pdf"
    job_id = create_job(
        filename=filename, file_size=0, source="malaysia",
        source_meta={"ticker": ticker, "company_name": company_name, "category": category},
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)
    return {
        "job_id": job_id, "status": "queued", "filename": filename,
        "source": "malaysia", "ticker": ticker, "company_name": company_name,
        "category": category,
    }


class ThExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-thailand")
async def extract_from_thailand(
    payload: ThExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve a Thai listing (major-issuer ticker or company name) to its SEC
    56-1 One Report, fetch the report PDF off the SEC iDisc service (plain HTTP —
    the SET exchange portal is bot-walled, the regulator is not), and run the
    standard pipeline. The SEC listing has no ticker column, so company name
    resolves most reliably."""
    ticker = (payload.ticker or "").strip()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )
    category = (payload.category or "annual").strip().lower()
    label = ticker or "company"
    filename = f"{label}_TH_{category}.pdf"
    job_id = create_job(
        filename=filename, file_size=0, source="thailand",
        source_meta={"ticker": ticker, "company_name": company_name, "category": category},
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)
    return {
        "job_id": job_id, "status": "queued", "filename": filename,
        "source": "thailand", "ticker": ticker, "company_name": company_name,
        "category": category,
    }


class EuExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"
    country: Optional[str] = None
    lei: Optional[str] = None
    isin: Optional[str] = None


@app.get("/api/eu-search")
async def eu_search(q: str, limit: int = 10):
    """Autocomplete for the EU/EEA (ESEF) tab. Returns companies that actually
    have a downloadable ESEF report on filings.xbrl.org whose name matches `q`,
    with their LEI and country — one row per company, newest filing first."""
    query = (q or "").strip()
    if len(query) < 2:
        return {"query": query, "results": []}
    try:
        results = search_eu_companies(query, limit=max(1, min(limit, 25)))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"EU search failed: {e}")
    return {"query": query, "results": results}


@app.get("/api/gurufocus-resolve")
async def gurufocus_resolve(url: str):
    """EU tab: turn a GuruFocus stock URL (e.g.
    https://www.gurufocus.com/stock/OSL:AUSS/summary) into the pipeline inputs —
    {company_name, ticker, exchange, country, european}. The ticker + country come
    from the URL's exchange prefix; the company name from the page <h1> (one light
    request). `european=false` means a non-European listing (frontend then routes
    the user to the Diamond tab)."""
    try:
        return gurufocus.resolve((url or "").strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GuruFocus resolve failed: {e}")


@app.post("/api/extract-from-eu")
async def extract_from_eu(
    payload: EuExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Resolve an EU/EEA listing (company name, LEI, or ISIN) to its LEI, fetch
    the latest ESEF Annual Financial Report from filings.xbrl.org (the free
    pan-European repository), render it to PDF, and run the standard extraction
    pipeline. One endpoint covers every EU/EEA regulated-market issuer. When the
    frontend autocomplete already resolved the company, it passes `lei` directly
    and the background resolve is skipped. An optional `country` hint (ISO code,
    e.g. "FR") disambiguates name matches."""
    ticker = (payload.ticker or "").strip()
    company_name = (payload.company_name or "").strip()
    lei = (payload.lei or "").strip().upper() or None
    isin = (payload.isin or "").strip().upper() or None
    if not ticker and not company_name and not lei and not isin:
        raise HTTPException(
            status_code=400,
            detail="company_name, LEI, ISIN or ticker is required",
        )

    category = (payload.category or "annual").strip().lower()
    country = (payload.country or "").strip().upper() or None

    # Resolve (name/ISIN → LEI) is deferred to the background pipeline so this
    # request returns the job_id instantly and never trips the proxy/edge
    # timeout — same pattern as Korea/Brazil/Taiwan. When `lei` is supplied
    # (autocomplete already resolved it), the pipeline skips resolve entirely.
    label = ticker or lei or "company"
    filename = f"{label}_EU_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="eu",
        source_meta={
            "ticker": ticker,
            "company_name": company_name,
            "category": category,
            "country": country,
            "lei": lei,
            "isin": isin,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "eu",
        "ticker": ticker,
        "company_name": company_name,
        "category": category,
        "country": country,
        "lei": lei,
        "isin": isin,
    }


@app.post("/api/eu-try-alternate/{job_id}")
async def eu_try_alternate(job_id: str, background_tasks: BackgroundTasks):
    """EU tab only: re-run extraction on the interim/quarterly report the scraper
    already downloaded for `job_id`, when its annual report had no option data
    (NO_PAGES). No re-download — the saved PDF is copied into a fresh job and put
    through the standard Stage 1/2/3 pipeline. Returns the new job_id to poll."""
    import shutil

    orig = JOBS.get(job_id)
    if orig is None:
        raise HTTPException(status_code=404, detail="Job not found")
    meta = orig.get("source_meta") or {}
    alt_path = meta.get("alt_report_path")
    if not alt_path or not Path(alt_path).exists():
        raise HTTPException(status_code=404, detail="No alternate report available")

    company = (meta.get("company") or meta.get("company_name") or "").strip()
    year = meta.get("alt_report_year")
    label = (meta.get("ticker") or "").strip() or "company"
    new_filename = f"{label}_EU_interim.pdf"

    # Plain extraction job (source=None) -> the pipeline runs Stage 1/2/3 directly on
    # the PDF in its job dir, exactly like an uploaded file. Carry company/year so the
    # "no options data" copy stays friendly if the interim is empty too.
    new_job_id = create_job(
        filename=new_filename,
        file_size=Path(alt_path).stat().st_size,
        source_meta={
            "company_name": company,
            "company": company,
            "report_year": year,
            "report_period": year,
            "eu_path": "ir_scraper_interim",
        },
    )
    new_dir = get_job_dir(new_job_id)
    new_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(alt_path, new_dir / new_filename)

    background_tasks.add_task(run_extraction_pipeline, new_job_id)
    return {
        "job_id": new_job_id,
        "status": "queued",
        "filename": new_filename,
        "source": "eu",
        "report_kind": meta.get("alt_report_kind", "interim"),
        "report_year": year,
    }


class GermanyExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-germany")
async def extract_from_germany(
    payload: GermanyExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Germany by ticker/name. Germany has no open data API (Bundesanzeiger has none;
    DE issuers have ~0 ESEF filings), so this uses the SAME 'IR scraper first' strategy
    as the EU tab: scrape the company's own investor-relations site for the latest
    ANNUAL report, then fall back to SEC EDGAR (German blue-chips often file a 20-F).
    If both miss, the tab keeps a manual-upload box."""
    ticker = (payload.ticker or "").strip()
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required")

    category = (payload.category or "annual").strip().lower()
    label = ticker or "company"
    filename = f"{label}_DE_{category}.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="germany",
        source_meta={
            "ticker": ticker,
            "company_name": company_name,
            "category": category,
            "country": "Germany",
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "germany",
        "ticker": ticker,
        "company_name": company_name,
        "category": category,
    }


class CaExtractRequest(BaseModel):
    ticker: str = ""
    company_name: Optional[str] = None
    category: str = "annual"


@app.post("/api/extract-from-canada")
async def extract_from_canada(
    payload: CaExtractRequest,
    background_tasks: BackgroundTasks,
):
    """Fetch a Canadian issuer's annual report BY TICKER via SEC EDGAR. Canada's
    SEDAR+ is bot-walled, but most cross-listed Canadian issuers file an MJDS
    Form 40-F (or 20-F / 10-K) with the SEC; we pull the financial-statements
    exhibit from it and run the standard pipeline. Cross-listed (SEC-registered)
    issuers only — TSX-only issuers must be uploaded manually."""
    ticker = (payload.ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")

    category = (payload.category or "annual").strip().lower()
    company_name = (payload.company_name or "").strip() or None

    filename = f"{ticker}_CA_{category}.pdf"
    job_id = create_job(
        filename=filename,
        file_size=0,
        source="canada",
        source_meta={
            "ticker": ticker,
            "company_name": company_name,
            "category": category,
        },
    )
    background_tasks.add_task(run_extraction_pipeline, job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source": "canada",
        "ticker": ticker,
        "company_name": company_name,
        "category": category,
    }


@app.get("/api/job/{job_id}")
async def get_job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    job = JOBS[job_id].copy()
    job["elapsed_seconds"] = time.time() - job["start_time"]

    if job["status"] == "processing" and job["progress"] > 0:
        elapsed = job["elapsed_seconds"]
        estimated_total = elapsed / (job["progress"] / 100)
        job["estimated_remaining"] = max(0, estimated_total - elapsed)
    elif job["status"] == "completed":
        job["estimated_remaining"] = 0

    job.pop("start_time", None)
    return job


@app.get("/api/result/{job_id}")
async def get_result(job_id: str):
    # Like the Excel download: fall back to the on-disk result if the in-memory job
    # record was lost to a backend restart.
    job = JOBS.get(job_id)
    if job is not None and job["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Job not ready (status: {job['status']})",
        )

    json_path = get_job_dir(job_id) / "extraction.json"
    if not json_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Job not found" if job is None else "Result file not found",
        )

    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/download/{job_id}/excel")
async def download_excel(job_id: str):
    # Serve from disk even if the in-memory job record is gone (e.g. the backend was
    # restarted) — the generated workbook persists under jobs/<id>/. Only block the
    # download if the job IS in memory and hasn't finished yet.
    job = JOBS.get(job_id)
    if job is not None and job["status"] != "completed":
        raise HTTPException(status_code=409, detail="Job not completed")

    job_dir = get_job_dir(job_id)
    excel_files = list(job_dir.glob("*.xlsx")) if job_dir.exists() else []
    if not excel_files:
        raise HTTPException(
            status_code=404,
            detail="Job not found" if job is None else "Excel file not found",
        )

    excel_path = excel_files[0]
    return FileResponse(
        path=excel_path,
        filename=excel_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/download/{job_id}/pdf")
async def download_pdf(job_id: str):
    """Serve the fetched source PDF (used by the TESTING tab). Served from disk
    so it works even if the in-memory job record was lost to a restart."""
    job = JOBS.get(job_id)
    # Allow "failed" too: a NO_PAGES failure still fetched a valid source PDF the
    # user may want to inspect. Only block before the fetch has produced anything.
    if job is not None and job["status"] not in ("completed", "processing", "failed"):
        raise HTTPException(status_code=409, detail="PDF not ready")

    job_dir = get_job_dir(job_id)
    # Prefer the job's recorded filename; fall back to any PDF in the job dir.
    pdf_path = None
    if job is not None:
        candidate = job_dir / job.get("filename", "")
        if candidate.exists():
            pdf_path = candidate
    if pdf_path is None:
        pdfs = list(job_dir.glob("*.pdf")) if job_dir.exists() else []
        pdf_path = pdfs[0] if pdfs else None
    if pdf_path is None:
        raise HTTPException(
            status_code=404,
            detail="Job not found" if job is None else "PDF file not found",
        )

    return FileResponse(
        path=pdf_path,
        filename=pdf_path.name,
        media_type="application/pdf",
    )


@app.delete("/api/job/{job_id}")
async def cancel_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    JOBS[job_id]["status"] = "cancelled"
    job_dir = get_job_dir(job_id)
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    JOBS.pop(job_id, None)
    return {"status": "cancelled", "job_id": job_id}


@app.get("/api/jobs")
async def list_jobs():
    return {
        "total": len(JOBS),
        "jobs": [
            {
                "job_id": j["job_id"],
                "filename": j["filename"],
                "status": j["status"],
                "progress": j["progress"],
                "created_at": j["created_at"],
            }
            for j in JOBS.values()
        ],
    }


# ─── Simply Wall St forecast (standalone feature, same origin) ─────
# Separate from the options pipeline. Adds GET /simply (+ /api/simply,
# /api/simply/excel). Included BEFORE the StaticFiles catch-all below so
# the /simply route isn't swallowed by the SPA mount.
from simply_route import router as simply_router
app.include_router(simply_router)


# ─── Serve the built React frontend (single-origin) ────────────────
# Mounted AFTER all /api routes so the API always takes precedence.
from fastapi.staticfiles import StaticFiles

_DIST = Path(__file__).parent / "Frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

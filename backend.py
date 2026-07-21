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

import asyncio
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
from typing import Any, Dict, Optional

from core import cache

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Pipeline modules (live at project root) ──────────────────────────
from core.options import (
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
from markets.edgar_fetch import fetch_filing_as_pdf
from markets.companies_house_fetch import fetch_filing_as_pdf as fetch_uk_filing_as_pdf
from markets.uk_resolve import resolve_company_number
from markets.denmark_fetch import fetch_filing_as_pdf as fetch_dk_filing_as_pdf
from markets.dk_resolve import resolve_company_number as resolve_dk_company_number
from markets.japan_fetch import fetch_filing_as_pdf as fetch_jp_filing_as_pdf
from markets.jp_resolve import resolve_company_number as resolve_jp_company_number
from markets.kr_fetch import fetch_filing_as_pdf as fetch_kr_filing_as_pdf
from markets.kr_resolve import resolve_company_number as resolve_kr_company_number
from markets.br_fetch import fetch_filing_as_pdf as fetch_br_filing_as_pdf
from markets.br_resolve import resolve_company_number as resolve_br_company_number
from markets.tw_fetch import fetch_filing_as_pdf as fetch_tw_filing_as_pdf
from markets.tw_resolve import resolve_company_number as resolve_tw_company_number
from markets.eu_fetch import fetch_filing_as_pdf as fetch_eu_filing_as_pdf
from markets.eu_resolve import (
    resolve_company_number as resolve_eu_company_number,
    search_companies as search_eu_companies,
)
from markets.ca_fetch import fetch_filing_as_pdf as fetch_ca_filing_as_pdf
from markets.cn_fetch import fetch_filing_as_pdf as fetch_cn_filing_as_pdf
from markets.cn_resolve import resolve_company_number as resolve_cn_company_number
from markets.in_fetch import fetch_filing_as_pdf as fetch_in_filing_as_pdf
from markets.in_resolve import resolve_company_number as resolve_in_company_number
from markets.hk_fetch import fetch_filing_as_pdf as fetch_hk_filing_as_pdf
from markets.hk_resolve import resolve_company_number as resolve_hk_company_number
from markets.id_fetch import fetch_filing_as_pdf as fetch_id_filing_as_pdf
from markets.il_fetch import fetch_filing_as_pdf as fetch_il_filing_as_pdf
from markets.il_resolve import resolve_company_number as resolve_il_company_number
from markets.my_fetch import fetch_filing_as_pdf as fetch_my_filing_as_pdf
from markets.my_resolve import resolve_company_number as resolve_my_company_number
from markets.th_fetch import fetch_filing_as_pdf as fetch_th_filing_as_pdf
from markets.th_resolve import resolve_company_number as resolve_th_company_number
from routes import diamond_route
from core import fc_client
from routes import gurufocus

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


# ── IR-URL cache (ticker+country → resolved IR crawl entry URL) ────────────
# The IR resolver depends on search engines (DDG rate-limits randomly), so a
# resolve that worked minutes ago can fail on the next run — which silently
# downgrades the Japan/EU quarterly-first balance-sheet path to the EDGAR/ESEF
# annual fallback (MUFG live run 2026-07-16: transient resolve failure →
# 20-F instead of the tanshin). Successful CRAWLS (not bare resolutions —
# the URL must actually have yielded a report) are remembered here, same
# pattern as sws_url_cache.json. Best-effort file I/O; corruption = miss.
_IR_URL_CACHE_PATH = Path(__file__).parent / "ir_url_cache.json"
_IR_URL_CACHE_TTL_SEC = 30 * 86400


def _ir_url_cache_key(ticker: str, name: str, country: str) -> str:
    who = (ticker or "").strip().upper() or " ".join(
        (name or "").strip().lower().split())
    return f"{who}|{(country or '').strip().lower()}"


def _ir_url_cache_get(ticker: str, name: str, country: str):
    try:
        data = json.loads(_IR_URL_CACHE_PATH.read_text(encoding="utf-8"))
        ent = data.get(_ir_url_cache_key(ticker, name, country)) or {}
        if ent.get("url") and (time.time() - (ent.get("ts") or 0)
                               <= _IR_URL_CACHE_TTL_SEC):
            return ent["url"]
    except Exception:
        pass
    return None


def _ir_url_cache_put(ticker: str, name: str, country: str, url: str):
    try:
        try:
            data = json.loads(_IR_URL_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data[_ir_url_cache_key(ticker, name, country)] = {
            "url": url, "ts": time.time()}
        _IR_URL_CACHE_PATH.write_text(
            json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def _ir_scraper_fetch_annual(out_pdf_path, name, ticker, country, ocr_cb,
                             timeout_sec=EU_IR_SCRAPER_TIMEOUT_SEC):
    """Run the universal IR scraper for the latest ANNUAL report only, capped at
    `timeout_sec` wall-clock. Writes the PDF to out_pdf_path and returns the scraper
    info dict on success, else None. A timed-out scraper is abandoned (its orphan
    thread keeps running but only ever writes out_pdf_path, so it cannot race a
    subsequent fallback that writes a different file). Shared by the UK / Germany
    'IR scraper first' branches (EU and Japan use the dual annual+interim path)."""
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

            jp_ok = False
            # Step A: universal IR scraper FIRST, capped at 100s — the SAME dual
            # annual+interim crawl the EU tab / balance-sheet flow uses
            # (ir_resolve_proto + ir_fetch_proto.fetch_reports). One IR-page crawl
            # captures BOTH the latest annual report AND the latest recent
            # interim/quarterly report:
            #   - annual found        -> run the annual; keep the interim as a fallback
            #                            the user can opt into if the annual has no data.
            #   - only interim found  -> run the interim directly (no annual to prefer).
            #   - nothing within 100s -> abandon the scraper (its orphan thread only
            #                            writes its own _annual/_interim PDFs, never the
            #                            EDINET/pipeline PDF) and fall through to EDINET.
            if _jp_name or _jp_ticker:
                import concurrent.futures
                _annual_pdf = job_dir / f"{pdf_stem}_annual.pdf"
                _interim_pdf = job_dir / f"{pdf_stem}_interim.pdf"
                _results_pdf = job_dir / f"{pdf_stem}_results.pdf"
                # QUARTERLY-FIRST (balance-sheet / fetch-only jobs, user rule
                # 2026-07-16): prefer the quarterly-cadence financial-results
                # doc (JP tanshin "Summary Report" / "Financial Highlights" —
                # freshest period, real consolidated balance sheet) over the
                # annual report; annual → EDGAR 20-F → EDINET stay as
                # fallbacks. The options extraction pipeline (not fetch_only)
                # keeps annual-first — results docs carry no SBC note.
                _bs_mode = bool(job.get("fetch_only"))

                # fetch_reports records the annual into this sink the MOMENT it
                # passes the gate (and saves the PDF right then) — so a 100s cap
                # that expires during the bonus interim probing can be salvaged
                # instead of discarding an annual that was already found.
                _sink: dict = {}

                def _jp_scrape_both():
                    from prototypes import ir_resolve_proto as _R
                    from prototypes import ir_fetch_proto as _F
                    _cached_url = _ir_url_cache_get(_jp_ticker, _jp_name,
                                                    "Japan")
                    if _cached_url:
                        print(f"[{job_id}] jp: IR URL from cache "
                              f"{_cached_url}", flush=True)
                        _res = {"chosen_url": _cached_url,
                                "confidence": "CACHED"}
                    else:
                        _res = _R.resolve(_jp_name or "", _jp_ticker or "", "",
                                          "Japan")
                    _url = _res.get("chosen_url")
                    if not _url:
                        raise RuntimeError("IR-scraper: could not resolve an IR site")
                    # Runs on cache hits too: an OPTIONS run may have cached
                    # the Japanese root; the probe is 1-3 cheap GETs and
                    # self-skips when the URL is already an /english|/en path.
                    if _bs_mode:
                        # JP sites conventionally host the English IR library
                        # under /english/ (or /eng/, /en/) — the resolver often
                        # returns the JAPANESE root, whose report pages (株主
                        # 通信 / 有報) never link the English tanshin, so the
                        # results leg sees zero candidates (MUFG live run
                        # 2026-07-16: mufg.jp root → no results docs → 20-F
                        # fallback). It can also return a DEEP English subpage
                        # (Fast Retailing 2026-07-17: cached /eng/ir/stockinfo/
                        # description.html — the crawl never reached the IR
                        # library with the tanshin). Probe the conventional
                        # English IR entries and crawl there when one exists;
                        # fall back to the resolved URL. Only skip probing when
                        # the URL already IS an English root / IR top.
                        import requests as _rq
                        from urllib.parse import urlparse as _up
                        _pr = _up(_url)
                        _path = (_pr.path or "").rstrip("/")
                        _at_entry = re.fullmatch(
                            r"/(?:english|eng|en)(?:/ir)?", _path) is not None
                        if _pr.netloc and not _at_entry:
                            for _cand in (
                                    f"{_pr.scheme}://{_pr.netloc}/english/ir/",
                                    f"{_pr.scheme}://{_pr.netloc}/eng/ir/",
                                    f"{_pr.scheme}://{_pr.netloc}/en/ir/",
                                    f"{_pr.scheme}://{_pr.netloc}/english/",
                                    f"{_pr.scheme}://{_pr.netloc}/eng/"):
                                try:
                                    _pv = _rq.get(
                                        _cand, timeout=8, allow_redirects=True,
                                        headers={"User-Agent": "Mozilla/5.0"})
                                    if _pv.status_code == 200:
                                        print(f"[{job_id}] jp bs: English IR "
                                              f"entry {_cand}", flush=True)
                                        _url = _cand
                                        break
                                except Exception:
                                    continue
                    _sink["ir_url"] = _url
                    _out = _F.fetch_reports(
                        _url, allow_fc=True,
                        annual_path=str(_annual_pdf), interim_path=str(_interim_pdf),
                        name=_jp_name or "", early_sink=_sink,
                        purpose=("balance_sheet" if _bs_mode else "options"),
                        results_path=(str(_results_pdf) if _bs_mode else None))
                    _out["ir_url"] = _url
                    _out["resolver_confidence"] = _res.get("confidence")
                    # Remember the entry URL only when the crawl actually
                    # yielded a report — a dud URL must never be cached.
                    if any(_out.get(k) for k in ("results", "annual", "interim")):
                        _ir_url_cache_put(_jp_ticker, _jp_name, "Japan", _url)
                    return _out

                _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                _r = None
                try:
                    _scr = _ex.submit(_jp_scrape_both)
                    _r = _scr.result(timeout=EU_IR_SCRAPER_TIMEOUT_SEC)
                except concurrent.futures.TimeoutError:
                    # Scraper over the cap — salvage the early-saved results
                    # doc or annual if the crawl had already secured one
                    # (Kawasaki: the 73pp annual passed, then 3 interim probes
                    # blew the budget). No interim fallback in that case.
                    if _sink.get("annual") or _sink.get("results"):
                        _r = {"annual": _sink.get("annual"), "interim": None,
                              "results": _sink.get("results"),
                              "ir_url": _sink.get("ir_url")}
                except Exception:
                    pass  # scraper failed; fall back to EDINET
                finally:
                    # Do NOT wait — a timed-out scraper keeps running in its own
                    # thread but only ever writes its _annual/_interim PDFs (the
                    # early-saved annual is never rewritten), so it can't race
                    # the EDINET/pipeline PDF.
                    _ex.shutdown(wait=False)

                if _r:
                    _ann = (_r or {}).get("annual")
                    _intm = (_r or {}).get("interim")
                    _resd = (_r or {}).get("results")

                    # Pick the primary doc: quarterly-cadence results doc first
                    # (only ever set in _bs_mode); else the annual; else the
                    # interim. Balance-sheet jobs (user rule 2026-07-17:
                    # always the most recent quarterly/interim) rank the
                    # interim ABOVE the annual unless the annual is strictly
                    # newer; options jobs keep annual-first (interim reports
                    # often carry no SBC note).
                    _primary = None        # (path, kind, fiscal_year)
                    _alt = None            # runner-up kept as the opt-in fallback
                    _ann_ok = bool(_ann and _annual_pdf.exists()
                                   and _annual_pdf.stat().st_size > 0)
                    _intm_ok = bool(_intm and _interim_pdf.exists()
                                    and _interim_pdf.stat().st_size > 0)
                    if _resd and _results_pdf.exists() and _results_pdf.stat().st_size > 0:
                        _primary = (_results_pdf, "results", _resd.get("fiscal_year"))
                    elif (_bs_mode and _ann_ok and _intm_ok
                          and (_intm.get("fiscal_year") or 0)
                          >= (_ann.get("fiscal_year") or 0)):
                        _primary = (_interim_pdf, "interim", _intm.get("fiscal_year"))
                        _alt = (_annual_pdf, "annual", _ann.get("fiscal_year"))
                    elif _ann_ok:
                        _primary = (_annual_pdf, "annual", _ann.get("fiscal_year"))
                        if _intm_ok:
                            _alt = (_interim_pdf, "interim", _intm.get("fiscal_year"))
                    elif _intm_ok:
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
                            "company": _jp_name or meta.get("company_name"),
                            "jp_path": "ir_scraper",
                            "ir_url": (_r or {}).get("ir_url"),
                            "form": ("Annual Report" if _pkind == "annual"
                                     else "Financial Results"
                                     if _pkind == "results"
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
                            job_id, "jp_fetch",
                            duration=time.time() - stage_start, cost=0,
                            details=(f"{meta.get('company', '?')} · IR scraper · "
                                     f"{meta['form']}"
                                     + (f" (+{_alt[1]} available)" if _alt else "")))
                        jp_ok = True

            # Step B: SEC EDGAR fallback — Japanese ADR filers (MUFG, Toyota,
            # Sony, ...) file an English 20-F with the full consolidated
            # balance sheet, while the IR site often surfaces only the
            # integrated report (summary tables, no statements — MUFG's
            # ir2025_all_en.pdf). Name-verified inside _attempt_edgar, so a
            # 4-digit TSE code can never match the wrong EDGAR entity.
            if not jp_ok:
                try:
                    info = diamond_route._attempt_edgar(
                        _jp_name, _jp_ticker, pdf_path,
                        meta.get("category", "annual"), None)
                    if info and pdf_path.exists() and pdf_path.stat().st_size > 0:
                        try:
                            JOBS[job_id]["file_size"] = pdf_path.stat().st_size
                        except Exception:
                            pass
                        meta = {**meta, **info, "jp_path": "edgar"}
                        JOBS[job_id]["source_meta"] = meta
                        _mark_stage(job_id, "jp_fetch",
                                    duration=time.time() - stage_start, cost=0,
                                    details=(f"{info.get('company','?')} · "
                                             f"SEC EDGAR · {info.get('form','?')}"))
                        jp_ok = True
                except Exception:
                    pass  # not an ADR filer / no match; fall through to EDINET

            # Step C: EDINET fallback — only when an EDINET_API_KEY is configured
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
                            # Balance-sheet jobs (user rule 2026-07-17) take
                            # the freshest statement filing — annual (120) OR
                            # semi-annual (160); EDINET has no quarterly
                            # reports since April 2024.
                            category=("latest" if job.get("fetch_only")
                                      else meta.get("category", "annual")),
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
                        "Could not find an annual (or quarterly results) report "
                        "for this Japanese company on its investor-relations "
                        "site, SEC EDGAR (ADR 20-F), or EDINET. Please use the "
                        "Upload tab to submit the PDF directly.")

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
                _results_pdf = job_dir / f"{pdf_stem}_results.pdf"
                # QUARTERLY-FIRST (balance-sheet / fetch-only jobs, user rule
                # 2026-07-16, same as the Japan branch): prefer the freshest
                # quarterly-cadence results doc that carries a real balance
                # sheet; annual stays the fallback. Options runs (not
                # fetch_only) keep annual-first.
                _bs_mode = bool(job.get("fetch_only"))

                def _eu_scrape_both():
                    from prototypes import ir_resolve_proto as _R
                    from prototypes import ir_fetch_proto as _F
                    _cached_url = _ir_url_cache_get(_ir_ticker, _ir_name,
                                                    _ir_country)
                    if _cached_url:
                        print(f"[{job_id}] eu: IR URL from cache "
                              f"{_cached_url}", flush=True)
                        _res = {"chosen_url": _cached_url,
                                "confidence": "CACHED"}
                    else:
                        _res = _R.resolve(_ir_name or "", _ir_ticker or "", "",
                                          _ir_country or "")
                    _url = _res.get("chosen_url")
                    if not _url:
                        raise RuntimeError("IR-scraper: could not resolve an IR site")
                    _out = _F.fetch_reports(
                        _url, allow_fc=True,
                        annual_path=str(_annual_pdf), interim_path=str(_interim_pdf),
                        name=_ir_name or "",
                        purpose=("balance_sheet" if _bs_mode else "options"),
                        results_path=(str(_results_pdf) if _bs_mode else None))
                    _out["ir_url"] = _url
                    _out["resolver_confidence"] = _res.get("confidence")
                    # Remember the entry URL only when the crawl actually
                    # yielded a report — a dud URL must never be cached.
                    if any(_out.get(k) for k in ("results", "annual", "interim")):
                        _ir_url_cache_put(_ir_ticker, _ir_name, _ir_country,
                                          _url)
                    return _out

                _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    _scr = _ex.submit(_eu_scrape_both)
                    _r = _scr.result(timeout=EU_IR_SCRAPER_TIMEOUT_SEC)
                    _ann = (_r or {}).get("annual")
                    _intm = (_r or {}).get("interim")
                    _resd = (_r or {}).get("results")

                    # Pick the primary doc: quarterly-cadence results doc first
                    # (only ever set in _bs_mode); else the annual; else the
                    # interim. Balance-sheet jobs (user rule 2026-07-17:
                    # always the most recent quarterly/interim) rank the
                    # interim ABOVE the annual unless the annual is strictly
                    # newer; options jobs keep annual-first (interim reports
                    # often carry no SBC note).
                    _primary = None        # (path, kind, fiscal_year)
                    _alt = None            # runner-up kept as the opt-in fallback
                    _ann_ok = bool(_ann and _annual_pdf.exists()
                                   and _annual_pdf.stat().st_size > 0)
                    _intm_ok = bool(_intm and _interim_pdf.exists()
                                    and _interim_pdf.stat().st_size > 0)
                    if _resd and _results_pdf.exists() and _results_pdf.stat().st_size > 0:
                        _primary = (_results_pdf, "results", _resd.get("fiscal_year"))
                    elif (_bs_mode and _ann_ok and _intm_ok
                          and (_intm.get("fiscal_year") or 0)
                          >= (_ann.get("fiscal_year") or 0)):
                        _primary = (_interim_pdf, "interim", _intm.get("fiscal_year"))
                        _alt = (_annual_pdf, "annual", _ann.get("fiscal_year"))
                    elif _ann_ok:
                        _primary = (_annual_pdf, "annual", _ann.get("fiscal_year"))
                        if _intm_ok:
                            _alt = (_interim_pdf, "interim", _intm.get("fiscal_year"))
                    elif _intm_ok:
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
                                     else "Financial Results"
                                     if _pkind == "results"
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
                                     + (f" (+{_alt[1]} available)" if _alt else "")))
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
                    from markets.eu_fetch import _latest_filing as _eu_latest_filing
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
                    # Balance-sheet / fetch-only jobs: quarterly-first — prefer the
                    # most recent quarterly/interim report over the annual (user rule).
                    bs_mode=bool(job.get("fetch_only")),
                    # Direct-URL override: when the caller supplied an exact report
                    # PDF link, fetch that instead of resolving/crawling the IR site.
                    report_url=(meta.get("report_url") or "").strip() or None,
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

        # ── Fetch-only jobs stop here: the most-recent filing PDF is on disk
        # (download via /api/download/{job_id}/pdf). No Stage 1/2/3, no LLM.
        if job.get("fetch_only"):
            update_job(job_id, status="completed", progress=100,
                       current_stage=None, result_available=False)
            return

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

        # ── Company Comment (analyst note) ─────────────────────────
        # Firecrawl search for the results press release + one Claude call,
        # composed in the fixed 5-step format. Best-effort — never fails the
        # job (no FIRECRAWL_API_KEY / no sources → note skipped or degraded).
        if final.get("company_name"):
            try:
                from core.comment import generate_comment
                _src_meta = job.get("source_meta") or {}
                _note = generate_comment(
                    client,
                    final["company_name"],
                    quarter_label=final.get("report_period"),
                    country=(_src_meta.get("country") or "").strip() or None,
                    cost_tracker=cost_tracker,
                )
                if _note:
                    final["company_comment"] = _note["comment"]
                    final["company_comment_sources"] = _note["sources"]
                    print(f"[{job_id}] company comment generated", flush=True)
            except Exception as _cm_exc:
                print(f"[{job_id}] company comment skipped: {_cm_exc}",
                      file=sys.stderr)

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

        # ── Evidence PDF (user request 2026-07-09): save the exact pages the
        # extraction referred to, trimmed + highlighted, to HareKrishna/
        # <TICKER>_<FORM>.pdf. Best-effort — never fails the pipeline.
        try:
            from core.evidence import save_evidence_pdf
            _ev = save_evidence_pdf(str(pdf_path), target_pages,
                                    JOBS.get(job_id, {}), final=final)
            if _ev is not None:
                print(f"[{job_id}] evidence PDF: {_ev}", flush=True)
        except Exception as _ev_exc:
            print(f"[{job_id}] evidence PDF skipped: {_ev_exc}",
                  file=sys.stderr)

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
        # EU / Japan tabs: if the annual report had no option data but the scraper also
        # saved a recent interim/quarterly, advertise it so the frontend can offer a
        # retry. Only the IR-scraper paths (EU, Japan) ever set `alt_report_path`.
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

# Section order + description for the /docs (Swagger) page. Every endpoint
# carries one of these tags; routers mounted at the bottom of this file
# (XBRL, Simply Wall St, Industry, Credit Rating) tag themselves.
_OPENAPI_TAGS = [
    {"name": "Extraction — Upload & Unified",
     "description": "Upload a PDF or run the unified ticker → extraction "
                    "pipeline; Excel-ready options output."},
    {"name": "Markets — US (EDGAR)",
     "description": "US-listed companies via SEC EDGAR filings."},
    {"name": "Markets — Europe",
     "description": "UK (Companies House), EU/EEA (ESEF), Germany, Denmark, "
                    "plus the EU search / GuruFocus resolve helpers."},
    {"name": "Markets — Asia",
     "description": "Japan, Korea, China, Hong Kong, Taiwan, India, Singapore, "
                    "Indonesia, Malaysia, Thailand, Israel."},
    {"name": "Markets — Americas",
     "description": "Canada (SEC MJDS), Brazil (CVM), Mexico."},
    {"name": "Diamond — Any Market",
     "description": "Flagship any-market route: dedicated country integration "
                    "→ EDGAR → universal IR scraper; plus the scraper-only "
                    "testing route."},
    {"name": "Filings & Reports",
     "description": "Fetch the latest filing PDF for any market, or archive "
                    "all recent IR reports for a company."},
    {"name": "Balance Sheet",
     "description": "Standardize a filing's balance sheet and download it as "
                    "Excel."},
    {"name": "Analyst Comment",
     "description": "Generate the analyst Comments note for a company's most "
                    "recent reported quarter."},
    {"name": "XBRL",
     "description": "US options data straight from SEC XBRL facts (no PDF, "
                    "no LLM) — comparison prototype."},
    {"name": "Simply Wall St",
     "description": "Simply Wall St forecast data (standalone feature)."},
    {"name": "Industry",
     "description": "Ticker → Damodaran industry via GuruFocus (standalone "
                    "feature)."},
    {"name": "Credit Rating",
     "description": "Company → mapped credit rating via Firecrawl (standalone "
                    "feature)."},
    {"name": "Jobs & Downloads",
     "description": "Poll job status, fetch results, download Excel/PDF, "
                    "cancel jobs."},
    {"name": "System",
     "description": "Health check."},
]

app = FastAPI(
    title="Pavaki Options Extractor API",
    description="Extract share-based compensation data from annual reports",
    version="1.0.0",
    openapi_tags=_OPENAPI_TAGS,
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


@app.get("/api/health", tags=["System"])
async def health():
    return {"status": "healthy", "active_jobs": len(JOBS)}


@app.post("/api/extract", tags=["Extraction — Upload & Unified"])
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


class CommentRequest(BaseModel):
    company: str
    country: Optional[str] = ""      # omit -> inferred from sources
    industry: Optional[str] = ""     # omit -> inferred from sources
    # Optional pre-computed stats used VERBATIM in the closing sentence, e.g.
    # {"net_debt_pct": "23%", "interest_coverage": "4.8 times",
    #  "fcff_positive_years": "9 out of 10 years", "dividend_yield": "6.4%",
    #  "methods_upside": "21 of 24 methods", "recommendation": "BUY"}.
    # Omit -> the note ends after the management comment (no recommendation).
    financials: Optional[Dict[str, Any]] = None


@app.post("/api/comment", tags=["Analyst Comment"])
def company_comment(payload: CommentRequest):
    """Generate the analyst Comments note for a company.

    Always covers the company's MOST RECENT reported quarter (found via the
    press-release search). Firecrawl fetches the results press release, then
    Claude composes the note in the fixed 5-step format: company -> country ->
    industry, results good/bad, sales/profits direction, verbatim CEO quote
    (or quarter summary if no quote found), and — only when `financials` are
    supplied — the balance-sheet close with recommendation. Synchronous: ~30-60 s.
    """
    company = (payload.company or "").strip()
    if not company:
        raise HTTPException(status_code=400, detail="company is required")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        raise HTTPException(status_code=500,
                            detail="ANTHROPIC_API_KEY not configured")

    from core.comment import generate_comment
    client = anthropic.Anthropic(api_key=anthropic_key)
    result = generate_comment(
        client, company,
        country=(payload.country or "").strip() or None,
        industry=(payload.industry or "").strip() or None,
        financials=payload.financials or None,
    )
    if not result:
        raise HTTPException(
            status_code=502,
            detail="Comment generation failed (search/LLM error — see server logs)")
    return {"company": company,
            "comment": result["comment"],
            "sources": result["sources"]}


class EdgarExtractRequest(BaseModel):
    ticker: str
    company_name: Optional[str] = None
    form: str = "10-K"


@app.post("/api/extract-from-edgar", tags=["Markets — US (EDGAR)"])
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


@app.post("/api/extract-from-diamond", tags=["Diamond — Any Market"])
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


@app.post("/api/extract-from-singapore", tags=["Markets — Asia"])
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


@app.post("/api/extract-from-mexico", tags=["Markets — Americas"])
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


class AustraliaExtractRequest(BaseModel):
    # Australia (ASX): company name + ticker. Country is forced to "Australia"
    # server-side; the ticker is auto-prefixed with "ASX:" for the scraper.
    company_name: Optional[str] = ""
    ticker: Optional[str] = ""
    category: Optional[str] = "annual"


@app.post("/api/extract-from-australia", tags=["Markets — Asia"])
async def extract_from_australia(
    payload: AustraliaExtractRequest,
    background_tasks: BackgroundTasks,
):
    """🇦🇺 Australia (ASX): company name + ticker. Uses the SAME scraper framework as
    Diamond (universal IR-scraper) but locked to Australia — country is fixed to
    "Australia" and the ticker is auto-prefixed with "ASX:" for scraping/resolution
    (e.g. the user enters BHP -> internally ASX:BHP). Mirrors the Singapore/Mexico
    tabs; the US-EDGAR fallback is disabled so a name match never returns an
    unrelated US filer."""
    company_name = (payload.company_name or "").strip()
    raw_ticker = (payload.ticker or "").strip()
    if not company_name and not raw_ticker:
        raise HTTPException(
            status_code=400, detail="company_name or ticker is required"
        )

    # Prefix the ASX exchange code for the scraper (BHP -> ASX:BHP), unless the
    # user already typed an ASX: prefix.
    if raw_ticker and not raw_ticker.upper().startswith("ASX:"):
        ticker = f"ASX:{raw_ticker}"
    else:
        ticker = raw_ticker

    label = _safe_filename_part(ticker or company_name)
    filename = f"{label}_AUSTRALIA.pdf"

    job_id = create_job(
        filename=filename,
        file_size=0,
        source="diamond",
        source_meta={
            "company_name": company_name,
            "ticker": ticker,
            "category": payload.category or "annual",
            "country": "Australia",
            # Mirror Singapore/Mexico: locked to ASX issuers — never fall back to
            # US SEC EDGAR (a name match there would fetch an unrelated US filer).
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


@app.post("/api/scrape-test", tags=["Diamond — Any Market"])
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


@app.post("/api/extract-from-uk", tags=["Markets — Europe"])
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


@app.post("/api/extract-from-denmark", tags=["Markets — Europe"])
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


@app.post("/api/extract-from-japan", tags=["Markets — Asia"])
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


@app.post("/api/extract-from-korea", tags=["Markets — Asia"])
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


@app.post("/api/extract-from-brazil", tags=["Markets — Americas"])
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


@app.post("/api/extract-from-taiwan", tags=["Markets — Asia"])
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


@app.post("/api/extract-from-china", tags=["Markets — Asia"])
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


@app.post("/api/extract-from-india", tags=["Markets — Asia"])
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


@app.post("/api/extract-from-hongkong", tags=["Markets — Asia"])
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


@app.post("/api/extract-from-indonesia", tags=["Markets — Asia"])
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


@app.post("/api/extract-from-israel", tags=["Markets — Asia"])
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


@app.post("/api/extract-from-malaysia", tags=["Markets — Asia"])
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


@app.post("/api/extract-from-thailand", tags=["Markets — Asia"])
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


@app.get("/api/eu-search", tags=["Markets — Europe"])
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


@app.get("/api/gurufocus-resolve", tags=["Markets — Europe"])
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


@app.post("/api/extract-from-eu", tags=["Markets — Europe"])
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


@app.post("/api/eu-try-alternate/{job_id}", tags=["Markets — Europe"])
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


@app.post("/api/extract-from-germany", tags=["Markets — Europe"])
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


@app.post("/api/extract-from-canada", tags=["Markets — Americas"])
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


class OptionsExtractRequest(BaseModel):
    # Unified entry point: company ticker + name + country. Routes to the SAME
    # per-country handler the dedicated tabs use (so each market's exact
    # resolve/fetch/fallback logic runs), then returns the final JSON in one call.
    ticker: Optional[str] = ""
    company_name: Optional[str] = ""
    country: Optional[str] = ""
    category: Optional[str] = None
    form: Optional[str] = None   # only used when the country routes to US EDGAR
    refresh: bool = False        # force a fresh run, bypassing the ticker cache


# Normalized country name -> the source whose dedicated handler we delegate to.
_OPTIONS_COUNTRY_TO_SOURCE = {
    "united states": "edgar", "united states of america": "edgar",
    "usa": "edgar", "us": "edgar", "u.s.": "edgar", "u.s.a.": "edgar", "america": "edgar",
    "canada": "canada",
    "united kingdom": "uk", "uk": "uk", "great britain": "uk",
    "england": "uk", "britain": "uk", "scotland": "uk", "wales": "uk",
    "denmark": "denmark",
    "japan": "japan",
    "south korea": "korea", "korea": "korea", "republic of korea": "korea",
    "china": "china", "people's republic of china": "china",
    "prc": "china", "mainland china": "china",
    "hong kong": "hongkong", "hongkong": "hongkong",
    "taiwan": "taiwan", "republic of china (taiwan)": "taiwan",
    "india": "india",
    "indonesia": "indonesia",
    "israel": "israel",
    "malaysia": "malaysia",
    "thailand": "thailand",
    "brazil": "brazil",
    "germany": "germany",
    "singapore": "singapore",
    "mexico": "mexico",
    "australia": "australia",
}

# Exchange prefixes seen in "EXCHANGE:TICKER" inputs (GuruFocus / Capital IQ /
# Excel workbook style, e.g. "LSE:GSK", "SGX:Z77") -> the country whose market
# handler serves that listing. Only unambiguous prefixes are listed — e.g.
# "TSE" (Toronto vs Tokyo) and "OSE" (Oslo vs Osaka) are deliberately omitted;
# an unknown prefix is stripped without changing the caller's country.
_TICKER_EXCHANGE_PREFIX_TO_COUNTRY = {
    # US
    "NYSE": "United States", "NASDAQ": "United States", "NAS": "United States",
    "AMEX": "United States", "ARCA": "United States", "BATS": "United States",
    # UK
    "LSE": "United Kingdom", "LON": "United Kingdom",
    # Canada
    "TSX": "Canada", "TSXV": "Canada", "CVE": "Canada",
    # Japan
    "TYO": "Japan", "JPX": "Japan",
    # South Korea
    "KRX": "South Korea", "KOSDAQ": "South Korea", "KOSE": "South Korea",
    # China
    "SHSE": "China", "SZSE": "China", "SHA": "China", "SHE": "China",
    # Hong Kong
    "HKSE": "Hong Kong", "HKG": "Hong Kong", "SEHK": "Hong Kong", "HKEX": "Hong Kong",
    # Taiwan
    "TPE": "Taiwan", "TWSE": "Taiwan", "ROCO": "Taiwan",
    # India
    "NSE": "India", "BSE": "India", "BOM": "India",
    # Indonesia
    "IDX": "Indonesia", "JKT": "Indonesia",
    # Israel
    "TASE": "Israel", "TLV": "Israel",
    # Malaysia
    "KLSE": "Malaysia", "MYX": "Malaysia",
    # Thailand
    "SET": "Thailand", "BKK": "Thailand",
    # Brazil
    "BOVESPA": "Brazil", "BVMF": "Brazil", "SAO": "Brazil",
    # Germany
    "XETRA": "Germany", "ETR": "Germany", "FRA": "Germany", "XTER": "Germany",
    # Singapore
    "SGX": "Singapore",
    # Mexico
    "BMV": "Mexico", "MEX": "Mexico",
    # Australia
    "ASX": "Australia",
    # Denmark
    "CPH": "Denmark", "OMXC": "Denmark",
    # EU/EEA (ESEF)
    "EPA": "France", "PAR": "France",
    "AMS": "Netherlands",
    "BIT": "Italy", "MIL": "Italy",
    "BME": "Spain", "MCE": "Spain", "MAD": "Spain",
    "STO": "Sweden",
    "HEL": "Finland",
    "OSL": "Norway",
    "EBR": "Belgium", "BRU": "Belgium",
    "VIE": "Austria", "WBAG": "Austria",
    "LIS": "Portugal", "ELI": "Portugal",
    "WSE": "Poland", "WAR": "Poland",
    "ATH": "Greece",
    "ISE": "Ireland",
}


# ── US cross-listing detection (SEC official ticker map) ────────────────
# EU/EEA exchanges cross-list US SEC filers — e.g. Borsa Italiana's Global
# Equity Market lists Republic Services as "1RSG" (the segment's convention
# is "1" + the US ticker). Those companies file 10-Ks with the SEC and never
# file ESEF annual reports, so routing them to the "eu" source can only fail
# with NO_REPORT. On an EU-routed request, check the ticker against SEC's
# official ticker map and, on a confident match, redirect to the US ticker +
# United States so the request flows through EDGAR like a native US one.

_SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_TICKER_MAP: Dict[str, Any] = {"map": {}, "ts": 0.0}
_SEC_TICKER_MAP_TTL = 24 * 3600      # refresh daily
_SEC_TICKER_MAP_RETRY = 15 * 60      # back off after a failed download


def _sec_ticker_map() -> Dict[str, str]:
    """{"RSG": "Republic Services, Inc.", ...} from SEC's official ticker
    map, cached in-process for 24h. Returns {} (retried after 15 min) when
    the download fails — callers treat that as "no match", so an SEC outage
    can never break EU routing."""
    now = time.time()
    if _SEC_TICKER_MAP["ts"] and now - _SEC_TICKER_MAP["ts"] < (
            _SEC_TICKER_MAP_TTL if _SEC_TICKER_MAP["map"]
            else _SEC_TICKER_MAP_RETRY):
        return _SEC_TICKER_MAP["map"]
    try:
        import urllib.request
        ua = (os.environ.get("EDGAR_IDENTITY")
              or os.environ.get("SEC_USER_AGENT")
              or "Pavaki Options Extractor contact@pavaki.local")
        req = urllib.request.Request(_SEC_TICKER_MAP_URL,
                                     headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        _SEC_TICKER_MAP["map"] = {
            (v.get("ticker") or "").upper(): (v.get("title") or "")
            for v in data.values() if v.get("ticker")
        }
    except Exception as exc:
        print(f"[route] SEC ticker map unavailable ({exc}) — US "
              f"cross-listing check skipped", flush=True)
    _SEC_TICKER_MAP["ts"] = now
    return _SEC_TICKER_MAP["map"]


# Corporate-suffix words ignored when comparing company names.
_COMPANY_NAME_NOISE = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "plc",
    "ltd", "limited", "holdings", "holding", "group", "the", "sa", "nv",
    "ag", "spa", "se", "new",
}


def _company_names_match(a: str, b: str) -> bool:
    """True when one name's meaningful tokens are contained in the other's
    ("Republic Services, Inc" vs SEC's "Republic Services, Inc.")."""
    ta = {t for t in re.findall(r"[a-z0-9]+", (a or "").lower())
          if t not in _COMPANY_NAME_NOISE}
    tb = {t for t in re.findall(r"[a-z0-9]+", (b or "").lower())
          if t not in _COMPANY_NAME_NOISE}
    return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)


def _us_sec_crosslisting(ticker, company_name=""):
    """(us_ticker, sec_title) when an EU-routed ticker is really a US SEC
    filer cross-listed on an EU exchange; None otherwise. Deliberately
    conservative — a bare ticker collision alone never redirects:
      * "1"+<US ticker> (the Borsa Italiana Global Equity Market convention)
        redirects when the stripped ticker exists in the SEC map, unless a
        supplied company name contradicts the SEC registrant name;
      * the raw ticker redirects only when a supplied company name CONFIRMS
        the SEC registrant name (e.g. "G" is both Assicurazioni Generali on
        BIT and Genpact on NYSE — the name breaks the tie)."""
    t = (ticker or "").strip().upper()
    if not t:
        return None
    smap = _sec_ticker_map()
    if not smap:
        return None
    if re.fullmatch(r"1[A-Z][A-Z0-9.\-]*", t):
        title = smap.get(t[1:])
        if title and (not company_name
                      or _company_names_match(company_name, title)):
            return t[1:], title
    title = smap.get(t)
    if title and company_name and _company_names_match(company_name, title):
        return t, title
    return None


def _normalize_ticker_and_country(ticker, country, company_name=""):
    """Tolerate exchange-prefixed tickers from Excel/GuruFocus-style clients
    (e.g. "LSE:GSK", "NYSE:UHS"): strip the "EXCHANGE:" prefix — EDGAR and the
    other market fetchers reject the colon — and when the prefix names a known
    exchange let it override `country`, since the listing venue is more specific
    than the caller's country field. Unknown prefixes are stripped without
    touching the country.

    US cross-listings on EU/EEA exchanges (e.g. "BIT:1RSG" = Republic
    Services) are redirected to the US ticker + United States — the ESEF
    source can never serve a US SEC filer, so the request routes to EDGAR
    like a native US one. `company_name`, when supplied, confirms the match.

    Returns (ticker, country), both stripped."""
    ticker = (ticker or "").strip()
    country = (country or "").strip()
    if ":" in ticker:
        prefix, _, rest = ticker.partition(":")
        mapped = _TICKER_EXCHANGE_PREFIX_TO_COUNTRY.get(prefix.strip().upper())
        if rest.strip():
            ticker = rest.strip()
        if mapped:
            country = mapped
    if country.lower() in _OPTIONS_EU_COUNTRIES:
        us = _us_sec_crosslisting(ticker, company_name)
        if us:
            us_ticker, title = us
            print(f"[route] {ticker!r} ({country}) is US SEC filer "
                  f"{title!r} — rerouting to EDGAR as {us_ticker!r}",
                  flush=True)
            ticker, country = us_ticker, "United States"
    return ticker, country


# EU/EEA member states served by the pan-EU ESEF source (extract_from_eu).
_OPTIONS_EU_COUNTRIES = {
    "france", "netherlands", "the netherlands", "spain", "italy", "sweden", "finland",
    "belgium", "austria", "portugal", "poland", "greece", "luxembourg", "norway",
    "iceland", "croatia", "hungary", "romania", "slovakia", "slovenia", "estonia",
    "latvia", "lithuania", "cyprus", "malta", "ireland", "bulgaria",
    "czechia", "czech republic", "liechtenstein",
}

# Countries whose filings follow the IFRS "statement of financial position"
# presentation — the balance-sheet standardizer uses the European prompt
# (Balance_sheet/prompts/prompt_eu.py) for these; every other country (incl.
# the US) keeps the existing prompt.
_BS_EU_PROMPT_COUNTRIES = _OPTIONS_EU_COUNTRIES | {
    "germany", "united kingdom", "uk", "great britain", "england",
    "denmark", "switzerland", "europe", "eu",
}

# Countries that file under AASB (IFRS) but get their OWN Stage-3 prompt
# (Balance_sheet/prompts/prompt_au.py) so Australia-specific tuning never affects the
# European/IFRS prompt. The AU prompt started as a byte-identical copy of the
# EU one, so today's output is unchanged. Checked BEFORE the EU set below.
_BS_AU_PROMPT_COUNTRIES = {
    "australia",
}

# France gets its OWN Stage-3 prompt + code guard (Balance_sheet/France/):
# French filers print the "Total passif" grand total labeled just "Total
# liabilities" (= total assets, e.g. Bolloré), which the EU prompt copies
# verbatim into filing_totals.total_liabilities and the tally then fails by
# exactly the equity total. Checked BEFORE the EU set below ("france" is in
# the ESEF set too).
_BS_FR_PROMPT_COUNTRIES = {
    "france",
}


def _route_options_request(payload, edgar_default_form: str = "10-K"):
    """Shared routing for the unified endpoints: validate {ticker, company_name,
    country}, pick the market source, and build the dedicated handler's request
    kwargs. Returns (source, handler, Model, kwargs, ticker, company_name,
    country). Raises HTTPException on missing/unsupported input. Pure — no job
    is created and nothing is fetched here.

    `edgar_default_form` is the US form used when the payload gives none:
    "10-K" for the extraction endpoints; the fetch-filing endpoint passes
    "LATEST" (newest of 10-K/10-Q, resolved in edgar_fetch)."""
    ticker, country = _normalize_ticker_and_country(
        payload.ticker, payload.country, payload.company_name
    )
    company_name = (payload.company_name or "").strip()
    if not country:
        raise HTTPException(status_code=400, detail="country is required")
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )

    key = country.lower()
    source = _OPTIONS_COUNTRY_TO_SOURCE.get(key)
    if source is None and key in _OPTIONS_EU_COUNTRIES:
        source = "eu"
    if source is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported country {country!r}. Supported: United States, Canada, "
                "United Kingdom, Denmark, Japan, South Korea, China, Hong Kong, "
                "Taiwan, India, Indonesia, Israel, Malaysia, Thailand, Brazil, "
                "Germany, Singapore, Mexico, Australia, and EU/EEA member states."
            ),
        )

    # The dedicated handlers (defined above) — the SAME logic the dedicated tabs use.
    dispatch = {
        "edgar": (extract_from_edgar, EdgarExtractRequest),
        "canada": (extract_from_canada, CaExtractRequest),
        "uk": (extract_from_uk, UkExtractRequest),
        "denmark": (extract_from_denmark, DkExtractRequest),
        "japan": (extract_from_japan, JpExtractRequest),
        "korea": (extract_from_korea, KrExtractRequest),
        "china": (extract_from_china, CnExtractRequest),
        "hongkong": (extract_from_hongkong, HkExtractRequest),
        "taiwan": (extract_from_taiwan, TwExtractRequest),
        "india": (extract_from_india, InExtractRequest),
        "indonesia": (extract_from_indonesia, IdExtractRequest),
        "israel": (extract_from_israel, IlExtractRequest),
        "malaysia": (extract_from_malaysia, MyExtractRequest),
        "thailand": (extract_from_thailand, ThExtractRequest),
        "brazil": (extract_from_brazil, BrExtractRequest),
        "germany": (extract_from_germany, GermanyExtractRequest),
        "singapore": (extract_from_singapore, SingaporeExtractRequest),
        "mexico": (extract_from_mexico, MexicoExtractRequest),
        "australia": (extract_from_australia, AustraliaExtractRequest),
        "eu": (extract_from_eu, EuExtractRequest),
    }
    handler, Model = dispatch[source]

    # Build the chosen handler's request payload from the common fields. Only the
    # fields each model actually supports are set; missing ones use that model's
    # own defaults (e.g. UK -> "accounts", others -> "annual").
    kwargs = {"ticker": ticker, "company_name": company_name or None}
    # Ignore the Swagger/OpenAPI placeholder value "string" that clients often
    # leave in optional fields — treat it as unset so real defaults apply.
    category = (payload.category or "").strip()
    if category.lower() == "string":
        category = ""
    category = category or None
    form = (payload.form or "").strip()
    if form.lower() == "string":
        form = ""
    if Model is EdgarExtractRequest:
        kwargs["form"] = (form or edgar_default_form)
    else:
        if category:
            kwargs["category"] = category
    if Model is EuExtractRequest:
        kwargs["country"] = country

    return source, handler, Model, kwargs, ticker, company_name, country


@app.post("/api/extract-from-options", tags=["Extraction — Upload & Unified"])
async def extract_from_options(payload: OptionsExtractRequest):
    """Single unified endpoint: take {ticker, company_name, country} and route to
    the EXACT existing per-country handler (UK -> Companies House + IR fallback,
    US -> EDGAR, EU members -> ESEF, etc.). No new fetch/routing logic — it
    delegates to the dedicated handler, then runs the standard pipeline to
    completion and returns the final extraction JSON in this one response.

    Result cache: a successful response is cached (durable NeonDB, disk fallback)
    keyed by TICKER only for EXCEL_CACHE_TTL_SECONDS (default 7 days). A repeat
    request for the same ticker returns instantly — NO fetch, NO Stage 1/2/3, NO
    LLM (so it uses almost no memory). Failures are never cached. Pass
    {"refresh": true} to force a fresh run and overwrite the cached entry."""
    return await _extract_from_options_core(payload, use_cache=True)


async def _extract_from_options_core(
    payload: OptionsExtractRequest, use_cache: bool = True
):
    """Core of the options endpoint. `use_cache` controls the ticker result cache;
    internal callers (e.g. the excel endpoint's dual-form 10-Q/10-K runs) pass
    use_cache=False so the ticker-only cache never returns the wrong form."""
    from fastapi.concurrency import run_in_threadpool

    source, handler, Model, kwargs, ticker, company_name, country = (
        _route_options_request(payload)
    )

    # ── Ticker-keyed result cache (durable NeonDB, on-disk fallback) ──
    # A repeat request for the SAME ticker returns the stored full response,
    # skipping fetch + Stage 1/2/3 + LLM entirely (so it uses almost no memory).
    # Internal callers (the excel endpoint's dual-form runs) pass use_cache=False
    # to bypass, since this key ignores form and would otherwise cross the
    # 10-Q/10-K results. Only successful results are cached (below); failures
    # never are. ?refresh forces a fresh run.
    from core import excel_cache
    _cache_ttl = int(os.environ.get("EXCEL_CACHE_TTL_SECONDS", 7 * 24 * 3600))
    _cache_key = ("options-full-v1", ticker.upper())
    if use_cache and ticker and not payload.refresh:
        _hit = excel_cache.get(_cache_key, _cache_ttl)
        if _hit is not None:
            return _hit

    # Delegate to the dedicated handler: it runs that market's real validation +
    # resolution + job creation. We hand it a throwaway BackgroundTasks so it
    # registers the pipeline task without firing it, then we run the pipeline
    # synchronously below so this single call can return the final JSON.
    try:
        delegated = await handler(Model(**kwargs), BackgroundTasks())
    except HTTPException:
        raise
    job_id = delegated["job_id"]

    # Run the standard extraction pipeline to completion (blocking; off the event
    # loop). The per-source Stage 0 fetch + Stages 1/2/3 all run here.
    await run_in_threadpool(run_extraction_pipeline, job_id)

    job = JOBS.get(job_id, {})
    if job.get("status") == "failed":
        raise HTTPException(
            status_code=502,
            detail={
                "job_id": job_id,
                "source": source,
                "error": job.get("error"),
                "error_code": job.get("error_code"),
                "error_context": job.get("error_context"),
            },
        )

    json_path = get_job_dir(job_id) / "extraction.json"
    if not json_path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Extraction produced no result (job {job_id})",
        )
    with open(json_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    out = {
        "job_id": job_id,
        "status": "completed",
        "source": source,
        "country": country,
        "ticker": ticker,
        "company_name": company_name,
        "extraction_id": job.get("extraction_id"),
        "result": result,
    }
    # Cache only successful results, keyed by ticker (never cache failures — the
    # failure paths above raise HTTPException before reaching here).
    if use_cache and ticker:
        try:
            excel_cache.set(_cache_key, out)
        except Exception:
            pass
    return out


class FetchFilingRequest(BaseModel):
    # Fetch-only entry point: {ticker, company_name, country} -> most recent
    # filing PDF via the same per-country routing as /api/extract-from-options,
    # but stops after the fetch (no Stage 1/2/3, no LLM).
    ticker: Optional[str] = ""
    company_name: Optional[str] = ""
    country: Optional[str] = ""
    category: Optional[str] = None
    form: Optional[str] = None   # only used when the country routes to US EDGAR
    # Balance-sheet standardize only: force a fresh run, bypassing the 24h
    # ticker+company_name result cache (the fresh result overwrites the entry).
    refresh: Optional[bool] = False
    # Direct-URL override: exact report PDF URL. When set, the fetch skips the
    # IR-site resolver + crawl (and the dedicated-source / EDGAR fallbacks) and
    # downloads THIS document — for reports the crawl can't reach because they
    # live on a different domain than the company's resolved IR site.
    report_url: Optional[str] = None


_ARCHIVE_FY_RE = re.compile(r"^FY(\d{4})_(annual|interim)_", re.I)


def _archived_report_path(ticker: str, company_name: str):
    """Newest usable report saved by POST /api/reports/collect for this
    company (reports/<ticker>/FY<year>_<annual|interim>_*.pdf). Only files at
    or above the IR fetcher's freshness floor qualify (same no-stale-data
    rule as the live crawl); the highest fiscal year wins, annual over
    interim within a year. Returns (path, fiscal_year, kind) or None."""
    folder = BASE_DIR / "reports" / _safe_filename_part(ticker or company_name)
    if not folder.is_dir():
        return None
    try:
        from prototypes.ir_fetch_proto import MIN_FISCAL_YEAR as _floor
    except Exception:
        _floor = datetime.utcnow().year - 1
    best = None
    for p in folder.glob("*.pdf"):
        m = _ARCHIVE_FY_RE.match(p.name)
        if not m:
            continue
        fy, kind = int(m.group(1)), m.group(2).lower()
        if fy < _floor:
            continue
        key = (fy, 1 if kind == "annual" else 0)
        if best is None or key > best[0]:
            best = (key, p, fy, kind)
    if best is None:
        return None
    return best[1], best[2], best[3]


async def _fetch_filing_pdf(payload: FetchFilingRequest):
    """Shared fetch-only flow behind /api/fetch-filing and
    /api/balance-sheet/standardize: route to the per-market handler, create a
    fetch_only job, run the fetch synchronously (no Stage 1/2/3, no LLM).
    Returns (job_id, pdf_path, job); raises HTTPException on failure.

    ARCHIVE-FIRST (IR-crawl markets): for sources whose fetch depends on the
    non-deterministic IR-site crawl (germany, eu), a report previously saved
    by POST /api/reports/collect under reports/<ticker>/ is reused instead of
    re-crawling — instant and deterministic. Falls through to the normal
    fetch when the folder has nothing fresh enough."""
    from fastapi.concurrency import run_in_threadpool

    source, handler, Model, kwargs, ticker, company_name, country = (
        _route_options_request(payload, edgar_default_form="LATEST")
    )

    if source in ("germany", "eu"):
        archived = _archived_report_path(ticker, company_name)
        if archived is not None:
            arch_path, arch_fy, arch_kind = archived
            job_id = create_job(
                filename=arch_path.name,
                file_size=arch_path.stat().st_size,
                source="upload",
                source_meta={
                    "ticker": ticker,
                    "company_name": company_name,
                    "company": company_name or ticker,
                    "country": country,
                    "form": ("Annual Report" if arch_kind == "annual"
                             else "Interim/Quarterly Report"),
                    "report_year": arch_fy,
                    "report_period": arch_fy,
                    "fetch_path": "reports_archive",
                },
            )
            JOBS[job_id]["fetch_only"] = True
            for _stage in ("stage1_keywords", "stage2_classifier",
                           "stage3_extraction", "validation",
                           "excel_generation"):
                JOBS[job_id]["stages"].pop(_stage, None)
            pdf_path = get_job_dir(job_id) / JOBS[job_id]["filename"]
            shutil.copy2(arch_path, pdf_path)
            update_job(job_id, status="completed", progress=100)
            print(f"[{job_id}] archive-first: reusing {arch_path} "
                  f"(FY{arch_fy} {arch_kind})", flush=True)
            return job_id, pdf_path, JOBS[job_id]

    # Delegate to the dedicated handler with a throwaway BackgroundTasks so it
    # creates the job (running its real validation/resolution) without starting
    # the pipeline; we run the fetch synchronously below.
    delegated = await handler(Model(**kwargs), BackgroundTasks())
    job_id = delegated["job_id"]

    # Direct-URL override: carry the user-supplied report PDF link onto the job's
    # source_meta so run_extraction_pipeline hands it to fetch_for_diamond, which
    # downloads it directly instead of resolving/crawling the IR site.
    _rurl = (getattr(payload, "report_url", None) or "").strip()
    if _rurl:
        JOBS[job_id].setdefault("source_meta", {})["report_url"] = _rurl

    JOBS[job_id]["fetch_only"] = True
    # Drop the extraction stages from the job record — they will never run.
    for _stage in ("stage1_keywords", "stage2_classifier", "stage3_extraction",
                   "validation", "excel_generation"):
        JOBS[job_id]["stages"].pop(_stage, None)

    # Run the fetch to completion (off the event loop).
    await run_in_threadpool(run_extraction_pipeline, job_id)

    job = JOBS.get(job_id, {})
    if job.get("status") == "failed":
        raise HTTPException(
            status_code=502,
            detail={
                "job_id": job_id,
                "source": source,
                "error": job.get("error"),
                "error_code": job.get("error_code"),
                "error_context": job.get("error_context"),
            },
        )

    pdf_path = get_job_dir(job_id) / job.get("filename", "")
    if not pdf_path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Fetch produced no PDF (job {job_id})",
        )

    return job_id, pdf_path, job


@app.post("/api/fetch-filing", tags=["Filings & Reports"])
async def fetch_filing(payload: FetchFilingRequest):
    """Single endpoint returning the MOST RECENT filing PDF for a company —
    the PDF file itself, in this one response.

    Takes {ticker, company_name, country}, routes to the exact per-market
    resolve/fetch logic (Korea -> DART A001, China -> CNINFO annual report,
    etc.), runs ONLY the fetch (no Stage 1/2/3, no LLM cost), and streams back
    the fetched PDF. For the US with no explicit `form`, the truly latest
    filing wins — 10-Q or 10-K, whichever was filed most recently. Blocks
    until the fetch completes — slow markets (e.g. Korea's cold ~90s corp-list
    resolve) can exceed proxy timeouts like Cloudflare quick-tunnels' ~100s
    edge limit; call directly (not through the tunnel) for those."""
    job_id, pdf_path, job = await _fetch_filing_pdf(payload)

    # For US "LATEST" runs the fetch records which form actually won (10-K vs
    # 10-Q) — use it in the download name instead of the LATEST placeholder.
    download_name = pdf_path.name
    actual_form = ((job.get("source_meta") or {}).get("form") or "").strip()
    if "LATEST" in download_name and actual_form and actual_form != "LATEST":
        download_name = download_name.replace(
            "LATEST", actual_form.replace("/", "-")
        )

    return FileResponse(
        path=pdf_path,
        filename=download_name,
        media_type="application/pdf",
    )


class CollectReportsRequest(BaseModel):
    ticker: Optional[str] = ""
    company_name: Optional[str] = ""
    country: Optional[str] = ""
    # Archive freshness floor: keep reports with fiscal year >= this.
    # Default (None) = last 3 fiscal years (see fetch_all_reports).
    min_fiscal_year: Optional[int] = None


@app.post("/api/reports/collect", tags=["Filings & Reports"])
async def collect_reports(payload: CollectReportsRequest):
    """Crawl the company's investor-relations site ONCE and save ALL recent
    annual + quarterly reports into reports/<ticker>/ — one folder, one file
    per report (FY2025_annual_*.pdf, FY2026_interim_*.pdf, ...) — for later
    one-by-one use. Same resolve + crawl logic as the EU/Germany IR scraper
    (ir_resolve_proto + ir_fetch_proto), but instead of keeping only the best
    annual/interim pair, EVERY gate-passing report at or above
    min_fiscal_year is kept. Blocks until the crawl finishes (~1-3 min)."""
    from fastapi.concurrency import run_in_threadpool
    from prototypes import ir_fetch_proto as _F
    from prototypes import ir_resolve_proto as _R

    ticker, country = _normalize_ticker_and_country(
        payload.ticker, payload.country
    )
    company_name = (payload.company_name or "").strip()
    if not ticker and not company_name:
        raise HTTPException(
            status_code=400, detail="ticker or company_name is required"
        )

    def _run():
        res = _R.resolve(company_name or "", ticker or "", "", country or "")
        url = res.get("chosen_url")
        if not url:
            raise RuntimeError(
                "Could not resolve the company's investor-relations site "
                "from the ticker/name — try supplying company_name."
            )
        out_dir = BASE_DIR / "reports" / _safe_filename_part(
            ticker or company_name
        )
        out = _F.fetch_all_reports(
            str(url), str(out_dir), allow_fc=True,
            min_fy=payload.min_fiscal_year, name=company_name or ticker,
        )
        out["resolver_confidence"] = res.get("confidence")
        return out

    try:
        out = await run_in_threadpool(_run)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if not out["saved"]:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "No annual/quarterly report passed the content "
                         "gate on the resolved IR site.",
                "ir_url": out.get("ir_url"),
                "probed": out.get("probed"),
            },
        )
    return out


# ── Balance-sheet standardize: async job pattern ─────────────────────
# The fetch + LlamaParse + LLM standardize + tally run takes 30-90s — far too
# long to hold a client connection open (Excel freezes; proxies return
# 499/timeout). So POST returns a job_id in <1s and the work runs in the
# background; the client polls GET /api/balance-sheet/status until the job
# leaves "pending". In-process dict store — requires a SINGLE uvicorn worker
# (--workers 1) so every poll sees the same dict (see Balance_sheet/README.md).

BS_JOBS: dict[str, dict] = {}
BS_JOB_TTL_SECONDS = 30 * 60

# ── Balance-sheet result cache (user request 2026-07-15) ─────────────
# Same durable store as the options excel endpoint (core/excel_cache: NeonDB,
# disk fallback) — the "balance-sheet-v1" prefix keeps the keys separate inside
# the shared table. A repeat request with the SAME ticker within the TTL is
# served from cache: no fetch, no LlamaParse, no LLM. Only successful results
# are cached; pass {"refresh": true} to force a fresh run.
# ENABLED with a 24h default (user request 2026-07-18); override with the
# BS_CACHE_TTL_SECONDS env var (0 disables — the guard stays at the call sites:
# excel_cache.get treats ttl<=0 as "never expires", NOT as "disabled").
BS_CACHE_TTL_SECONDS = int(os.environ.get("BS_CACHE_TTL_SECONDS", 24 * 3600))


def _bs_cache_key(payload: "FetchFilingRequest"):
    """Cache key = TICKER only (same ticker within the TTL hits, regardless of
    company_name) — mirrors the options result cache. An "EXCHANGE:" prefix is
    stripped so ASX:CUV and CUV key identically. Falls back to company_name only
    when no ticker is supplied; returns None (caching skipped) when both blank."""
    raw = (payload.ticker or "").strip()
    if ":" in raw:                       # ASX:CUV -> CUV (venue prefix dropped)
        raw = raw.split(":", 1)[1]
    ticker = raw.strip().upper()
    if ticker:
        return ("balance-sheet-v1", ticker)
    name = " ".join((payload.company_name or "").strip().lower().split())
    if name:
        return ("balance-sheet-v1", "name:" + name)
    return None

# Hard references to in-flight tasks — asyncio only keeps weak refs, so an
# unreferenced task can be garbage-collected mid-run.
_BS_TASKS: set = set()


def _bs_cleanup_jobs():
    """Drop balance-sheet jobs older than the TTL so the dict never grows
    forever. Called on each POST (the only place the dict grows)."""
    cutoff = time.time() - BS_JOB_TTL_SECONDS
    for jid in [j for j, rec in BS_JOBS.items()
                if (rec.get("created") or 0) < cutoff]:
        BS_JOBS.pop(jid, None)


async def _bs_run_job(bs_job_id: str, payload: FetchFilingRequest):
    """Background worker: fetch the filing PDF then run the (unchanged)
    Balance_sheet standardization pipeline, recording the outcome in BS_JOBS.
    Never raises — every failure lands in the store as status=error."""
    from fastapi.concurrency import run_in_threadpool

    from Balance_sheet.pipeline import run_pipeline as run_balance_sheet

    try:
        job_id, pdf_path, job = await _fetch_filing_pdf(payload)

        # European companies get the IFRS prompt (Balance_sheet/prompts/prompt_eu.py);
        # US and every other market keep the existing prompt. Country comes
        # from the request (exchange-prefixed tickers override it, same rule
        # as routing).
        _, _bs_country = _normalize_ticker_and_country(
            payload.ticker, payload.country, payload.company_name
        )
        _bs_c = (_bs_country or "").strip().lower()
        region = ("fr" if _bs_c in _BS_FR_PROMPT_COUNTRIES
                  else "au" if _bs_c in _BS_AU_PROMPT_COUNTRIES
                  else "eu" if _bs_c in _BS_EU_PROMPT_COUNTRIES
                  else None)

        # The pipeline itself never crashes — failures come back as JSON with
        # "warnings" (and an "error" field), passed through inside the result.
        result = await run_in_threadpool(
            run_balance_sheet, str(pdf_path), region
        )

        # Filing-vintage warning: ESEF coverage on filings.xbrl.org is
        # partial, so the newest report a source offers can be years old
        # (e.g. Eiffage = FY2023). Flag it rather than fail — the numbers
        # themselves are untouched.
        _meta = (job or {}).get("source_meta") or {}
        _fy = re.search(r"(?:19|20)\d{2}",
                        str(_meta.get("report_year")
                            or _meta.get("report_period") or ""))
        if (_fy and isinstance(result, dict)
                and int(_fy.group(0)) < datetime.now().year - 1):
            result.setdefault("warnings", []).append(
                f"Filing vintage: FY{_fy.group(0)} — the newest report "
                f"available from this source for this company."
            )

        # Quarterly-first rule (user, 2026-07-17): the balance sheet must come
        # from the most recent quarterly/interim report; an annual filing is
        # only the last-resort fallback and is flagged, never silently used.
        _form = str(_meta.get("form") or "")
        if isinstance(result, dict) and re.search(
                r"annual|10-K|20-F|40-F|有価証券報告書", _form, re.IGNORECASE):
            result.setdefault("warnings", []).append(
                f"Report type: balance sheet taken from an ANNUAL filing "
                f"({_form}) — no more-recent quarterly/interim report was "
                f"found on the IR site or registry fallbacks; figures are "
                f"as of the fiscal year-end."
            )

        # ── Evidence PDF (user request 2026-07-09): save the located
        # balance-sheet pages, highlighted, to HareRam/. Best-effort — the
        # job result is never affected by an evidence failure.
        try:
            from core.evidence import save_bs_evidence_pdf
            _ev = await run_in_threadpool(
                save_bs_evidence_pdf, str(pdf_path),
                result.get("source_pages") or [], job, result)
            if _ev:
                print(f"[{bs_job_id}] BS evidence PDF: {_ev}", flush=True)
        except Exception as _ev_exc:
            print(f"[{bs_job_id}] BS evidence PDF skipped: {_ev_exc}",
                  flush=True)

        # ── Result cache write (only when BS_CACHE_TTL_SECONDS > 0):
        # successful results only (an "error" field means a stage failed —
        # always re-run those). excel_cache.set is best-effort, never raises.
        if BS_CACHE_TTL_SECONDS > 0 and not result.get("error"):
            _ckey = _bs_cache_key(payload)
            if _ckey:
                from core import excel_cache
                excel_cache.set(_ckey, result)
                print(f"[{bs_job_id}] BS result cached "
                      f"(key={_ckey[1]!r}, "
                      f"ttl={BS_CACHE_TTL_SECONDS}s)", flush=True)

        BS_JOBS[bs_job_id] = {"status": "done", "result": result,
                              "created": time.time()}
    except HTTPException as exc:
        # _fetch_filing_pdf raises 502 with {"error", "error_code", ...} when
        # the fetch fails (registry miss, no report, config, ...).
        detail = exc.detail
        if isinstance(detail, dict):
            error = str(detail.get("error") or detail)
            error_code = detail.get("error_code") or "NO_REPORT"
        else:
            error = str(detail)
            error_code = "NO_REPORT" if exc.status_code == 502 else "INTERNAL"
        BS_JOBS[bs_job_id] = {"status": "error", "error": error,
                              "error_code": error_code, "created": time.time()}
    except Exception as e:
        BS_JOBS[bs_job_id] = {"status": "error", "error": str(e),
                              "error_code": "INTERNAL", "created": time.time()}


@app.post("/api/balance-sheet/standardize", status_code=202, tags=["Balance Sheet"])
async def balance_sheet_standardize(payload: FetchFilingRequest):
    """Start a balance-sheet standardization job (same inputs + same
    per-market routing as /api/fetch-filing: {ticker, company_name, country,
    form?}) and return IMMEDIATELY with a job_id — the fetch + LlamaParse +
    LLM standardize + tally all run in the background (logic unchanged).
    Poll GET /api/balance-sheet/status?job_id=<id> for the result.

    Result cache: ENABLED with a 24h TTL by default (override with the
    BS_CACHE_TTL_SECONDS env var; 0 disables). A successful result is cached
    (NeonDB, disk fallback) keyed by TICKER only — a repeat request with the
    same ticker within the TTL returns a job that is ALREADY done, and the
    first status poll delivers the full result instantly with
    "served_from_cache": true (no fetch, no LlamaParse, no LLM). Failures are
    never cached. Pass {"refresh": true} to bypass and overwrite the entry."""
    # Validate the body up front (400 on missing/unsupported input) — pure
    # routing check, no job created, nothing fetched.
    _route_options_request(payload, edgar_default_form="LATEST")

    _bs_cleanup_jobs()

    bs_job_id = uuid.uuid4().hex

    # ── Result cache read (only when BS_CACHE_TTL_SECONDS > 0): on a hit
    # the job is born "done", so the client's normal poll loop gets the
    # result on its first call — no client change needed. refresh=true
    # skips the read (fresh run then overwrites the entry).
    _ckey = _bs_cache_key(payload) if BS_CACHE_TTL_SECONDS > 0 else None
    if _ckey and not payload.refresh:
        from core import excel_cache
        _hit = excel_cache.get(_ckey, BS_CACHE_TTL_SECONDS)
        if isinstance(_hit, dict):
            print(f"[{bs_job_id}] BS cache HIT "
                  f"(key={_ckey[1]!r})", flush=True)
            BS_JOBS[bs_job_id] = {
                "status": "done",
                "result": {**_hit, "served_from_cache": True},
                "created": time.time(),
            }
            return JSONResponse(status_code=202,
                                content={"job_id": bs_job_id,
                                         "status": "done"})
        print(f"[{bs_job_id}] BS cache MISS "
              f"(key={_ckey[1]!r}) — running full pipeline",
              flush=True)
    elif _ckey and payload.refresh:
        print(f"[{bs_job_id}] BS cache BYPASSED (refresh=true, "
              f"key={_ckey[1]!r}) — running full pipeline",
              flush=True)

    BS_JOBS[bs_job_id] = {"status": "pending", "created": time.time()}

    task = asyncio.create_task(_bs_run_job(bs_job_id, payload))
    _BS_TASKS.add(task)
    task.add_done_callback(_BS_TASKS.discard)

    return JSONResponse(status_code=202,
                        content={"job_id": bs_job_id, "status": "pending"})


@app.get("/api/balance-sheet/status", tags=["Balance Sheet"])
async def balance_sheet_status(job_id: str = ""):
    """Poll a balance-sheet standardization job. Pure dict lookup — never does
    any work. Returns pending / done (with the full standardized JSON merged
    in, same shape the old synchronous endpoint returned) / error."""
    rec = BS_JOBS.get(job_id)
    if rec is None:
        return JSONResponse(status_code=404,
                            content={"status": "error",
                                     "error": "unknown job_id"})
    if rec["status"] == "done":
        return JSONResponse(content={"job_id": job_id, "status": "done",
                                     **(rec.get("result") or {})})
    if rec["status"] == "error":
        return JSONResponse(content={"job_id": job_id, "status": "error",
                                     "error": rec.get("error"),
                                     "error_code": rec.get("error_code")})
    return {"job_id": job_id, "status": "pending"}


@app.post("/api/balance-sheet/excel", tags=["Balance Sheet"])
async def balance_sheet_excel(payload: FetchFilingRequest):
    """Same workflow as /api/balance-sheet/standardize (same inputs, same
    per-market fetch routing, same Stage 1 page location + Stage 2 LlamaParse)
    — but instead of standardizing, the balance sheet is written to an Excel
    workbook AS PRINTED: every line item and every value copied as-is, no
    summarizing, no schema mapping, no LLM call. Streams back the .xlsx.
    Blocks through fetch + parse — same proxy-timeout caveat as
    /api/fetch-filing for slow markets."""
    from fastapi.concurrency import run_in_threadpool

    from Balance_sheet.excel import run_raw_pipeline

    job_id, pdf_path, job = await _fetch_filing_pdf(payload)

    xlsx_path = get_job_dir(job_id) / f"{pdf_path.stem}_balance_sheet.xlsx"
    try:
        await run_in_threadpool(run_raw_pipeline, str(pdf_path), str(xlsx_path))
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"job_id": job_id, "error": str(exc)},
        )

    return FileResponse(
        path=xlsx_path,
        filename=xlsx_path.name,
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
    )


@app.get("/api/excel/options", tags=["Extraction — Upload & Unified"])
async def excel_options(
    ticker: str = "",
    country: str = "",
    company_name: str = "",
    category: Optional[str] = None,
    form: Optional[str] = None,
    refresh: bool = False,
):
    """Minimal, Excel-ready option-plan inputs for the Damodaran valuation
    workbook. Reuses the EXACT extraction pipeline behind
    POST /api/extract-from-options (same engine + same per-page caching), then
    reduces the full plans[] to the four fields the workbook needs:
    count_mn, strike, maturity_years, kind.

    US (SEC EDGAR) dual-form: when no explicit form is requested, the most
    recent 10-Q is tried FIRST and the 10-K is used only as a fallback (see the
    branch below). NON-US markets and explicit-form requests run once, unchanged.

    Result cache: a successful response for a given input (ticker+country+
    company_name+category+form) is cached for EXCEL_CACHE_TTL_SECONDS (default
    7 days). A cache hit returns instantly — NO fetch, NO Stage 1/2/3, NO LLM
    call. Error/credit failures are never cached. Pass ?refresh=true to force a
    fresh run and overwrite the cached entry.

    NEVER 500s: on ANY failure it returns option_plans: [] plus an "error"
    field describing what happened."""
    from core.excel_options import map_plans_to_excel

    # Same exchange-prefix normalization as the routing ("LSE:GSK" -> GSK, UK),
    # so the is_us dual-form check and the ticker cache key see the real values.
    ticker, country_norm = _normalize_ticker_and_country(ticker, country)
    currency = None
    try:
        if not ticker:
            return {"ticker": ticker, "currency": None, "option_plans": [],
                    "error": "ticker is required"}
        if not country_norm:
            return {"ticker": ticker, "currency": None, "option_plans": [],
                    "error": "country is required"}

        # ── Endpoint result cache (skips fetch + Stage 1/2/3 + LLM entirely) ──
        # Keyed by TICKER ONLY (per request): the same ticker returns the cached
        # result regardless of country / company_name / category / form. Success-
        # ful results (including a genuine "no options" empty) are cached for
        # EXCEL_CACHE_TTL_SECONDS (default 7 days); error/credit failures are
        # NEVER cached. A cache hit returns instantly with no LLM call.
        # ?refresh=true forces a fresh run. Backed by NeonDB/Postgres (durable
        # across Render restarts/deploys/idle spin-downs that wipe the local
        # disk) with an automatic on-disk fallback — see excel_cache.py.
        from core import excel_cache
        _ttl = int(os.environ.get("EXCEL_CACHE_TTL_SECONDS", 7 * 24 * 3600))
        _ckey = ("ticker-v1", ticker.upper())

        def _finalize(payload):
            # Cache only SUCCESSFUL results (no "error" key); never cache failures.
            if "error" not in payload:
                excel_cache.set(_ckey, payload)
            return payload

        def _cleanup_job(job_id):
            # The excel endpoint only needs the in-memory result (already read by
            # extract_from_options) + the cached payload — it never serves this
            # job's PDF/extraction.json/.xlsx. So delete the throwaway job folder
            # it created, to avoid filling the (ephemeral, free-tier) Render disk.
            # These are jobs the EXCEL endpoint spawned; the normal UI flow uses
            # its OWN job_ids and is unaffected.
            if not job_id:
                return
            try:
                d = get_job_dir(job_id)
                if d and d.exists():
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
            try:
                JOBS.pop(job_id, None)
            except Exception:
                pass

        if not refresh:
            hit = excel_cache.get(_ckey, _ttl)
            if hit is not None:
                return hit

        async def _run(form_value):
            """Run the existing options pipeline ONCE for a given SEC form (or
            None to use that market's default). Reuses extract_from_options
            verbatim — same resolve/fetch/routing + Stages 1/2/3 (with caching).
            Returns (result_dict_or_None, error_str_or_None); never raises.
            Deletes its throwaway job folder afterwards (success OR failure)."""
            job_id = None
            try:
                resp = await _extract_from_options_core(OptionsExtractRequest(
                    ticker=ticker,
                    company_name=company_name or "",
                    country=country_norm,
                    category=category,
                    form=form_value,
                ), use_cache=False)
                if isinstance(resp, dict):
                    job_id = resp.get("job_id")
                res = resp.get("result", {}) if isinstance(resp, dict) else {}
                if not isinstance(res, dict):
                    res = {}
                # Stage-3 failures (e.g. Anthropic credit/API errors) come back
                # as a result dict carrying an "error" key instead of raising —
                # surface it as a real error so the response isn't a silent [].
                if res.get("error"):
                    detail = str(res.get("details") or "")[:300]
                    return None, f"{res['error']}: {detail}".strip().rstrip(": ")
                return res, None
            except HTTPException as exc:
                d = exc.detail
                # A 502 from extract_from_options carries the failed job_id —
                # grab it so its (partial) folder gets cleaned up too.
                if isinstance(d, dict):
                    job_id = d.get("job_id") or job_id
                return None, (f"extraction failed ({exc.status_code}): "
                              f"{d if isinstance(d, str) else d}")
            finally:
                _cleanup_job(job_id)

        async def _run_ir_annual():
            """IR-website fallback (2026-07-09): fetch the company's ANNUAL
            REPORT from its own IR site (Diamond IR-scraper) and run the SAME
            full pipeline (Stage 0 fetch + Stages 1/2/3). country is left
            blank so the scraper is the primary route, and the last-resort
            EDGAR retry is disabled — the 10-Q/10-K were already tried.
            Returns (result_dict_or_None, error_str_or_None); never raises."""
            from fastapi.concurrency import run_in_threadpool as _rtp
            job_id = None
            try:
                name = (company_name or "").strip()
                if not name:
                    # IR resolution needs a real company name; EDGAR knows it.
                    try:
                        from markets.edgar_fetch import _ensure_identity
                        _ensure_identity()
                        from edgar import Company as _EdgarCompany
                        name = (getattr(_EdgarCompany(ticker), "name", "")
                                or "").strip()
                    except Exception:
                        name = ""
                if not name:
                    name = ticker
                job_id = create_job(
                    filename=f"{ticker}_IR_Annual.pdf",
                    file_size=0,
                    source="diamond",
                    source_meta={
                        "company_name": name,
                        "ticker": ticker,
                        "category": "annual",
                        "country": "",              # blank -> IR-scraper primary
                        "no_edgar_fallback": True,  # 10-Q/10-K already tried
                    },
                )
                await _rtp(run_extraction_pipeline, job_id)
                job = JOBS.get(job_id, {})
                if job.get("status") == "failed":
                    return None, f"IR fallback failed: {job.get('error')}"
                json_path = get_job_dir(job_id) / "extraction.json"
                if not json_path.exists():
                    return None, "IR fallback produced no extraction result"
                with open(json_path, "r", encoding="utf-8") as f:
                    res = json.load(f)
                if isinstance(res, dict) and res.get("error"):
                    detail = str(res.get("details") or "")[:300]
                    return None, f"{res['error']}: {detail}".strip().rstrip(":")
                return (res if isinstance(res, dict) else {}), None
            except Exception as exc:
                return None, f"IR fallback: {type(exc).__name__}: {exc}"
            finally:
                _cleanup_job(job_id)

        is_us = _OPTIONS_COUNTRY_TO_SOURCE.get(country_norm.lower()) == "edgar"
        explicit_form = bool((form or "").strip())

        # ── US dual-form: prefer the most recent 10-Q, fall back to the 10-K ──
        # Only when the caller did NOT pin a specific form. The 10-Q is tried
        # first; run_extraction_pipeline raises at Stage 1/2 page-detection —
        # BEFORE the Claude/Stage-3 LLM call — when a document has no relevant
        # pages, so a 10-Q with no options data costs zero LLM spend and we fall
        # straight through to the 10-K. Net effect: BOTH forms are page-checked,
        # but only the winning document is ever sent to the LLM. (The lone
        # exception — 10-Q pages that extract but map to no usable plans — is
        # itself "no 10-Q options data", so falling back to the 10-K is correct.)
        if is_us and not explicit_form:
            result_q, _err_q = await _run("10-Q")
            plans_q = map_plans_to_excel(result_q or {})
            if plans_q:
                return _finalize({"ticker": ticker,
                                  "currency": (result_q or {}).get("currency"),
                                  "option_plans": plans_q})
            # No usable 10-Q option data -> use the 10-K instead.
            result_k, err_k = await _run("10-K")
            plans_k = map_plans_to_excel(result_k or {})
            if plans_k:
                return _finalize({"ticker": ticker,
                                  "currency": (result_k or {}).get("currency"),
                                  "option_plans": plans_k})

            # ── IR-website fallback (2026-07-09): NEITHER the 10-Q NOR the
            # 10-K yielded any option plans -> fetch the annual report from
            # the company's own IR website and run the full pipeline on it.
            result_ir, err_ir = await _run_ir_annual()
            plans_ir = map_plans_to_excel(result_ir or {})
            out = {"ticker": ticker,
                   "currency": ((result_ir if plans_ir else result_k)
                                or {}).get("currency"),
                   "option_plans": plans_ir}
            if plans_ir:
                out["source"] = "ir_annual_report"
            else:
                errs = "; ".join(e for e in (err_k, err_ir) if e)
                if errs:
                    out["error"] = errs
            return _finalize(out)

        # ── Single run: non-US market, or an explicitly requested form ──
        result, err = await _run(form if explicit_form else None)
        if result is None:
            return _finalize({"ticker": ticker, "currency": None,
                              "option_plans": [], "error": err})
        return _finalize({"ticker": ticker,
                          "currency": result.get("currency"),
                          "option_plans": map_plans_to_excel(result)})

    except HTTPException as exc:
        # extract_from_options raises HTTPException on bad input / fetch /
        # extraction failure. Surface it as an error field, not a 500.
        detail = exc.detail
        msg = detail if isinstance(detail, str) else str(detail)
        return {"ticker": ticker, "currency": currency, "option_plans": [],
                "error": f"extraction failed ({exc.status_code}): {msg}"}
    except Exception as exc:  # absolute backstop — the no-failure mandate.
        return {"ticker": ticker, "currency": currency, "option_plans": [],
                "error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/job/{job_id}", tags=["Jobs & Downloads"])
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


@app.get("/api/result/{job_id}", tags=["Jobs & Downloads"])
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


@app.get("/api/download/{job_id}/excel", tags=["Jobs & Downloads"])
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


@app.get("/api/download/{job_id}/pdf", tags=["Jobs & Downloads"])
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


@app.delete("/api/job/{job_id}", tags=["Jobs & Downloads"])
async def cancel_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    JOBS[job_id]["status"] = "cancelled"
    job_dir = get_job_dir(job_id)
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    JOBS.pop(job_id, None)
    return {"status": "cancelled", "job_id": job_id}


@app.get("/api/jobs", tags=["Jobs & Downloads"])
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
from routes.simply_route import router as simply_router
app.include_router(simply_router)


# ─── USA XBRL comparison route (standalone prototype) ──────────────
# Extracts options data straight from SEC XBRL facts (no PDF/LLM) for
# side-by-side comparison with the PDF pipeline. Lives in the "USA xbrl"
# folder — the space in the name rules out a normal import, so the module
# is loaded from its file path.
import importlib.util as _xbrl_ilu

_XBRL_SERVICE = Path(__file__).parent / "USA xbrl" / "xbrl_service.py"
if _XBRL_SERVICE.is_file():
    _xbrl_spec = _xbrl_ilu.spec_from_file_location("usa_xbrl_service", str(_XBRL_SERVICE))
    _xbrl_mod = _xbrl_ilu.module_from_spec(_xbrl_spec)
    _xbrl_spec.loader.exec_module(_xbrl_mod)
    if getattr(_xbrl_mod, "router", None) is not None:
        app.include_router(_xbrl_mod.router)


# ─── Company industry route (standalone feature, same origin) ──────
# Adds GET /api/industry — ticker -> Damodaran industry via GuruFocus.
from Company_Industry.industry_route import router as industry_router
app.include_router(industry_router)


# ─── Credit rating route (standalone feature, same origin) ─────────
# Adds GET /api/credit-rating — company -> mapped credit rating via
# Firecrawl. The hyphen in the "Credit-Ratings" folder name rules out a
# normal import, so the module is loaded from its file path.
import importlib.util as _credit_ilu

_CREDIT_ROUTE = Path(__file__).parent / "Credit-Ratings" / "credit_route.py"
if _CREDIT_ROUTE.is_file():
    _credit_spec = _credit_ilu.spec_from_file_location("credit_route", str(_CREDIT_ROUTE))
    _credit_mod = _credit_ilu.module_from_spec(_credit_spec)
    _credit_spec.loader.exec_module(_credit_mod)
    if getattr(_credit_mod, "router", None) is not None:
        app.include_router(_credit_mod.router)


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

"""
OCR helper for image-only PDFs (Companies House filings)
========================================================

Large UK companies file their statutory accounts with Companies House as
**scanned / image-only PDFs** — `page.get_text()` returns nothing. The whole
detection pipeline in options.py is text-driven (keyword filter → LLM
classifier all read page text), so an image-only PDF would surface zero
candidate pages and fail.

This module rebuilds such a PDF into a **searchable PDF** with an embedded
OCR text layer (via Tesseract, exposed through PyMuPDF's `pdfocr_tobytes`).
Once a text layer exists, the existing pipeline runs completely unchanged.

OCR is parallelised across CPU cores with a process pool (Tesseract is the
bottleneck), preserving page order on reassembly.

Public API:
    has_text_layer(pdf_path) -> bool
    ensure_searchable_pdf(pdf_path, dpi=200, progress=None) -> dict
        OCRs in place (overwrites pdf_path) only if no text layer is present.
"""

from __future__ import annotations

import glob
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

import fitz  # PyMuPDF


# ── Locate Tesseract's tessdata so PyMuPDF's OCR can find the language data ──
def _ensure_tessdata() -> None:
    if os.environ.get("TESSDATA_PREFIX"):
        return
    for cand in (
        r"C:\Program Files\Tesseract-OCR\tessdata",
        r"C:\Program Files (x86)\Tesseract-OCR\tessdata",
        "/usr/share/tesseract-ocr/4.00/tessdata",
        "/usr/share/tesseract-ocr/5/tessdata",
        "/usr/share/tessdata",
        "/opt/homebrew/share/tessdata",
    ):
        if os.path.isdir(cand):
            os.environ["TESSDATA_PREFIX"] = cand
            return
    # Last resort: glob common Windows install roots.
    for pattern in (
        r"C:\Program Files*\Tesseract-OCR\tessdata",
    ):
        for hit in glob.glob(pattern):
            if os.path.isdir(hit):
                os.environ["TESSDATA_PREFIX"] = hit
                return


_ensure_tessdata()


def has_text_layer(pdf_path: str | Path, sample_pages: int = 12,
                   min_chars: int = 200) -> bool:
    """Heuristic: does the PDF already contain extractable text?

    Samples up to `sample_pages` spread across the document; returns True if
    the cumulative extracted text exceeds `min_chars`.
    """
    with fitz.open(pdf_path) as doc:
        n = len(doc)
        if n == 0:
            return False
        if n <= sample_pages:
            idxs = range(n)
        else:
            step = n / sample_pages
            idxs = {int(i * step) for i in range(sample_pages)}
        total = 0
        for i in idxs:
            try:
                total += len((doc[i].get_text() or "").strip())
            except Exception:
                pass
            if total >= min_chars:
                return True
    return total >= min_chars


# ── Per-page OCR worker (runs in a separate process) ─────────────────
def _worker_init() -> None:
    """Keep each OCR worker single-threaded so N parallel workers don't each
    spin up M math/Tesseract threads (which exhausts memory on busy machines)."""
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "OMP_THREAD_LIMIT", "MKL_NUM_THREADS"):
        os.environ[var] = "1"
    _ensure_tessdata()


def _ocr_one_page(args) -> tuple[int, Optional[bytes]]:
    pdf_path, idx, dpi = args
    _ensure_tessdata()
    try:
        with fitz.open(pdf_path) as doc:
            pix = doc[idx].get_pixmap(dpi=dpi)
            return idx, pix.pdfocr_tobytes(language="eng")
    except Exception:
        return idx, None


def ensure_searchable_pdf(
    pdf_path: str | Path,
    dpi: int = 200,
    progress: Optional[Callable[[int, int], None]] = None,
    max_workers: Optional[int] = None,
) -> dict:
    """If `pdf_path` has no text layer, OCR every page and overwrite it with a
    searchable PDF. Returns a dict describing what happened.

    `progress(done, total)` is called as pages complete (best-effort).
    """
    pdf_path = Path(pdf_path)

    if has_text_layer(pdf_path):
        with fitz.open(pdf_path) as doc:
            return {"ocr": False, "pages": len(doc), "reason": "text-layer-present"}

    with fitz.open(pdf_path) as doc:
        total = len(doc)

    if total == 0:
        return {"ocr": False, "pages": 0, "reason": "empty-pdf"}

    if max_workers is None:
        env_workers = os.environ.get("OCR_MAX_WORKERS")
        if env_workers and env_workers.isdigit():
            max_workers = max(1, int(env_workers))
        else:
            # Leave headroom for the API server(s) sharing this machine.
            max_workers = max(1, min(8, (os.cpu_count() or 2) - 2))
    max_workers = min(max_workers, total)

    # OCR pages in parallel; collect single-page searchable-PDF bytes by index.
    page_bytes: dict[int, Optional[bytes]] = {}
    done = 0
    tasks = [(str(pdf_path), i, dpi) for i in range(total)]

    with ProcessPoolExecutor(max_workers=max_workers,
                             initializer=_worker_init) as ex:
        futures = [ex.submit(_ocr_one_page, t) for t in tasks]
        for fut in as_completed(futures):
            idx, data = fut.result()
            page_bytes[idx] = data
            done += 1
            if progress:
                try:
                    progress(done, total)
                except Exception:
                    pass

    # Reassemble in page order. Pages that failed OCR fall back to the original
    # (image-only) page so the document length and layout are preserved.
    out = fitz.open()
    with fitz.open(pdf_path) as src:
        for i in range(total):
            data = page_bytes.get(i)
            if data:
                with fitz.open("pdf", data) as one:
                    out.insert_pdf(one)
            else:
                out.insert_pdf(src, from_page=i, to_page=i)

    tmp = pdf_path.with_suffix(".ocr.pdf")
    out.save(str(tmp), garbage=4, deflate=True)
    out.close()
    os.replace(tmp, pdf_path)

    failed = sum(1 for i in range(total) if not page_bytes.get(i))
    return {
        "ocr": True,
        "pages": total,
        "failed_pages": failed,
        "dpi": dpi,
        "workers": max_workers,
    }


if __name__ == "__main__":
    import sys, json, time
    p = sys.argv[1] if len(sys.argv) > 1 else "_test_tesco.pdf"
    t0 = time.time()
    info = ensure_searchable_pdf(p, progress=lambda d, t: print(f"  ocr {d}/{t}", end="\r"))
    info["secs"] = round(time.time() - t0, 1)
    print("\n" + json.dumps(info, indent=2))

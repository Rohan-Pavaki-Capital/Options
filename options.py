#!/usr/bin/env python3
"""
Share-Based Compensation / Options Data Extractor (v3 — Hybrid Detection)
==========================================================================
Extracts structured options/equity compensation data from financial report PDFs.

Architecture:
  STAGE 1: Lenient keyword filter (free, fast)
           → Catches all plausible candidates
           → Patterns live in keywords.py
  STAGE 2: Together AI LLM classifier (cheap, accurate)
           → Eliminates false positives universally
           → Prompts live in prompt.py
  STAGE 3: Claude Sonnet extraction (premium quality)
           → Structured JSON with validation
           → Code, prompts, and schema live in Anthropic/ package

Setup .env file:
    ANTHROPIC_API_KEY=sk-ant-...
    TOGETHER_API_KEY=...
    TOGETHER_MODEL=deepseek-ai/DeepSeek-V3

Usage:
    python options.py <pdf_path> [options]

Requirements:
    pip install anthropic openai pymupdf pypdf python-dotenv
    System: poppler-utils (for pdftoppm)
"""

import argparse
import json
import os
import sys
import re
import time
import base64
import subprocess
import tempfile
import glob
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dataclasses import dataclass, field

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

try:
    import anthropic
except ImportError:
    sys.exit("ERROR: pip install anthropic")

try:
    from openai import OpenAI
except ImportError:
    sys.exit("ERROR: pip install openai")

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("ERROR: pip install pypdf")

try:
    import fitz  # PyMuPDF — used for both text extraction AND page rasterization
except ImportError:
    sys.exit("ERROR: pip install pymupdf")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import cache
from prompt import CLASSIFIER_SYSTEM_PROMPT, CLASSIFIER_USER_PROMPT
from keywords import (
    PLAN_CODE_KEYWORDS,
    VALUATION_KEYWORDS,
    GENERIC_KEYWORDS,
    ROLLFORWARD_PATTERNS,
    SECTION_HEADERS,
    NOTE_TITLE_PATTERNS,
    NON_ENGLISH_KEYWORDS,
)
# Stage 3 — Anthropic Claude Sonnet extraction lives in its own package
from Anthropic import (
    extract_with_claude,
    validate_all_plans,
    validate_final_output,
    merge_results,
    set_verbose as _set_anthropic_verbose,
)
# Stage 4 — JSON → Excel converter
from format.json_to_excel import build_workbook
# Stage 5 — Persist results to NeonDB
from database.storage import save_extraction


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: STAGE 1 — LENIENT CANDIDATE FILTER
# Keyword patterns live in keywords.py
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PageMatch:
    """Lightweight match record for Stage 1 keyword filter."""
    page_num: int
    plan_code_hits: list = field(default_factory=list)
    valuation_hits: list = field(default_factory=list)
    generic_hits: list = field(default_factory=list)
    rollforward_hits: list = field(default_factory=list)
    header_hits: list = field(default_factory=list)
    note_title_hits: list = field(default_factory=list)
    non_english_hits: list = field(default_factory=list)
    has_numeric_table: bool = False
    text_length: int = 0

    @property
    def is_candidate(self) -> bool:
        """A page is a candidate if ANY of the lenient conditions match."""
        if self.text_length < 100:
            return False
        # Tier 1+2+6: high-precision signals — single match qualifies
        if self.note_title_hits:
            return True
        if self.plan_code_hits:
            return True
        if self.valuation_hits:
            return True
        # Tier 4: roll-forward — 3+ matches alone qualifies
        if len(self.rollforward_hits) >= 3:
            return True
        # Tier 3+5: generic signals — require a numeric table for corroboration
        if self.generic_hits and self.has_numeric_table:
            return True
        if self.header_hits and self.has_numeric_table:
            return True
        # Tier 7: international support
        if self.non_english_hits:
            return True
        # Borderline: 1-2 rollforward terms + any plan-related keyword
        if self.rollforward_hits and (self.generic_hits or self.header_hits):
            return True
        return False

    @property
    def reason(self) -> str:
        reasons = []
        if self.note_title_hits:
            reasons.append(f"note-title:{self.note_title_hits[0]}")
        if self.plan_code_hits:
            reasons.append(f"plan-code:{self.plan_code_hits[0]}")
        if self.valuation_hits:
            reasons.append(f"valuation:{self.valuation_hits[0]}")
        if len(self.rollforward_hits) >= 3:
            reasons.append(f"rollforward×{len(self.rollforward_hits)}")
        elif self.rollforward_hits and (self.generic_hits or self.header_hits):
            reasons.append(f"rollforward×{len(self.rollforward_hits)}+keyword")
        if self.generic_hits and self.has_numeric_table:
            reasons.append(f"generic+table:{self.generic_hits[0]}")
        if self.header_hits and self.has_numeric_table:
            reasons.append(f"header+table:{self.header_hits[0]}")
        if self.non_english_hits:
            reasons.append(f"non-en:{self.non_english_hits[0]}")
        return "; ".join(reasons) if reasons else "(no match)"


def match_page(page_num: int, text: str, page_obj=None) -> PageMatch:
    """Run all keyword patterns against page text; check table for numbers."""
    pm = PageMatch(page_num=page_num, text_length=len(text))
    text_lower = text.lower()

    for pattern in PLAN_CODE_KEYWORDS:
        m = re.search(pattern, text_lower)
        if m:
            pm.plan_code_hits.append(m.group().strip())
    for pattern in VALUATION_KEYWORDS:
        m = re.search(pattern, text_lower)
        if m:
            pm.valuation_hits.append(m.group().strip())
    for pattern in GENERIC_KEYWORDS:
        m = re.search(pattern, text_lower)
        if m:
            pm.generic_hits.append(m.group().strip())
    for pattern in ROLLFORWARD_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            pm.rollforward_hits.append(m.group().strip())
    for pattern in SECTION_HEADERS:
        m = re.search(pattern, text_lower)
        if m:
            pm.header_hits.append(m.group().strip())
    for pattern in NOTE_TITLE_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            pm.note_title_hits.append(m.group().strip())
    for pattern in NON_ENGLISH_KEYWORDS:
        target = text_lower if pattern.isascii() else text
        m = re.search(pattern, target)
        if m:
            pm.non_english_hits.append(m.group().strip())

    # Table detection is consulted by is_candidate/reason ONLY when there are
    # generic or header hits (Tier 3/5). Skip the expensive PyMuPDF table scan
    # otherwise — identical result, far less work on large reports (most pages
    # have no generic/header hit, so this avoids hundreds of needless scans).
    if page_obj is not None and (pm.generic_hits or pm.header_hits):
        for tbl in _find_tables(page_obj):
            try:
                rows = tbl.extract()
            except Exception:
                continue
            for row in (rows or []):
                for cell in (row or []):
                    if cell and re.search(r'\d[\d,\.]+', str(cell)):
                        pm.has_numeric_table = True
                        break
                if pm.has_numeric_table:
                    break
            if pm.has_numeric_table:
                break
    return pm


def _find_tables(page) -> list:
    """
    PyMuPDF table detection wrapper. Tries 'lines' strategy first (catches
    bordered tables), falls back to 'text' (catches borderless tables that
    are laid out purely by alignment — common in financial reports).
    Returns a flat list of fitz Table objects.
    """
    for strategy in ("lines", "text"):
        try:
            tf = page.find_tables(strategy=strategy)
            tables = list(tf.tables) if tf else []
            if tables:
                return tables
        except Exception:
            continue
    return []


def keyword_filter(pdf_path: str) -> tuple[list[int], dict]:
    """
    Stage 1: Lenient keyword scan over the entire PDF.

    Returns (candidate_pages, matches_by_page) where matches_by_page maps
    page_num -> PageMatch for downstream logging.
    """
    matches = {}
    candidates = []
    with fitz.open(pdf_path) as doc:
        total = len(doc)
        log(f"   Scanning {total} pages...")
        for i in range(total):
            page = doc.load_page(i)
            try:
                text = page.get_text() or ""
            except Exception:
                text = ""
            pm = match_page(i + 1, text, page)
            matches[i + 1] = pm
            if pm.is_candidate:
                candidates.append(i + 1)
    return candidates, matches


def toc_fallback(pdf_path: str) -> list[int]:
    """Scan front-matter for table-of-contents references to share-based notes."""
    toc_patterns = [
        r"share[\s\-]*based.*?(?:page\s*)?(\d+)",
        r"(?:note|notes?)\s*\d+[:\.\s]+.*?(?:share|stock|option|equity).*?(\d+)",
        r"employee\s*(?:benefit|compensation).*?(\d+)",
    ]
    relevant = set()
    with fitz.open(pdf_path) as doc:
        total_pages = len(doc)
        for i in range(min(10, total_pages)):
            try:
                text = (doc.load_page(i).get_text() or "").lower()
            except Exception:
                continue
            for pattern in toc_patterns:
                for m in re.findall(pattern, text):
                    try:
                        pg = int(m)
                        if 1 <= pg <= total_pages:
                            for p in range(max(1, pg - 1), min(total_pages, pg + 3) + 1):
                                relevant.add(p)
                    except ValueError:
                        pass
    return sorted(relevant)


def get_page_text(pdf_path: str, page_num: int) -> str:
    """Cheap text-only extraction for a single page (used by Stage 2)."""
    with fitz.open(pdf_path) as doc:
        idx = page_num - 1
        if 0 <= idx < len(doc):
            try:
                return doc.load_page(idx).get_text() or ""
            except Exception:
                return ""
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: STAGE 2 — TOGETHER AI LLM CLASSIFIER
# Prompts live in prompt.py
# ═══════════════════════════════════════════════════════════════════════════════

def _truncate_for_classifier(page_text: str) -> str:
    """Truncate page text for the Stage-2 classifier. Tables (high digit
    density) get more room.

    When a page is longer than the head budget we ALSO append a tail slice:
    in financial notes the roll-forward / activity table almost always trails
    a block of narrative plan descriptions, and a head-only cut silently drops
    it — the classifier then sees only prose and wrongly rejects the page. The
    head window is preserved exactly, so any page kept before stays kept; the
    tail is purely additive.
    """
    if not page_text:
        return ""
    # Digit density over the WHOLE page (not just the first 1k chars) so a
    # trailing table can still widen the head window when the page opens with
    # narrative.
    digit_chars = sum(1 for c in page_text if c.isdigit())
    digit_density = digit_chars / max(len(page_text), 1)
    head_limit = 3500 if digit_density > 0.10 else 2500
    tail_limit = 1500
    # Whole page already fits in the combined budget — return it intact
    # (strictly more context than the old head-only cut, never less).
    if len(page_text) <= head_limit + tail_limit:
        return page_text
    return page_text[:head_limit] + "\n[...]\n" + page_text[-tail_limit:]


def parse_classification(raw_response: str) -> dict:
    """
    Robustly parse the LLM classifier response.
    Defaults to KEEP/LOW if parsing fails (let Sonnet decide downstream).
    """
    raw = (raw_response or "").strip()

    decision_match = re.search(r"DECISION\s*:\s*(KEEP|REJECT)", raw, re.IGNORECASE)
    confidence_match = re.search(r"CONFIDENCE\s*:\s*(HIGH|MEDIUM|LOW)", raw, re.IGNORECASE)
    reason_match = re.search(r"REASON\s*:\s*(.+?)(?:\n\s*\n|$)", raw, re.IGNORECASE | re.DOTALL)

    if decision_match:
        decision = decision_match.group(1).upper()
        keep = decision == "KEEP"
    else:
        if re.search(r"\bREJECT\b", raw, re.IGNORECASE):
            decision = "REJECT"
            keep = False
        elif re.search(r"\bKEEP\b", raw, re.IGNORECASE):
            decision = "KEEP"
            keep = True
        else:
            decision = "KEEP"
            keep = True

    confidence = confidence_match.group(1).upper() if confidence_match else "LOW"
    reason = reason_match.group(1).strip() if reason_match else "(no reason parsed)"
    reason = re.sub(r"\s+", " ", reason)[:200]

    return {
        "keep": keep,
        "decision": decision,
        "confidence": confidence,
        "reason": reason,
    }


def classify_with_llm(
    client: OpenAI,
    model: str,
    page_text: str,
    page_num: int,
    cost_tracker=None,
    max_retries: int = 3,
) -> dict:
    """
    Stage 2: Together AI classifier. Returns:
        {"keep": bool, "decision": str, "confidence": str, "reason": str}

    Cache: keyed on (model, system_prompt, page_text). A cache hit avoids
    the API call entirely. On repeated API failure, defaults to KEEP/LOW
    so Sonnet can make the final call.
    """
    truncated = _truncate_for_classifier(page_text)

    # ── Cache lookup ──────────────────────────────────────────────────────
    cached = cache.get("classifications", model, CLASSIFIER_SYSTEM_PROMPT, truncated)
    if cached is not None:
        log(f"     ⚡ Page {page_num}: cache hit")
        return cached

    user_prompt = CLASSIFIER_USER_PROMPT.format(page_num=page_num, page_text=truncated)

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=200,
                temperature=0,
            )
            raw = resp.choices[0].message.content or ""

            if cost_tracker is not None and getattr(resp, "usage", None):
                cost_tracker.add_together(
                    getattr(resp.usage, "prompt_tokens", 0) or 0,
                    getattr(resp.usage, "completion_tokens", 0) or 0,
                )

            result = parse_classification(raw)
            # Cache successful results (don't cache classifier-errors)
            cache.set("classifications", result, model, CLASSIFIER_SYSTEM_PROMPT, truncated)
            return result
        except Exception as e:
            last_error = e
            msg = str(e).lower()
            if "429" in msg or "rate" in msg or "timeout" in msg:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    log(f"     ⏳ Together AI rate-limited, retry in {wait}s...")
                    time.sleep(wait)
                    continue
            break

    log(f"     ⚠ Classifier failed for page {page_num}: {last_error}. Defaulting to KEEP.")
    return {
        "keep": True,
        "decision": "KEEP",
        "confidence": "LOW",
        "reason": f"classifier-error: {str(last_error)[:80]}",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: COMBINED DETECTION PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def detect_relevant_pages(
    pdf_path: str,
    together_client=None,
    together_model: str = None,
    skip_llm: bool = False,
    debug: bool = False,
    cost_tracker=None,
) -> tuple[list[int], dict]:
    """
    Three-stage pipeline:
      1. Lenient keyword filter → candidates
      2. LLM classifier → confirmed pages
      3. Neighbor expansion (LLM-verified) → final set
    """
    log("\n🔍 Stage 1: Keyword filter")
    candidates, matches = keyword_filter(pdf_path)

    if debug:
        for pg in candidates:
            log(f"     Page {pg}: {matches[pg].reason}")

    if not candidates:
        log("   ⚠ No keyword matches. Trying TOC fallback...")
        candidates = toc_fallback(pdf_path)
        if candidates:
            log(f"   ✓ TOC fallback found {len(candidates)} pages: {candidates}")
        else:
            log("   ⚠ TOC fallback failed. Will scan ALL pages with LLM.")
            with fitz.open(pdf_path) as doc:
                candidates = list(range(1, len(doc) + 1))

    log(f"   ✓ {len(candidates)} candidate pages: {candidates}")

    classifications = {}

    if skip_llm:
        log("\n⏭  Stage 2: SKIPPED (--skip-llm-filter)")
        for pg in candidates:
            classifications[pg] = {
                "keep": True, "decision": "KEEP",
                "confidence": "N/A", "reason": "skipped",
            }
        return sorted(candidates), classifications

    if together_client is None:
        log("\n⚠ Stage 2: No Together AI client — keyword-only mode")
        for pg in candidates:
            classifications[pg] = {
                "keep": True, "decision": "KEEP",
                "confidence": "N/A", "reason": "no-classifier",
            }
        return sorted(candidates), classifications

    log(f"\n🤖 Stage 2: LLM classification ({together_model})")
    log(f"   Classifying {len(candidates)} candidates...")

    # Classify pages CONCURRENTLY (each call is an independent, I/O-bound API
    # round-trip). Identical results to the old sequential loop — only the
    # wall-clock changes. Logging is emitted in deterministic page order after
    # all results are in. The cache + thread-safe CostTracker make this safe.
    def _classify_pages(pages):
        results = {}
        if not pages:
            return results
        # Extract all page texts in ONE PDF pass (get_page_text re-opens the file
        # per call — wasteful for large PDFs with many candidates). Same text.
        texts = {}
        with fitz.open(pdf_path) as doc:
            n = len(doc)
            for pg in pages:
                idx = pg - 1
                try:
                    texts[pg] = doc.load_page(idx).get_text() or "" if 0 <= idx < n else ""
                except Exception:
                    texts[pg] = ""
        try:
            cap = int(os.environ.get("STAGE2_MAX_WORKERS", "16"))
        except ValueError:
            cap = 16
        workers = max(1, min(cap, len(pages)))

        def _one(pg):
            return pg, classify_with_llm(
                together_client, together_model, texts[pg], pg, cost_tracker,
            )
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for pg, res in ex.map(_one, pages):
                results[pg] = res
        return results

    stage2 = _classify_pages(candidates)
    confirmed = []
    errors = 0
    for page_num in candidates:
        result = stage2[page_num]
        classifications[page_num] = result
        marker = "✓" if result["keep"] else "✗"
        decision_label = "KEEP  " if result["keep"] else "REJECT"
        log(f"   {marker} Page {page_num}: {decision_label} ({result['confidence']}) - {result['reason']}")
        if result["reason"].startswith("classifier-error"):
            errors += 1
        if result["keep"]:
            confirmed.append(page_num)

    # Resilience: broad classifier failure → fall back to keyword-only (keep all),
    # same intent as the original consecutive-failure guard.
    if errors >= 5:
        log("   ⚠ Too many classifier failures. Falling back to keyword-only.")
        for pg in candidates:
            classifications[pg] = {
                "keep": True, "decision": "KEEP",
                "confidence": "N/A", "reason": "classifier-down",
            }
        return sorted(candidates), classifications

    log(f"   ✓ {len(confirmed)} pages passed classification")

    # Stage 3: Neighbor expansion for HIGH-confidence confirmed pages
    log("\n🔗 Stage 3: Neighbor expansion")
    expanded = set(confirmed)
    total_pages = len(matches)  # keyword_filter populates matches for every page
    high_conf = [pg for pg in confirmed if classifications[pg].get("confidence") == "HIGH"]

    # Collect unique neighbors to classify (a page is classified once even if it
    # neighbours two confirmed pages), then classify them concurrently too.
    # Neighbors are checked even if Stage 1 did NOT flag them — this is what
    # rescues note CONTINUATION pages (e.g. a roll-forward table that spills onto
    # the page after the note header). Bound to valid page numbers; Stage 2 still
    # gates each neighbor, so only genuinely relevant adjacent pages are added.
    neighbors, parent_of = [], {}
    for page_num in high_conf:
        for neighbor in (page_num - 1, page_num + 1):
            if 1 <= neighbor <= total_pages and neighbor not in expanded and neighbor not in parent_of:
                parent_of[neighbor] = page_num
                neighbors.append(neighbor)

    nres = _classify_pages(neighbors)
    for neighbor in neighbors:
        result = nres[neighbor]
        classifications[neighbor] = result
        marker = "✓" if result["keep"] else "✗"
        decision_label = "KEEP  " if result["keep"] else "REJECT"
        log(f"   {marker} Page {neighbor} (neighbor of {parent_of[neighbor]}): {decision_label} ({result['confidence']}) - {result['reason']}")
        if result["keep"]:
            expanded.add(neighbor)

    added = len(expanded) - len(confirmed)
    if added > 0:
        log(f"   ✓ Added {added} neighbor page(s)")
    else:
        log("   (no neighbors added)")

    return sorted(expanded), classifications


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: COST TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

TOGETHER_PRICING = {
    "deepseek-v3":          {"input": 0.27, "output": 1.10},
    "llama-3.3-70b":        {"input": 0.88, "output": 0.88},
    "qwen2.5-72b":          {"input": 1.20, "output": 1.20},
    "gpt-oss-120b":         {"input": 0.15, "output": 0.60},
    "_default":             {"input": 0.60, "output": 0.60},
}

ANTHROPIC_SONNET_PRICING = {"input": 3.0, "output": 15.0}


def _together_pricing_for(model: str) -> dict:
    if not model:
        return TOGETHER_PRICING["_default"]
    key = model.lower()
    for substr, price in TOGETHER_PRICING.items():
        if substr != "_default" and substr in key:
            return price
    return TOGETHER_PRICING["_default"]


class CostTracker:
    def __init__(self, together_model: str = "deepseek-ai/DeepSeek-V3"):
        self.together_model = together_model
        self.together_input_tokens = 0
        self.together_output_tokens = 0
        self.anthropic_input_tokens = 0
        self.anthropic_output_tokens = 0
        self._lock = threading.Lock()   # token counters are updated from parallel Stage-2 threads

    def add_together(self, input_tok: int, output_tok: int):
        with self._lock:
            self.together_input_tokens += input_tok
            self.together_output_tokens += output_tok

    def add_anthropic(self, input_tok: int, output_tok: int):
        with self._lock:
            self.anthropic_input_tokens += input_tok
            self.anthropic_output_tokens += output_tok

    def together_cost(self) -> float:
        p = _together_pricing_for(self.together_model)
        return (self.together_input_tokens / 1e6) * p["input"] + \
               (self.together_output_tokens / 1e6) * p["output"]

    def anthropic_cost(self) -> float:
        p = ANTHROPIC_SONNET_PRICING
        return (self.anthropic_input_tokens / 1e6) * p["input"] + \
               (self.anthropic_output_tokens / 1e6) * p["output"]

    def total_cost(self) -> float:
        return self.together_cost() + self.anthropic_cost()

    def summary(self) -> dict:
        return {
            "together": {
                "model": self.together_model,
                "input_tokens": self.together_input_tokens,
                "output_tokens": self.together_output_tokens,
                "cost_usd": round(self.together_cost(), 4),
            },
            "anthropic": {
                "input_tokens": self.anthropic_input_tokens,
                "output_tokens": self.anthropic_output_tokens,
                "cost_usd": round(self.anthropic_cost(), 4),
            },
            "total_cost_usd": round(self.total_cost(), 4),
        }

    def print_summary(self):
        log("\n💰 Cost breakdown:")
        log(f"   Together AI ({self.together_model})")
        log(f"     input  tokens: {self.together_input_tokens:>10,}")
        log(f"     output tokens: {self.together_output_tokens:>10,}")
        log(f"     cost:          ${self.together_cost():.4f}")
        log(f"   Anthropic (Sonnet)")
        log(f"     input  tokens: {self.anthropic_input_tokens:>10,}")
        log(f"     output tokens: {self.anthropic_output_tokens:>10,}")
        log(f"     cost:          ${self.anthropic_cost():.4f}")
        log(f"   ─────────────────────────────")
        log(f"   TOTAL:           ${self.total_cost():.4f}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: PDF CONTENT EXTRACTION (for Stage 3)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_text_from_pages(pdf_path: str, pages: list[int]) -> dict:
    result = {}
    with fitz.open(pdf_path) as doc:
        total = len(doc)
        for pg in pages:
            idx = pg - 1
            if 0 <= idx < total:
                try:
                    page = doc.load_page(idx)
                    text = page.get_text() or ""
                    tables = _find_tables(page)
                    if tables:
                        text += "\n\n[STRUCTURED TABLES]\n"
                        for t_idx, tbl in enumerate(tables):
                            text += f"\n[TABLE {t_idx + 1}]\n"
                            for row in tbl.extract():
                                cleaned = [str(c).strip() if c else "" for c in row]
                                text += " | ".join(cleaned) + "\n"
                    result[pg] = text
                except Exception as e:
                    log(f"  WARNING: Text extraction failed page {pg}: {e}")
                    result[pg] = ""
    return result


def rasterize_pages(pdf_path: str, pages: list[int], dpi: int = 200) -> dict:
    """
    Rasterize specific PDF pages to base64-encoded JPEG strings.

    Uses PyMuPDF (fitz) — pure Python, no external binaries required.
    Returns {page_num: base64_jpeg_string}.
    """
    if not pages:
        return {}
    if fitz is None:
        log("  WARNING: PyMuPDF not installed. Run `pip install pymupdf`. Using text-only.")
        return {}

    images = {}
    zoom = dpi / 72.0  # 72 dpi is PyMuPDF's native unit
    matrix = fitz.Matrix(zoom, zoom)

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        log(f"  WARNING: Could not open PDF for rasterization: {e}")
        return {}

    try:
        total = len(doc)
        for pg in pages:
            idx = pg - 1
            if not (0 <= idx < total):
                continue
            try:
                page = doc.load_page(idx)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                jpeg_bytes = pix.tobytes("jpeg")
                images[pg] = base64.standard_b64encode(jpeg_bytes).decode("utf-8")
            except Exception as e:
                log(f"  WARNING: Rasterize failed page {pg}: {e}")
    finally:
        doc.close()
    return images


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9: MAIN
# ═══════════════════════════════════════════════════════════════════════════════

VERBOSE = True


def log(msg: str):
    if VERBOSE:
        print(msg, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Extract share-based compensation data from financial report PDFs (v3 — hybrid detection).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python options.py annual_report.pdf -o results.json
  python options.py annual_report.pdf --pages 45-52
  python options.py annual_report.pdf --text-only
  python options.py annual_report.pdf --estimate
  python options.py annual_report.pdf --detect-only
  python options.py annual_report.pdf --skip-llm-filter
  python options.py annual_report.pdf --llm-model meta-llama/Llama-3.3-70B-Instruct-Turbo
        """
    )
    parser.add_argument("pdf_path", help="Path to the financial report PDF")
    parser.add_argument("-o", "--output",
                        help="Output JSON path (default: ./output/<pdf_name>_options.json)")
    parser.add_argument("-p", "--pages", help="Page range, e.g. '45-52' (default: auto-detect)")
    parser.add_argument("--text-only", action="store_true", help="Skip vision (cheaper, less accurate)")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Claude model for extraction")
    parser.add_argument("--llm-model", default=None,
                        help="Override TOGETHER_MODEL env variable (Stage 2 classifier)")
    parser.add_argument("--skip-llm-filter", action="store_true",
                        help="Skip LLM classification stage (keyword-only, faster but less accurate)")
    parser.add_argument("--max-pages-per-call", type=int, default=12, help="Max pages per API call")
    parser.add_argument("--estimate", action="store_true", help="Show cost estimate only")
    parser.add_argument("--detect-only", action="store_true",
                        help="Run page detection ONLY — no extraction. Useful for verifying classification.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    parser.add_argument("--skip-validation", action="store_true", help="Skip 2nd validation pass")
    parser.add_argument("--debug-scores", action="store_true", help="Show keyword match reasons for all candidates")
    parser.add_argument("--no-cache", action="store_true",
                        help="Disable disk cache + Anthropic prompt caching (force fresh API calls)")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Delete all cached entries before running")

    args = parser.parse_args()
    global VERBOSE
    VERBOSE = not args.quiet
    _set_anthropic_verbose(VERBOSE)

    # ── Cache configuration ──
    if args.clear_cache:
        removed = cache.clear()
        log(f"🧹 Cache cleared ({removed} entries removed)")
    if args.no_cache:
        cache.set_enabled(False)
        log("⚠ Cache disabled (--no-cache)")
    else:
        existing = cache.stats()
        if existing:
            ns_summary = ", ".join(f"{k}={v}" for k, v in sorted(existing.items()))
            log(f"💾 Cache enabled ({ns_summary})")

    pdf_path = args.pdf_path
    if not os.path.exists(pdf_path):
        sys.exit(f"ERROR: File not found: {pdf_path}")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    together_key = os.environ.get("TOGETHER_API_KEY")
    together_model = args.llm_model or os.environ.get("TOGETHER_MODEL", "deepseek-ai/DeepSeek-V3")

    needs_anthropic = not (args.estimate or args.detect_only)
    if needs_anthropic and not anthropic_key:
        sys.exit("ERROR: Set ANTHROPIC_API_KEY environment variable")

    use_llm_filter = not args.skip_llm_filter
    if use_llm_filter and (not together_key or together_key == "your_together_key_here"):
        log("⚠ TOGETHER_API_KEY not set — Stage 2 (LLM classifier) will be skipped.")
        log("  Set TOGETHER_API_KEY in .env or use --skip-llm-filter to silence this warning.")
        use_llm_filter = False

    cost_tracker = CostTracker(together_model=together_model)

    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)
    log(f"\n📄 PDF: {pdf_path} ({total_pages} pages)")

    debug_scores = args.debug_scores or args.detect_only
    classifications = {}

    if args.pages:
        parts = args.pages.split("-")
        s, e = int(parts[0]), int(parts[-1])
        target_pages = list(range(s, e + 1))
        log(f"📌 Specified pages: {s}-{e}")
    else:
        together_client = None
        if use_llm_filter and together_key:
            try:
                together_client = OpenAI(
                    api_key=together_key,
                    base_url="https://api.together.xyz/v1",
                )
            except Exception as e:
                log(f"⚠ Failed to init Together AI client: {e}. Falling back to keyword-only.")
                together_client = None

        target_pages, classifications = detect_relevant_pages(
            pdf_path,
            together_client=together_client,
            together_model=together_model,
            skip_llm=not use_llm_filter,
            debug=debug_scores,
            cost_tracker=cost_tracker,
        )
        log(f"\n🎯 Final: {len(target_pages)} page(s) {target_pages}")

    if args.detect_only:
        log(f"\n🔍 DETECTION ONLY (no extraction)")
        log(f"   Total PDF pages:     {total_pages}")
        log(f"   Pages selected:      {len(target_pages)}")
        log(f"   Pages:               {target_pages}")

        n = len(target_pages)
        passes = 1 if args.skip_validation else 2
        if args.text_only:
            est_input = (n * 400 + 3000) * passes
        else:
            est_input = (n * 2000 + 3000) * passes
        est_output = 3000 * passes
        est_extract_cost = est_input / 1e6 * 3 + est_output / 1e6 * 15

        log(f"\n💰 Estimated costs:")
        log(f"   Together AI (classification — already spent): ${cost_tracker.together_cost():.4f}")
        log(f"   Anthropic (extraction — if you proceed):       ${est_extract_cost:.4f}")
        log(f"   TOTAL:                                         ${cost_tracker.together_cost() + est_extract_cost:.4f}")

        if cost_tracker.together_input_tokens > 0:
            cost_tracker.print_summary()

        log(f"\n   To extract, re-run without --detect-only.")
        if target_pages:
            log(f"   To force these exact pages: --pages {target_pages[0]}-{target_pages[-1]}")
        return

    if args.estimate:
        n = len(target_pages)
        passes = 1 if args.skip_validation else 2
        if args.text_only:
            inp = (n * 400 + 3000) * passes
        else:
            inp = (n * 2000 + 3000) * passes
        out = 3000 * passes
        cost = inp / 1e6 * 3 + out / 1e6 * 15
        log(f"\n📊 ESTIMATE ({n} pages, {passes} pass{'es' if passes>1 else ''}):")
        log(f"   Anthropic input tokens:  ~{inp:,}")
        log(f"   Anthropic output tokens: ~{out:,}")
        log(f"   Est. extraction cost:    ${cost:.3f} (Sonnet)")
        if cost_tracker.together_input_tokens > 0:
            log(f"   Together AI cost so far: ${cost_tracker.together_cost():.4f}")
            log(f"   TOTAL estimated:         ${cost + cost_tracker.together_cost():.4f}")
        return

    log("\n📖 Extracting text...")
    texts = extract_text_from_pages(pdf_path, target_pages)

    images = {}
    if not args.text_only:
        log("🖼️  Rasterizing pages...")
        images = rasterize_pages(pdf_path, target_pages)
        log(f"   {len(images)} page images ready")

    client = anthropic.Anthropic(api_key=anthropic_key)
    batch_size = args.max_pages_per_call
    all_results = []

    for i in range(0, len(target_pages), batch_size):
        batch = target_pages[i:i + batch_size]
        log(f"\n🤖 Batch {i//batch_size+1}: pages {batch[0]}-{batch[-1]}")

        bt = {pg: texts[pg] for pg in batch if pg in texts}
        bi = {pg: images[pg] for pg in batch if pg in images}

        result = extract_with_claude(
            client, bt, bi, args.model,
            use_vision=not args.text_only,
            skip_validation=args.skip_validation,
            cost_tracker=cost_tracker,
        )
        all_results.append(result)

    if len(all_results) == 0:
        final = {"company_name": None, "report_period": None, "currency": None, "plans": []}
    elif len(all_results) == 1:
        final = all_results[0]
    else:
        log("\n🔗 Merging batches...")
        final = merge_results(all_results)

    log("\n✅ Arithmetic validation...")
    final = validate_all_plans(final)

    # Final quality validation — adds _validation_summary with completeness metrics
    final = validate_final_output(final)

    final["_meta"] = {
        "source_pdf": os.path.basename(pdf_path),
        "total_pdf_pages": total_pages,
        "pages_processed": target_pages,
        "mode": "text_only" if args.text_only else "vision+text",
        "model": args.model,
        "validation_pass": not args.skip_validation,
        "detection": {
            "stage2_classifier": together_model if use_llm_filter else "skipped",
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

    pdf_stem = Path(pdf_path).stem
    plans = len(final.get("plans", []))
    warns = len(final.get("_validation_warnings", []))

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_json = Path(tmpdir) / f"{pdf_stem}.json"
        tmp_xlsx = Path(tmpdir) / f"{pdf_stem}.xlsx"

        with open(tmp_json, "w", encoding="utf-8") as f:
            json.dump(final, f, indent=2, ensure_ascii=False)

        build_workbook(str(tmp_json), str(tmp_xlsx))
        xlsx_bytes = tmp_xlsx.read_bytes()

        extraction_id = save_extraction(final, xlsx_bytes, f"{pdf_stem}.xlsx")

    log(f"\n💾 Saved to NeonDB: extraction_id={extraction_id}")
    log(f"   Company: {final.get('company_name') or pdf_stem}")
    log(f"   Plans: {plans}")
    if warns:
        log(f"   ⚠ Warnings: {warns}")

    cost_tracker.print_summary()
    log("   Done!\n")


if __name__ == "__main__":
    main()

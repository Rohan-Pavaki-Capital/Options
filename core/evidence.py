"""
evidence.py — save the exact pages the options extraction referred to as a
trimmed, highlighted PDF, so the analyst can see precisely which source
pages produced the numbers (user request 2026-07-09).

Output: <project root>/HareKrishna/<TICKER>_<FORM>.pdf
    FORM = 10K | 10Q | 20F | AR (IR-website annual report) | UPLOAD | ...
    Same ticker+form overwrites on each run — the file always shows the
    latest extraction's evidence.

Each kept page gets a gray corner stamp ("source: TICKER FORM — original
page N") and yellow highlights on the share-based-compensation phrases the
extractor targets. Highlighting is keyword-based: it marks the relevant
tables/sentences, not the literal characters the LLM read.

Best-effort by design: callers wrap this in try/except — evidence must
never fail the pipeline.
"""

from __future__ import annotations

import re
from pathlib import Path

EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "HareKrishna"
BS_EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "HareRam"

# Phrases worth highlighting on the referred pages (case-insensitive —
# PyMuPDF's search_for ignores case).
_HIGHLIGHT_PHRASES = [
    "weighted-average exercise price", "weighted average exercise price",
    "weighted-average grant date fair value",
    "weighted average grant date fair value",
    "weighted-average grant-date fair value",
    "remaining contractual term", "aggregate intrinsic value",
    "options outstanding", "options granted", "options exercised",
    "options exercisable", "forfeited", "expired", "exercisable",
    "nonvested", "granted", "vested",
    "restricted stock unit", "performance share", "share units",
    "stock options", "stock appreciation right",
    "expected term", "expected life", "expected volatility",
    "risk-free", "dividend yield", "vesting period",
    "unrecognized compensation",
]


def _sanitize(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "", part or "") or "UNKNOWN"


def _labels_from_job(job: dict) -> tuple[str, str]:
    """(ticker, form_label) from a pipeline job record."""
    meta = (job or {}).get("source_meta") or {}
    ticker = str(meta.get("ticker") or "").strip().upper()
    if not ticker:
        # Uploads have no ticker — fall back to the file stem.
        ticker = Path(str((job or {}).get("filename") or "UNKNOWN")).stem

    form = str(meta.get("form") or "").strip().upper()
    if "ANNUAL REPORT" in form or meta.get("diamond_source") == "ir_scraper":
        label = "AR"
    elif form:
        label = form.replace("-", "").replace("/", "-")
    else:
        label = str((job or {}).get("source") or "UPLOAD").upper()
    return _sanitize(ticker), _sanitize(label)


def _final_response_values(final: dict) -> set[float]:
    """The numbers that appear in the FINAL /api/excel/options response
    (count_mn, strike, maturity_years per plan), expanded to the scales a
    filing may print them at (raw shares / thousands / millions)."""
    try:
        from core.excel_options import map_plans_to_excel
        rows = map_plans_to_excel(final or {})
    except Exception:
        rows = []
    vals: set[float] = set()
    for r in rows:
        c = r.get("count_mn")
        if isinstance(c, (int, float)) and c > 0:
            vals.update({round(c * 1_000_000, 2),   # raw shares: 16,876,310
                         round(c * 1_000, 4),       # thousands:  16,876.3
                         round(c, 6)})              # millions:   16.88
        for fld in ("strike", "maturity_years"):
            v = r.get(fld)
            if isinstance(v, (int, float)) and v > 0:
                vals.add(round(float(v), 4))
    return vals


def _word_number(word: str):
    """Parse a page word like '$16,876,310' / '(185.00)' / '7.6' to a float.
    Percentages are rejected — a '(7.6)%' growth rate must not match a
    7.6-year maturity."""
    if "%" in word:
        return None
    t = word.strip("$€£(),;:*†").replace(",", "").rstrip(".")
    if not t or len(t) > 15:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def save_evidence_pdf(pdf_path: str, pages: list[int], job: dict,
                      final: dict | None = None, log=None) -> Path | None:
    """Copy `pages` (1-based) of `pdf_path` into HareKrishna/<TICKER>_<FORM>.pdf
    and highlight ONLY the numbers that made it into the final response
    (count/strike/maturity per plan — user request 2026-07-09). Falls back
    to phrase highlighting when no final values are available. Returns the
    output path (or None if there was nothing to save)."""
    import fitz

    log = log or (lambda m: None)
    pages = sorted({int(p) for p in (pages or []) if int(p) >= 1})
    if not pages:
        return None

    ticker, label = _labels_from_job(job)
    out_path = EVIDENCE_DIR / f"{ticker}_{label}.pdf"
    values = _final_response_values(final) if final else set()

    with fitz.open(str(pdf_path)) as src:
        doc = fitz.open()
        kept = []
        for pg in pages:
            if pg <= len(src):
                # widgets=False: copying form widgets recurses through their
                # parent trees — deeply nested AcroForms (e.g. BMW annual
                # reports) overflow MuPDF's stack. Evidence pages only need
                # text/layout + our own highlight annotations.
                doc.insert_pdf(src, from_page=pg - 1, to_page=pg - 1,
                               widgets=False)
                kept.append(pg)
        if not kept:
            doc.close()
            return None

        total_marks = 0
        for i, pg in enumerate(kept):
            page = doc.load_page(i)
            n_marks = 0
            if values:
                # Highlight ONLY whole number-tokens equal to a final-response
                # value. Bare small integers (e.g. a maturity of "4") are
                # skipped — they'd light up every page number and date.
                for w in page.get_text("words"):
                    num = _word_number(w[4])
                    if num is None:
                        continue
                    if num < 10 and float(num).is_integer():
                        continue
                    if any(abs(num - v) < 0.005 for v in values):
                        page.add_highlight_annot(fitz.Rect(w[:4]))
                        n_marks += 1
            else:
                for phrase in _HIGHLIGHT_PHRASES:
                    for rect in page.search_for(phrase):
                        page.add_highlight_annot(rect)
                        n_marks += 1
            total_marks += n_marks
            # Gray corner stamp with the provenance.
            page.insert_text(
                (10, 12),
                f"source: {ticker} {label} — original page {pg} "
                f"({n_marks} final-value highlight{'s' if n_marks != 1 else ''})"
                if values else
                f"source: {ticker} {label} — original page {pg} "
                f"({n_marks} highlight{'s' if n_marks != 1 else ''})",
                fontsize=7, color=(0.45, 0.45, 0.45),
            )

        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_path))
        doc.close()

    log(f"evidence PDF saved: {out_path.name} ({len(kept)} page(s), "
        f"{total_marks} highlight(s), "
        f"{'final-values' if values else 'phrase'} mode)")
    return out_path


# ---------------------------------------------------------------------------
# Balance-sheet evidence (user request 2026-07-09): same idea as the options
# evidence above, but HYBRID highlighting — the final standardized values
# that appear literally on the page (totals, memo, single-line buckets) PLUS
# the anchor row labels — saved to HareRam/<TICKER>_<FORM>.pdf.
# ---------------------------------------------------------------------------

# Anchor labels of the rows the standardizer keys on (case-insensitive).
_BS_HIGHLIGHT_PHRASES = [
    "total assets", "total liabilities", "total equity",
    "total stockholders", "total shareholders",
    "total current assets", "total current liabilities",
    "cash and cash equivalents", "goodwill", "intangible",
    "long-term debt", "long term debt",
]


def _bs_final_values(result: dict) -> set[float]:
    """Every non-zero number in the standardized balance-sheet JSON (bucket
    values, preferred/mezzanine, memo_excluded, filing_totals), as absolute
    values — filings print negatives in parentheses, which _word_number
    parses unsigned."""
    vals: set[float] = set()

    def _add(v):
        if isinstance(v, (int, float)) and v != 0:
            vals.add(round(abs(float(v)), 4))

    r = result or {}
    for side in ("assets", "liabilities"):
        for group in (r.get(side) or {}).values():
            if isinstance(group, dict):
                for v in group.values():
                    _add(v)
            else:
                _add(group)  # preferred_stock / mezzanine_equity
    for section in ("memo_excluded", "filing_totals"):
        for v in (r.get(section) or {}).values():
            _add(v)
    return vals


def save_bs_evidence_pdf(pdf_path: str, pages: list[int], job: dict,
                         result: dict | None = None, log=None) -> Path | None:
    """Copy the located balance-sheet `pages` (1-based) of `pdf_path` into
    HareRam/<TICKER>_<FORM>.pdf with hybrid highlights: number tokens equal
    to a final standardized value AND the anchor row labels. Best-effort —
    callers wrap in try/except; must never fail the pipeline."""
    import fitz

    log = log or (lambda m: None)
    pages = sorted({int(p) for p in (pages or []) if int(p) >= 1})
    if not pages:
        return None

    ticker, label = _labels_from_job(job)
    out_path = BS_EVIDENCE_DIR / f"{ticker}_{label}.pdf"
    values = _bs_final_values(result or {})

    with fitz.open(str(pdf_path)) as src:
        doc = fitz.open()
        kept = []
        for pg in pages:
            if pg <= len(src):
                # widgets=False: same MuPDF stack-overflow guard as the
                # options evidence above (deeply nested AcroForm PDFs).
                doc.insert_pdf(src, from_page=pg - 1, to_page=pg - 1,
                               widgets=False)
                kept.append(pg)
        if not kept:
            doc.close()
            return None

        total_marks = 0
        for i, pg in enumerate(kept):
            page = doc.load_page(i)
            n_vals = 0
            words = page.get_text("words")
            matched = []
            # Final-value matches (same guards as options: skip bare small
            # integers so page numbers/dates never light up).
            for w in words:
                num = _word_number(w[4])
                if num is None:
                    continue
                if num < 10 and float(num).is_integer():
                    continue
                if any(abs(num - v) < 0.005 for v in values):
                    page.add_highlight_annot(fitz.Rect(w[:4]))
                    n_vals += 1
                    matched.append(w)
            # Field name of every matched value (user request 2026-07-10):
            # highlight the non-numeric words on the same visual line, left
            # of the value — e.g. "Accounts payable" next to 798.
            n_labels = 0
            seen_rows = set()
            for w in matched:
                row_key = round((w[1] + w[3]) / 2.0)
                if row_key in seen_rows:
                    continue
                seen_rows.add(row_key)
                label_rect = None
                for w2 in words:
                    if w2[3] <= w[1] or w2[1] >= w[3]:   # different line
                        continue
                    if w2[0] >= w[0]:                    # not left of value
                        continue
                    if w2[4] in ("$", "€", "£") or _word_number(w2[4]) is not None:
                        continue                          # skip other numbers
                    r = fitz.Rect(w2[:4])
                    label_rect = r if label_rect is None else label_rect | r
                if label_rect:
                    page.add_highlight_annot(label_rect)
                    n_labels += 1
            # Anchor row labels (totals/memo rows), even when their value
            # did not match.
            for phrase in _BS_HIGHLIGHT_PHRASES:
                for rect in page.search_for(phrase):
                    page.add_highlight_annot(rect)
                    n_labels += 1
            total_marks += n_vals + n_labels
            page.insert_text(
                (10, 12),
                f"source: {ticker} {label} — original page {pg} "
                f"({n_vals} value + {n_labels} label highlights)",
                fontsize=7, color=(0.45, 0.45, 0.45),
            )

        BS_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_path))
        doc.close()

    log(f"BS evidence PDF saved: {out_path.name} ({len(kept)} page(s), "
        f"{total_marks} highlight(s), hybrid mode)")
    return out_path

"""Stage 1 — locate the balance-sheet page(s) in a 10-Q/10-K PDF with PyMuPDF.

Finds the page whose text contains a balance-sheet title variant. If that page
alone holds both "Total assets" and a total-equity/liabilities line it is
captured by itself; otherwise the following page(s) are added until both are
covered (balance sheets often span two pages). The captured pages are exported
to a small temporary PDF for LlamaParse.
"""

import logging
import os
import re
import tempfile

import fitz  # PyMuPDF

from .config import TITLE_VARIANTS

logger = logging.getLogger("balance_sheet.pdf_locator")

_TOTAL_ASSETS_MARKER = "total assets"
# Some filers (e.g. Conagra) leave the assets-total row unlabeled — only the
# bare number is printed. The "Total current assets" subtotal still confirms
# the assets side of the statement was captured.
_ASSETS_SIDE_FALLBACK_MARKER = "total current assets"
# Any of these confirms the liabilities/equity side of the statement was captured.
_EQUITY_TOTAL_MARKERS = [
    "total liabilities and shareholders",
    "total liabilities and stockholders",
    "total liabilities and equity",
    "total liabilities, redeemable",
    "total equity",
    # Unlabeled L&E-total filers (e.g. Conagra) still print the equity total.
    "total stockholders",
    "total shareholders",
    # IFRS equity-first wording — European statements (BMW, L'Oréal) print
    # "Total equity and liabilities" / an "Equity & liabilities" section
    # header and never a standalone "Total liabilities".
    "equity and liabilities",
    "equity & liabilities",
]


def _page_text(doc: "fitz.Document", index: int) -> str:
    return doc[index].get_text("text").lower()


def _has_equity_total(text: str) -> bool:
    return any(marker in text for marker in _EQUITY_TOTAL_MARKERS)


def _has_assets_total(text: str) -> bool:
    return (_TOTAL_ASSETS_MARKER in text
            or _ASSETS_SIDE_FALLBACK_MARKER in text)


def _has_statement_structure(text: str) -> bool:
    """Lenient confirmation for filers (e.g. APA) that print EVERY total row
    unlabeled — no "Total assets" / "Total current assets" text exists on the
    statement at all. The section headings plus a labeled equity total still
    identify the page as the balance sheet (a TOC or prose cross-reference
    never carries all three)."""
    return ("current assets" in text
            and "current liabilities" in text
            and _has_equity_total(text))


def locate_balance_sheet(pdf_path: str) -> dict:
    """Find the balance-sheet page(s).

    Returns {"page_numbers": [1-based...], "temp_pdf_path": str,
             "matched_title": str, "warnings": [...]}.
    Raises RuntimeError if no balance sheet can be located.
    """
    if not os.path.isfile(pdf_path):
        raise RuntimeError(f"PDF not found: {pdf_path}")

    variants = [v.lower() for v in TITLE_VARIANTS]
    warnings: list[str] = []

    with fitz.open(pdf_path) as doc:
        result = _scan(doc, variants, warnings, lenient=False)
        if result is None:
            # No page passed the labeled-total check. Some filers (e.g. APA)
            # print every total row unlabeled, so re-scan accepting the
            # statement's structure (section headings + equity total) instead.
            result = _scan(doc, variants, warnings, lenient=True)
        if result is not None:
            return result

    raise RuntimeError(
        "No balance-sheet page found - none of the title variants matched a "
        "page that also contains 'Total assets' (or 'Total current assets')."
    )


def _scan(doc: "fitz.Document", variants: list[str], warnings: list[str],
          lenient: bool):
    """One pass over the document; returns the locate result dict or None.

    Strict mode confirms a title match with a labeled assets total ("Total
    assets" / "Total current assets") or — failing that — with the
    statement's section structure (_has_statement_structure): European
    filers (e.g. L'Oréal's URD) print every total row as a bare "TOTAL", so
    no label ever matches. Lenient mode confirms with the structure alone.

    A window whose equity/liabilities side is NOT confirmed is only kept as
    a last-resort FALLBACK, not accepted outright: a financial-highlights
    summary page matches title + "Total assets" exactly like the statement
    does, but never carries the equity side — accepting it outright would
    shadow the real statement further into the document."""
    confirm = _has_statement_structure if lenient else _has_assets_total

    def ok(text: str) -> bool:
        return confirm(text) or (not lenient
                                 and _has_statement_structure(text))

    fallback = None
    n_pages = len(doc)
    for i in range(n_pages):
        text = _page_text(doc, i)
        matched = next((v for v in variants if v in text), None)
        if not matched:
            continue

        # Capture the matched page alone when it already holds the whole
        # statement (both totals) — a needless next page is a different
        # statement (e.g. cash flows) that only feeds the LLM noise.
        # Otherwise add the next page (2-page statements).
        indices = [i]
        captured = text
        if (not ok(captured)
                or not _has_equity_total(captured)) and i + 1 < n_pages:
            indices.append(i + 1)
            captured = "\n".join(_page_text(doc, j) for j in indices)

        if not ok(captured):
            # Title without an assets total nearby — likely a table of
            # contents or a cross-reference; keep scanning.
            continue

        # If only "Total assets" is present, extend by one more page to
        # pick up the liabilities/equity side.
        if not _has_equity_total(captured) and indices[-1] + 1 < n_pages:
            indices.append(indices[-1] + 1)
            captured = "\n".join(_page_text(doc, j) for j in indices)

        if not _has_equity_total(captured):
            # Assets side only — remember the first such window and keep
            # scanning; the real (two-sided) statement may follow.
            if fallback is None:
                fallback = (list(indices), matched)
            continue

        if not _has_assets_total(captured):
            warnings.append(
                "No labeled 'Total assets' / 'Total current assets' line on "
                "the statement (all total rows unlabeled); page located via "
                "section headings + equity total instead."
            )
        return _build_locate_result(doc, indices, matched, warnings, lenient)

    if fallback is not None:
        indices, matched = fallback
        warnings.append(
            "Total-equity/liabilities line not confirmed on captured "
            "pages; proceeding with the pages found."
        )
        return _build_locate_result(doc, indices, matched, warnings, lenient)
    return None


def _build_locate_result(doc: "fitz.Document", indices: list[int],
                         matched: str, warnings: list[str],
                         lenient: bool) -> dict:
    temp_pdf_path = _export_pages(doc, indices)
    page_numbers = [j + 1 for j in indices]  # 1-based for traceability
    original_text = "\n".join(doc[j].get_text("text") for j in indices)
    logger.info(
        "Balance sheet located on page(s) %s (title: %r, lenient=%s) -> %s",
        page_numbers, matched, lenient, temp_pdf_path,
    )
    return {
        "page_numbers": page_numbers,
        "temp_pdf_path": temp_pdf_path,
        "matched_title": matched,
        "captured_text": original_text,
        "warnings": warnings,
    }


def extract_printed_totals(captured_text: str) -> dict:
    """Read the filing's PRINTED totals straight from the page text (most
    recent = first number after the label), so the reconciliation reference
    never depends on LLM transcription. When there is no explicit "Total
    liabilities" line, it is derived from printed Total liabilities & equity
    minus printed total equity. Returns None per key when not found."""
    def first_number_after(label_re: str):
        m = re.search(label_re + r"[^\d(]{0,40}\(?\$?\s*([\d,]{4,})", captured_text,
                      re.IGNORECASE)
        if not m:
            return None
        return int(m.group(1).replace(",", ""))

    totals = {
        # "total assets" not followed by more words on the label side
        "total_assets": first_number_after(r"total assets"),
        # exclude "Total liabilities and ..." / "Total liabilities, redeemable ..."
        "total_liabilities": first_number_after(r"total liabilities(?!\s*(?:and|&|,))"),
    }
    # Total equity — read once (labeled on most filings, including
    # fully-unlabeled ones like APA that still print "TOTAL EQUITY"). Kept so
    # the pipeline can derive total_liabilities = total_assets - total_equity
    # when no liabilities total is printed at all.
    equity = first_number_after(
        r"total\s+(?:shareholders|stockholders)\W{0,2}\s*equity"
    )
    if equity is None:
        equity = first_number_after(r"total\s+equity")
    totals["total_equity"] = equity

    if totals["total_liabilities"] is None:
        # First try the printed "Total liabilities & equity" - total equity
        # derivation (e.g. NIKE). Without this the LLM's self-computed total
        # becomes the tally target, letting a mis-map grade itself.
        liab_and_equity = first_number_after(
            r"total liabilities\s*(?:and|&|,)[^\n]{0,60}?equity"
        )
        if liab_and_equity is not None and equity is not None and 0 < equity < liab_and_equity:
            totals["total_liabilities"] = liab_and_equity - equity
            logger.info(
                "No printed Total Liabilities line - derived %s = %s (Total "
                "liabilities & equity) - %s (total equity).",
                totals["total_liabilities"], liab_and_equity, equity,
            )
        # Still None (e.g. APA — nothing labeled but TOTAL EQUITY): the
        # pipeline derives it from total_assets - total_equity once
        # total_assets is in place.
    return totals


_UNIT_LABEL_RE = re.compile(r"\bin\s+(millions|thousands|billions)\b", re.IGNORECASE)


def extract_unit_label(text: str):
    """Read the filing's scale wording ("in millions" / "in thousands")
    straight from the captured page text, so unit_label never depends on LLM
    transcription (LlamaParse sometimes drops the "($ in millions)" header
    line from the markdown entirely). Label only — numbers are NEVER scaled
    or converted because of it. Returns None when no scale wording is found."""
    m = _UNIT_LABEL_RE.search(text)
    if not m:
        return None
    return f"in {m.group(1).lower()}"


def _export_pages(doc: "fitz.Document", indices: list[int]) -> str:
    """Export the captured page indices to a small temporary PDF."""
    fd, temp_path = tempfile.mkstemp(prefix="balance_sheet_", suffix=".pdf")
    os.close(fd)
    out = fitz.open()
    try:
        # widgets=False: this temp PDF only feeds LlamaParse (text/layout), and
        # copying form widgets recurses through their parent trees — deeply
        # nested AcroForms (e.g. BMW annual reports) overflow MuPDF's stack.
        out.insert_pdf(doc, from_page=indices[0], to_page=indices[-1],
                       widgets=False)
        out.save(temp_path)
    finally:
        out.close()
    return temp_path

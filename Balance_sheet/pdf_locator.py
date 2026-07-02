"""Stage 1 — locate the balance-sheet page(s) in a 10-Q/10-K PDF with PyMuPDF.

Finds the page whose text contains a balance-sheet title variant, captures it
plus the following page (balance sheets often span two pages), confirms both
"Total assets" and a total-equity/liabilities line are covered (extending by
one page if not), and exports the captured pages to a small temporary PDF for
LlamaParse.
"""

import logging
import os
import re
import tempfile

import fitz  # PyMuPDF

from .config import TITLE_VARIANTS

logger = logging.getLogger("balance_sheet.pdf_locator")

_TOTAL_ASSETS_MARKER = "total assets"
# Any of these confirms the liabilities/equity side of the statement was captured.
_EQUITY_TOTAL_MARKERS = [
    "total liabilities and shareholders",
    "total liabilities and stockholders",
    "total liabilities and equity",
    "total liabilities, redeemable",
    "total equity",
]


def _page_text(doc: "fitz.Document", index: int) -> str:
    return doc[index].get_text("text").lower()


def _has_equity_total(text: str) -> bool:
    return any(marker in text for marker in _EQUITY_TOTAL_MARKERS)


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
        n_pages = len(doc)
        for i in range(n_pages):
            text = _page_text(doc, i)
            matched = next((v for v in variants if v in text), None)
            if not matched:
                continue

            # Capture the matched page + the next page (2-page statements).
            indices = [i] + ([i + 1] if i + 1 < n_pages else [])
            captured = "\n".join(_page_text(doc, j) for j in indices)

            if _TOTAL_ASSETS_MARKER not in captured:
                # Title without "Total assets" nearby — likely a table of
                # contents or a cross-reference; keep scanning.
                continue

            # If only "Total assets" is present, extend by one more page to
            # pick up the liabilities/equity side.
            if not _has_equity_total(captured) and indices[-1] + 1 < n_pages:
                indices.append(indices[-1] + 1)
                captured = "\n".join(_page_text(doc, j) for j in indices)

            if not _has_equity_total(captured):
                warnings.append(
                    "Total-equity/liabilities line not confirmed on captured "
                    "pages; proceeding with the pages found."
                )

            temp_pdf_path = _export_pages(doc, indices)
            page_numbers = [j + 1 for j in indices]  # 1-based for traceability
            original_text = "\n".join(doc[j].get_text("text") for j in indices)
            logger.info(
                "Balance sheet located on page(s) %s (title: %r) -> %s",
                page_numbers, matched, temp_pdf_path,
            )
            return {
                "page_numbers": page_numbers,
                "temp_pdf_path": temp_pdf_path,
                "matched_title": matched,
                "captured_text": original_text,
                "warnings": warnings,
            }

    raise RuntimeError(
        "No balance-sheet page found - none of the title variants matched a "
        "page that also contains 'Total assets'."
    )


def extract_printed_totals(captured_text: str) -> dict:
    """Read the filing's PRINTED totals straight from the page text (most
    recent = first number after the label), so the reconciliation reference
    never depends on LLM transcription. Returns None per key when the label
    isn't found — e.g. filings with no explicit "Total liabilities" line."""
    def first_number_after(label_re: str):
        m = re.search(label_re + r"[^\d(]{0,40}\(?\$?\s*([\d,]{4,})", captured_text,
                      re.IGNORECASE)
        if not m:
            return None
        return int(m.group(1).replace(",", ""))

    return {
        # "total assets" not followed by more words on the label side
        "total_assets": first_number_after(r"total assets"),
        # exclude "Total liabilities and ..." / "Total liabilities, redeemable ..."
        "total_liabilities": first_number_after(r"total liabilities(?!\s*(?:and|&|,))"),
    }


def _export_pages(doc: "fitz.Document", indices: list[int]) -> str:
    """Export the captured page indices to a small temporary PDF."""
    fd, temp_path = tempfile.mkstemp(prefix="balance_sheet_", suffix=".pdf")
    os.close(fd)
    out = fitz.open()
    try:
        out.insert_pdf(doc, from_page=indices[0], to_page=indices[-1])
        out.save(temp_path)
    finally:
        out.close()
    return temp_path

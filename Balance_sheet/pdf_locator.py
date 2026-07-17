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
import unicodedata

import fitz  # PyMuPDF

from .config import TITLE_VARIANTS

logger = logging.getLogger("balance_sheet.pdf_locator")


def _fold(text: str) -> str:
    """Lowercase + fold accents and ligatures (é→e, ﬁ→fi) + normalize
    typographic apostrophes and non-breaking spaces. French ESEF filings
    ("Total de l'actif", "Actifs ﬁnanciers") then match markers the same way
    English pages do; pure-ASCII English text is unchanged."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return (text.replace("’", "'").replace(" ", " ")
                .replace(" ", " ").lower())


# Any of these confirms the assets side of the statement was captured.
# "total current assets" covers filers (e.g. Conagra) whose assets-total row
# is unlabeled; the French markers cover official-language ESEF filings
# (Eiffage prints "Total actif non courant" / "Total de l'actif").
_ASSETS_TOTAL_MARKERS = [
    "total assets",
    "total current assets",
    "total de l'actif",
    "total actif",
]
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
    # French: "Total des capitaux propres" (equity) / "Capitaux propres et
    # passifs" (the L&E side title and its total row).
    "total des capitaux propres",
    "total capitaux propres",
    "capitaux propres et passif",
]


def _page_text(doc: "fitz.Document", index: int) -> str:
    return _fold(doc[index].get_text("text"))


def _has_equity_total(text: str) -> bool:
    return any(marker in text for marker in _EQUITY_TOTAL_MARKERS)


def _has_assets_total(text: str) -> bool:
    return any(marker in text for marker in _ASSETS_TOTAL_MARKERS)


# Titles of the OTHER primary statements. Two or more on one page mark a
# financial-statements TOC/index (TGS's lists every statement with page
# references) — a real balance-sheet page never carries them.
_OTHER_STATEMENT_TITLES = [
    "statement of cash flows",
    "statements of cash flows",
    "statement of changes in equity",
    "statements of changes in equity",
    # French (after _fold): "Tableau des flux de trésorerie" / "Variation
    # des capitaux propres".
    "flux de tresorerie",
    "variation des capitaux propres",
]


def _looks_like_statements_toc(text: str) -> bool:
    return sum(1 for t in _OTHER_STATEMENT_TITLES if t in text) >= 2


# Fiscal-year tokens, bare or FY-prefixed ("FY2015" has no \b before the
# digits, so a plain \b20\d{2}\b regex never sees it).
_YEAR_TOKEN_RE = re.compile(r"\b(?:fy\s?)?(20\d{2})\b")


_CHANGE_SUMMARY_RE = re.compile(
    r"%\s*change|change\s*%|%\s*chg|\bchange\b\s*\(?%\)?|"
    r"increase\s*/?\s*\(?decrease\)?\s*%", re.IGNORECASE)


def _looks_like_change_summary(text: str) -> bool:
    """Management-report balance-sheet SUMMARY tables print a period-over-period
    "% change" / "Change %" column (e.g. a "condensed statement of financial
    position" recap with EUR-million figures and a percentage-change column) —
    the real statement never carries one. Skipping these keeps a management
    summary from shadowing the actual statement further into the document."""
    return _CHANGE_SUMMARY_RE.search(text) is not None


def _looks_like_multiyear_summary(text: str) -> bool:
    """Multi-year financial summary tables (MUFG's "Ten-Year Summary of Major
    Financial Data" prints a "Balance sheet data:" section under columns
    FY2015..FY2024) match a balance-sheet title + "Total assets" exactly like
    the real statement does — but they are a decade of selected data, not the
    statement. A real balance sheet compares 2-3 period columns; five or more
    CONSECUTIVE fiscal years on one page mark a summary table."""
    years = sorted({int(m.group(1)) for m in _YEAR_TOKEN_RE.finditer(text)})
    best = run = 1 if years else 0
    for prev, cur in zip(years, years[1:]):
        run = run + 1 if cur == prev + 1 else 1
        best = max(best, run)
    return best >= 5


def _title_match(text: str, variants: list[str]) -> str | None:
    """First variant with at least one REAL title occurrence — i.e. not
    summary-table or prose wording. An occurrence does NOT count when it is
      - followed by "data"/"date": "Balance sheet data:" heads selected-data
        tables (MUFG's 20-F p94 / integrated report), "as of the balance
        sheet date" is note prose;
      - preceded by "average": "Average Balance Sheets, Interest and Average
        Rates" is the Guide-3 statistical section, whose average balances
        must never be read as the statement;
      - preceded by "off"/"off-": "off-balance sheet arrangements".
    """
    for v in variants:
        for m in re.finditer(re.escape(v), text):
            tail = text[m.end():m.end() + 10].lstrip(" \n:—-*")
            if tail.startswith(("data", "date")):
                continue
            head = text[max(0, m.start() - 12):m.start()].rstrip(" \n-")
            if head.endswith(("average", "off")):
                continue
            return v
    return None


def _has_statement_structure(text: str) -> bool:
    """Lenient confirmation for filers (e.g. APA) that print EVERY total row
    unlabeled — no "Total assets" / "Total current assets" text exists on the
    statement at all. The section headings plus a labeled equity total still
    identify the page as the balance sheet (a TOC or prose cross-reference
    never carries all three)."""
    en = "current assets" in text and "current liabilities" in text
    fr = "actif courant" in text and "passif courant" in text
    return (en or fr) and _has_equity_total(text)


def locate_balance_sheet(pdf_path: str) -> dict:
    """Find the balance-sheet page(s).

    Returns {"page_numbers": [1-based...], "temp_pdf_path": str,
             "matched_title": str, "warnings": [...]}.
    Raises RuntimeError if no balance sheet can be located.
    """
    if not os.path.isfile(pdf_path):
        raise RuntimeError(f"PDF not found: {pdf_path}")

    variants = [_fold(v) for v in TITLE_VARIANTS]
    # Variant priority: a SPECIFIC statement title ("Consolidated Balance
    # Sheets", "... Statement(s) of Financial Position", the French titles)
    # anywhere in the document outranks the bare "BALANCE SHEET" variant,
    # which a large filing matches on dozens of prose/summary pages (MUFG's
    # 512pp 20-F: "Balance sheet data:", "off-balance sheet", ... on ~50
    # pages, shadowing the real "Consolidated Balance Sheets" on p300).
    # The specific pass takes OUTRIGHT accepts only (no last-resort
    # candidates — an assets-only specific match must not shadow a full
    # bare-titled statement); the all-variants pass that follows is EXACTLY
    # the previous behavior, so bare-title-only filers (BMW) are unchanged.
    specific = [v for v in variants if v != "balance sheet"]
    warnings: list[str] = []

    with fitz.open(pdf_path) as doc:
        for lenient in (False, True):
            # lenient=True re-scans accepting the statement's structure
            # (section headings + equity total) for filers (e.g. APA) that
            # print every total row unlabeled.
            result = _scan(doc, specific, warnings, lenient,
                           last_resort=False)
            if result is None:
                result = _scan(doc, variants, warnings, lenient)
            if result is not None:
                return result

    raise RuntimeError(
        "No balance-sheet page found - none of the title variants matched a "
        "page that also contains 'Total assets' (or 'Total current assets')."
    )


def _scan(doc: "fitz.Document", variants: list[str], warnings: list[str],
          lenient: bool, last_resort: bool = True):
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
    shadow the real statement further into the document.

    Likewise, a window confirmed ONLY by structure (no labeled assets total)
    that also LOOKS like a financial-statements TOC is deferred, not
    returned: a TOC can carry all three structure signals purely in its note
    titles (TGS lists "Consolidated balance sheet — Equity and liabilities"
    and "Note 21 Current liabilities and other current assets") — a later
    page with a labeled "Total assets" is the real statement and must win.
    A structure-only window that does NOT look like a TOC is accepted
    outright, exactly as before: bare-TOTAL filers (L'Oréal, APA) print the
    real statement that way, and deferring it would let a later labeled
    page (e.g. the PARENT-company statement 67 pages on in L'Oréal's URD)
    shadow the consolidated one."""
    confirm = _has_statement_structure if lenient else _has_assets_total

    def ok(text: str) -> bool:
        return confirm(text) or (not lenient
                                 and _has_statement_structure(text))

    fallback = None
    structure_candidate = None
    n_pages = len(doc)
    for i in range(n_pages):
        text = _page_text(doc, i)
        matched = _title_match(text, variants)
        if not matched:
            continue

        # Multi-year summary tables ("Ten-Year Summary ... Balance sheet
        # data:") are never the statement — skip before they can be accepted
        # or remembered as the assets-only fallback.
        if _looks_like_multiyear_summary(text):
            continue

        # Management-report summary recaps with a "% change" column (e.g. a
        # "condensed statement of financial position" table earlier in the
        # document) are never the statement — skip so they can't shadow it.
        if _looks_like_change_summary(text):
            continue

        # Running-header/TOC pages: French URDs repeat the section title
        # ("Comptes consolidés") on EVERY page including the chapter TOC just
        # before the statement. If this page carries no assets-side signal of
        # its own and the NEXT page matches a title too, anchor there instead
        # — otherwise the TOC page is captured and feeds the LLM noise.
        if (not _has_assets_total(text) and i + 1 < n_pages
                and _title_match(_page_text(doc, i + 1), variants)):
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
            if _looks_like_statements_toc(captured):
                # Structure-only confirmation on a TOC-looking page — defer
                # and keep scanning (see docstring); it wins only if no
                # labeled assets-total window exists anywhere.
                if structure_candidate is None:
                    structure_candidate = (list(indices), matched)
                continue
            warnings.append(
                "No labeled 'Total assets' / 'Total current assets' line on "
                "the statement (all total rows unlabeled); page located via "
                "section headings + equity total instead."
            )
        return _build_locate_result(doc, indices, matched, warnings, lenient)

    if not last_resort:
        # Priority pass (specific titles only): outright accepts were taken
        # above; last-resort candidates are NOT returned — the follow-up
        # all-variants scan rediscovers them and weighs them against every
        # page, exactly as before the priority pass existed.
        return None
    if structure_candidate is not None:
        indices, matched = structure_candidate
        warnings.append(
            "Only match resembles a financial-statements table of contents "
            "(no labeled 'Total assets' and other statements' titles listed) "
            "and no better page followed; proceeding with it."
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


def _columns_oldest_first(text: str) -> bool:
    """Detect comparative columns printed OLDEST-first (NOS prints
    "31-12-2024 | 31-12-2025" — the reverse of nearly every other filer).

    A year pair separated by WHITESPACE ONLY is the column-header row exactly
    as text extraction emits it ("2025\\n2026") and is trusted first: header
    PROSE can run the opposite way (Kawasaki titles the page "As of March 31,
    2026 and 2025" but prints the columns "2025 | 2026 | 2026 USD", so the
    first-pair rule read the FY2025 column as most recent). When no
    whitespace-only pair exists, the first adjacent, close-together, distinct
    pair (prose like "As at 31 December 2024 and 2025") mirrors the column
    order as before. Defaults to False (most-recent-first) when no pair
    qualifies."""
    years = [(m.start(), m.end(), int(m.group(0)))
             for m in re.finditer(r"\b20\d{2}\b", text)]

    def _first_pair(whitespace_only: bool):
        for (s1, e1, y1), (s2, _e2, y2) in zip(years, years[1:]):
            if y1 == y2 or abs(y1 - y2) > 4 or s2 - s1 > 44:
                continue
            if whitespace_only and text[e1:s2].strip():
                continue
            return y1 < y2
        return None

    result = _first_pair(whitespace_only=True)
    if result is None:
        result = _first_pair(whitespace_only=False)
    return bool(result)


def extract_printed_totals(captured_text: str) -> dict:
    """Read the filing's PRINTED totals straight from the page text (most
    recent = first number after the label — or the SECOND when the columns
    are printed oldest-first, see _columns_oldest_first), so the
    reconciliation reference never depends on LLM transcription. When there
    is no explicit "Total liabilities" line, it is derived from printed Total
    liabilities & equity minus printed total equity. French labels ("Total de
    l'actif", "Total des capitaux propres [et passifs]") are tried after the
    English ones — French filings never contain the English labels, so the
    extra attempts are no-ops on English statements. Returns None per key
    when not found."""
    text = _fold(captured_text)
    oldest_first = _columns_oldest_first(text)

    # French filings group digits with spaces ("37 825"); English with commas.
    _NUM = r"((?:\d{1,3}(?: \d{3})+)|[\d,]{4,})"
    _GAP = r"[^\d(]{0,40}\(?\$?\s*"

    def first_number_after(label_re: str):
        pattern = label_re + _GAP + _NUM
        if oldest_first:
            # Oldest-first columns: the most-recent value is the SECOND
            # number after the label (when a second one follows closely).
            pattern += r"(?:" + _GAP + _NUM + r")?"
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            return None
        group = m.group(1)
        if oldest_first and m.lastindex and m.lastindex >= 2 and m.group(2):
            group = m.group(2)
        return int(group.replace(",", "").replace(" ", ""))

    totals = {
        # "total assets" not followed by more words on the label side
        "total_assets": first_number_after(r"total assets"),
        # exclude "Total liabilities and ..." / "Total liabilities, redeemable ..."
        "total_liabilities": first_number_after(r"total liabilities(?!\s*(?:and|&|,))"),
    }
    if totals["total_assets"] is None:
        # French: "Total de l'actif" (Eiffage) / "Total actif" — but never the
        # section subtotals "Total actif (non) courant" or the notes' "Sous-
        # total actifs de contrats".
        totals["total_assets"] = (
            first_number_after(r"total de l'actif")
            or first_number_after(
                r"total (?:des )?actifs?(?!\s+(?:non\s+)?courants?)(?!\s+de\s)")
        )
    if totals["total_liabilities"] is None:
        # French: "Total (du) passif" alone — exclude the section subtotals
        # and the L&E line "Total passif et capitaux propres".
        totals["total_liabilities"] = first_number_after(
            r"total (?:du\s+|des\s+)?passifs?(?!\s+(?:non\s+)?courants?)(?!\s+et)"
        )
    # Total equity — read once (labeled on most filings, including
    # fully-unlabeled ones like APA that still print "TOTAL EQUITY"). Kept so
    # the pipeline can derive total_liabilities = total_assets - total_equity
    # when no liabilities total is printed at all.
    equity = first_number_after(
        r"total\s+(?:shareholders|stockholders)\W{0,2}\s*equity"
    )
    if equity is None:
        equity = first_number_after(r"total\s+equity")
    if equity is None:
        # French: "Total des capitaux propres" — NOT the L&E line "Total des
        # capitaux propres et passifs".
        equity = first_number_after(
            r"total (?:des\s+)?capitaux propres(?!\s+et)")
    totals["total_equity"] = equity

    if totals["total_liabilities"] is None:
        # First try the printed "Total liabilities & equity" - total equity
        # derivation (e.g. NIKE). Without this the LLM's self-computed total
        # becomes the tally target, letting a mis-map grade itself.
        liab_and_equity = first_number_after(
            r"total liabilities\s*(?:and|&|,)[^\n]{0,60}?equity"
        )
        if liab_and_equity is None:
            # French L&E wording, both orders (Eiffage prints "Total des
            # capitaux propres et passifs").
            liab_and_equity = (
                first_number_after(
                    r"total (?:des\s+)?capitaux propres et (?:des\s+)?passifs?")
                or first_number_after(
                    r"total (?:du\s+)?passifs? et (?:des\s+)?capitaux propres")
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
# French scale wording ("En millions d'euros"), translated to the English label.
_UNIT_LABEL_FR_RE = re.compile(r"\ben\s+(millions|milliers|milliards)\b", re.IGNORECASE)
_FR_UNIT_WORDS = {"millions": "millions", "milliers": "thousands",
                  "milliards": "billions"}


def extract_unit_label(text: str):
    """Read the filing's scale wording ("in millions" / "in thousands" /
    "En millions d'euros") straight from the captured page text, so unit_label
    never depends on LLM transcription (LlamaParse sometimes drops the
    "($ in millions)" header line from the markdown entirely). Label only —
    numbers are NEVER scaled or converted because of it. Returns None when no
    scale wording is found."""
    m = _UNIT_LABEL_RE.search(text)
    if m:
        return f"in {m.group(1).lower()}"
    m = _UNIT_LABEL_FR_RE.search(text)
    if m:
        return f"in {_FR_UNIT_WORDS[m.group(1).lower()]}"
    return None


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

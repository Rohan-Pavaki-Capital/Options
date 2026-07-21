"""France-specific code guard (region="fr") — the "Total passif" trap.

French filers translate "Total passif" as plain "Total liabilities" and print
it as the GRAND total of the equity-and-liabilities side, EQUAL to the printed
"Total assets" (e.g. Bolloré: "Total liabilities 25,767" = "Total assets
25,767", with Shareholders' equity 24,427 above it). Copying that row into
filing_totals.total_liabilities makes the liabilities tally target include
equity, so the side under-counts by exactly the equity total and the
correction loop chases a printed line that does not exist.

This guard runs IN CODE after the printed-totals override. Signature: printed
total_liabilities EQUALS printed total_assets. Fix: re-derive
total_liabilities = total_assets − printed equity, where equity comes ONLY
from printed values — the code-read "Total equity" row, the statement's own
bare "Shareholders' equity" / "Capitaux propres" row read from the Stage-1
page text (French filers print the grand equity row WITHOUT the word
"Total"), an [EQUITY]-tagged checklist line, or the sum of the
[EQUITY]-tagged lines. A candidate is accepted ONLY when an INDEPENDENT
printed reference agrees within the rounding tolerance: the sum of the
[LIABILITY]-tagged checklist lines, or the printed "Non-current liabilities"
+ "Current liabilities" subtotal rows read from the page text (the checklist
can miss the whole right-hand table on two-table slide layouts — Bolloré's
results deck — so the page text is a first-class source, not a fallback).
No number is ever fabricated: if no candidate validates, the totals are left
untouched with a warning.
"""

import logging
import re
import unicodedata

from ..config import tally_tolerance
from ..tally.tally import _normalize_number_text, _parse_item_label

logger = logging.getLogger("balance_sheet.france")


def _fmt(v: float) -> str:
    """Thousands-separated display, no trailing .0 on whole numbers."""
    return f"{int(v):,}" if v == int(v) else f"{v:,}"


# ── Page-text row reading (Stage-1 captured text, English + French) ────────
# The statement's grand equity row and the liabilities-section subtotal rows,
# read straight from the page. Label matching is per printed LINE (folded,
# accent-stripped): "current liabilities" can never match inside
# "Non-current liabilities" or "Other current liabilities", because the line
# must START with the label and carry nothing but a value after it.

_EQUITY_ROW_LABELS = (
    "shareholders' equity", "shareholders equity",
    "stockholders' equity", "stockholders equity",
    "total equity", "capitaux propres",
)
# Sub-rows of the equity section that begin with the same words as the grand
# row — never the value we want.
_EQUITY_ROW_EXCLUDE = (
    "group share", "part du groupe", "attributable", "et passif",
)
_NC_LIAB_ROW_LABELS = (
    "non-current liabilities", "total non-current liabilities",
    "passifs non courants", "passif non courant",
    "total passif non courant", "total des passifs non courants",
)
_CUR_LIAB_ROW_LABELS = (
    "current liabilities", "total current liabilities",
    "passifs courants", "passif courant",
    "total passif courant", "total des passifs courants",
)

_NUM_TOKEN_RE = re.compile(r"-?\(?\d[\d ,.  ]*\)?")


def _fold(text: str) -> str:
    """Accent-fold + lowercase + normalize apostrophes/NBSP (same treatment
    the line-items extractor gives section headers)."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return (text.replace("’", "'").replace(" ", " ")
                .replace(" ", " ").lower())


def _parse_page_number(token: str):
    """Parse one printed number token (US "25,767", French "25 767,4",
    parenthesized negatives). None when it does not parse."""
    token = token.strip()
    negative = token.startswith("(") and token.endswith(")")
    cleaned = _normalize_number_text(token.strip("()"))
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


# Note-reference tokens ("6.1", "9.1", "5.8.1" — a Notes column sits between
# the label and the values on many statements): short DOT-separated numbers.
# French values use COMMA decimals ("24 427,1") so they never look like this.
_NOTE_REF_RE = re.compile(r"\d{1,2}(?:\.\d{1,2}){1,2}$")


def _row_values(lines: list, labels: tuple, exclude: tuple = ()) -> list:
    """The first FEW printed numbers of the first row matching one of
    `labels`: the line must START with the label; values follow on the same
    line or on the next lines (two-column PDF text prints "label\\nvalue\\n
    prior-year value"). Note-reference tokens are skipped. Up to 3 values are
    returned (most-recent column first on most filings) — the CALLER
    cross-validates each against an independent printed reference, so a
    prior-year or stray pick can never be used blindly."""
    for idx, line in enumerate(lines):
        for label in labels:
            if not line.startswith(label):
                continue
            if any(term in line for term in exclude):
                continue
            values = []
            rest = line[len(label):].strip(" :.-")
            candidates = [rest] if rest else []
            candidates += lines[idx + 1:idx + 4]
            for chunk in candidates:
                match = _NUM_TOKEN_RE.match(chunk)
                if not match:
                    if values or chunk is rest:
                        break  # values ended / label followed by prose
                    continue
                token = match.group(0).strip()
                if _NOTE_REF_RE.fullmatch(token.strip("()")):
                    continue  # Notes-column reference, not a value
                value = _parse_page_number(token)
                if value is not None:
                    values.append(value)
                if len(values) >= 3:
                    break
            if values:
                return values
    return []


def _page_text_signals(page_text: str):
    """(equity-row candidate values, liabilities-subtotal reference sums)
    read from the Stage-1 page text — each a list, empty when the rows are
    not printed/found. The reference sums pair the liabilities subtotals by
    column index (most-recent + most-recent, prior + prior)."""
    if not page_text:
        return [], []
    lines = [_fold(l).strip() for l in page_text.splitlines()]
    lines = [l for l in lines if l]
    equity = _row_values(lines, _EQUITY_ROW_LABELS, _EQUITY_ROW_EXCLUDE)
    non_current = _row_values(lines, _NC_LIAB_ROW_LABELS)
    current = _row_values(lines, _CUR_LIAB_ROW_LABELS)
    liabilities = [nc + cur for nc, cur in zip(non_current, current)]
    return equity, liabilities


def _tagged_values(line_items, side_tag: str) -> list:
    """Values of the checklist lines tagged with the given filing side."""
    values = []
    for label, value in line_items or []:
        tag, _core, _cnc = _parse_item_label(label)
        if tag == side_tag and value:
            values.append(float(value))
    return values


def _equity_candidates(printed_equity, page_equity: list,
                       equity_values: list) -> list:
    """Printed-equity candidates, most-authoritative first: the code-read
    "Total equity" row, the statement's own bare equity row values read from
    the page text (most-recent column first), then each [EQUITY]-tagged line
    value in REVERSE print order (the grand equity row is printed last in
    the section), then the sum of all [EQUITY]-tagged lines (covers a grand
    row collapsed out of the checklist as a parent subtotal). Every candidate
    is a printed value or a sum of printed values — never an invented
    number; the caller validates each against an independent reference."""
    candidates = []
    for v in [printed_equity] + list(page_equity):
        if isinstance(v, (int, float)) and v:
            candidates.append(float(v))
    candidates.extend(reversed(equity_values))
    if equity_values:
        candidates.append(sum(equity_values))
    seen, ordered = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def fix_grand_total_liabilities(result: dict, line_items: list | None = None,
                                printed_equity=None,
                                page_text: str | None = None) -> dict:
    """Detect the "Total passif" signature and fix filing_totals in place.

    Runs after coercion + the printed-totals override, so both totals are
    numbers. Does nothing unless printed total_liabilities == total_assets
    (a true liabilities-only total is always smaller). `printed_equity` is
    the code-read printed "Total equity" (pdf_locator.extract_printed_totals),
    None when the page prints no such labeled row; `page_text` is the Stage-1
    captured page text (equity row + liabilities subtotals are read from it)."""
    totals = result.get("filing_totals") or {}
    ta, tl = totals.get("total_assets"), totals.get("total_liabilities")
    if not isinstance(ta, (int, float)) or not isinstance(tl, (int, float)):
        return result
    if not ta or tl != ta:
        return result  # no signature — nothing to do
    warnings = result.setdefault("warnings", [])

    liability_values = _tagged_values(line_items, "LIABILITY")
    equity_values = _tagged_values(line_items, "EQUITY")
    page_equity, page_liabilities = _page_text_signals(page_text)

    # Independent printed references the derived total must agree with:
    # (label, value, mapped-line count for the rounding tolerance).
    references = []
    if len(liability_values) >= 2:
        references.append(("the [LIABILITY]-tagged printed lines",
                           sum(liability_values), len(liability_values)))
    for ref in page_liabilities:
        references.append(
            ("the printed 'Non-current liabilities' + 'Current liabilities' "
             "subtotal rows", ref, 2))

    if references:
        for equity in _equity_candidates(printed_equity, page_equity,
                                         equity_values):
            if not 0 < equity < ta:
                continue
            derived = round(ta - equity, 3)  # kill float artifacts (25767 - 24427.1)
            for ref_label, ref_value, ref_lines in references:
                if abs(derived - ref_value) > tally_tolerance(ref_lines,
                                                              derived):
                    continue
                totals["total_liabilities"] = derived
                warnings.append(
                    f"France guard: printed 'Total liabilities' {_fmt(tl)} "
                    f"equals printed 'Total assets' - the French 'Total "
                    f"passif' grand total (equity + liabilities), not a "
                    f"liabilities-only total. total_liabilities re-derived "
                    f"as {_fmt(derived)} = total assets {_fmt(ta)} - printed "
                    f"equity {_fmt(equity)}; {ref_label} independently sum "
                    f"to {_fmt(ref_value)}."
                )
                logger.info(
                    "France guard: total_liabilities %s == total_assets "
                    "(grand total); re-derived %s = %s - equity %s "
                    "(validated by %s = %s).",
                    _fmt(tl), _fmt(derived), _fmt(ta), _fmt(equity),
                    ref_label, _fmt(ref_value),
                )
                return result

    warnings.append(
        f"France guard: printed 'Total liabilities' {_fmt(tl)} equals "
        f"printed 'Total assets' (the French 'Total passif' grand total), "
        f"but no printed equity value confirmed the liabilities-only total "
        f"- filing_totals.total_liabilities left as printed."
    )
    logger.warning(
        "France guard: grand-total signature detected but no printed equity "
        "candidate validated against the liability lines - totals unchanged."
    )
    return result

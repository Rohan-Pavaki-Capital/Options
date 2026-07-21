"""Stage 3 helper — extract the code-side line-item checklist from the
balance-sheet markdown.

Pulls (label, most-recent-column value) pairs from the parsed markdown table,
tags each with its filing section (ASSET/LIABILITY/EQUITY + current/non-current),
realigns the label/value pairing against the Stage-1 page text when LlamaParse
shifts a value column, and collapses Japanese-GAAP parent/component hierarchies.
Giving the LLM this checklist keeps it from re-reading numbers off the table.
"""

import logging
import re
import unicodedata

logger = logging.getLogger("balance_sheet.line_items")


_CELL_NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?")
# French number format: spaces as digit-group separators, comma as the decimal
# mark ("2 099", "37 825", "1 013,5"). Tried only when the US format misses.
_CELL_NUM_FR_RE = re.compile(r"-?\d{1,3}(?: \d{3})+(?:,\d+)?|-?\d+,\d+")

# Placeholder a filing prints for a zero/nil value in a column.
_DASH_CELLS = {"—", "–", "-", "--"}

# Year token inside a table cell — a row with two or more year-bearing cells
# is the date header ("| | 31-12-2024 | 31-12-2025 |") that names the value
# columns and their order.
_YEAR_CELL_RE = re.compile(r"\b20\d{2}\b")


def _parse_cell(cell: str):
    """Parse one markdown table cell: a float/int value, 0 for a printed
    dash, or None when the cell is not a value (label text, note refs like
    "3, 9, 10", empty)."""
    raw = (cell.strip("* ").replace("$", "").replace("€", "")
           .replace(" ", " ").replace(" ", " ").strip())
    if raw in _DASH_CELLS:
        return 0
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()").strip()
    value = None
    if _CELL_NUM_RE.fullmatch(raw):
        value = float(raw.replace(",", ""))
    elif _CELL_NUM_FR_RE.fullmatch(raw):
        # French format: strip the space group separators, comma
        # becomes the decimal point ("2 099" -> 2099, "1 013,5").
        value = float(raw.replace(" ", "").replace(",", "."))
    if value is None:
        return None
    if negative:
        value = -value
    return int(value) if value == int(value) else value


def _fold_label(text: str) -> str:
    """Accent-folded lowercase for section/keyword matching only — the label
    shown to the LLM keeps its original spelling. Mirrors pdf_locator._fold."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.replace("’", "'").lower()


# Section keywords for tagging line items with the side they sit under.
# Order matters: "LIABILITIES AND EQUITY" / "Capitaux propres et passifs"
# headers must tag as LIABILITY (liability terms first), and the equity terms
# must run before the asset terms.
_SECTION_TERMS = (("liabilit", "LIABILITY"), ("passif", "LIABILITY"),
                  ("equity", "EQUITY"), ("capitaux propres", "EQUITY"),
                  # J-GAAP titles the equity section "Net assets:" — must be
                  # checked before the bare "asset" term tags it ASSET.
                  ("net assets", "EQUITY"),
                  ("asset", "ASSET"), ("actif", "ASSET"))


def _update_section(text: str, section: str, cnc: str) -> tuple[str, str]:
    """Advance the (side, current/non-current) state from a header line.
    Loose, case-insensitive, accent-folded substring matching (English and
    French section wording). A side change resets the current/non-current
    state so a stale tag never leaks across statements; when no header
    decides it, cnc stays "" (unknown) — never force a tag."""
    lowered = _fold_label(text)
    new_section = section
    for term, tag in _SECTION_TERMS:
        if term in lowered:
            new_section = tag
            break
    if new_section != section:
        cnc = ""
    # "non-current ..." headers contain "current asset(s)"/"current liabilit..."
    # as substrings, so the non-current check must run first. French: "ACTIF
    # NON COURANT" / "PASSIF NON COURANT" vs "ACTIF COURANT" / "PASSIF COURANT".
    if ("non-current" in lowered or "noncurrent" in lowered
            or "non current" in lowered or "non courant" in lowered):
        cnc = "non_current"
    elif ("current asset" in lowered or "current liabilit" in lowered
          or "actif courant" in lowered or "actifs courants" in lowered
          or "passif courant" in lowered or "passifs courants" in lowered):
        cnc = "current"
    return new_section, cnc


# Bare section-header labels (folded): never line items, even when the row
# carries a value (LlamaParse shift artifact — see extract_line_items).
_HEADER_ONLY_RE = re.compile(
    r"^(assets|liabilities|equity|net assets|shareholders'? equity|"
    r"liabilities and (?:net assets|equity)|equity and liabilities)\s*:?\s*$")


def extract_line_items(markdown: str,
                       page_text: str | None = None) -> list[tuple[str, float]]:
    """Pull (label, most-recent-column value) pairs from the markdown table
    IN CODE, skipping subtotal/total rows. Giving the LLM this checklist
    means it only classifies labels into buckets — it never re-reads the
    table, so it cannot hallucinate values, use the prior-period column, or
    map a subtotal row. Labels carry the filing section they sit under: the
    side ([ASSET] / [LIABILITY] / [EQUITY]) plus a (current)/(non-current)
    tag read from the filing's own section headers — so a line cannot be
    mapped onto the wrong side, and identically-named lines in different
    sections (IBM prints "Deferred costs" both current and non-current) stay
    separate lines counted once each. Tags appear ONLY in this hint text,
    never in the output JSON. When no current/non-current header exists
    (unclassified REIT/bank sheets) the tag is omitted and the LLM falls
    back to its own current-vs-non-current judgement rule."""
    items = []
    section = ""
    cnc = ""  # "current" / "non_current" / "" (unknown)
    # Value-column layout, learned from the table's date header row: how many
    # dated value columns there are and which of them (0-based, left to
    # right) holds the MOST RECENT period. Until a date header is seen the
    # legacy rule applies (first numeric cell = most-recent column).
    n_value_cols = 0
    recent_pos = 0
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            heading = stripped.lstrip("#").strip()
            if heading:
                section, cnc = _update_section(heading, section, cnc)
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        # Date header row ("| | Notes | 31-12-2024 | 31-12-2025 |"): two or
        # more year-bearing cells name the value columns. Their order decides
        # which value each data row contributes — NOS prints OLDEST-first, so
        # "first numeric cell" would read the prior year (and its Notes
        # column would poison the checklist with note references first).
        year_cells = [_YEAR_CELL_RE.search(c) for c in cells]
        years = [int(m.group(0)) for m in year_cells if m]
        if len(years) >= 2:
            n_value_cols = len(years)
            recent_pos = years.index(max(years))
            continue
        label = cells[0].strip("* ").strip()
        if not label:
            continue
        if _HEADER_ONLY_RE.match(_fold_label(label)):
            # Bare section-header row ("Assets:", "Liabilities:", "Net
            # assets:"). LlamaParse sometimes puts the FIRST data row's value
            # on this row (the start of a one-row value shift, MUFG tanshin),
            # so a header must be recognized even when the row carries a
            # value — it is never a line item.
            section, cnc = _update_section(label, section, cnc)
            continue
        if label.lower().startswith("total"):
            # Subtotal/total rows never map into buckets, but they advance
            # the section state: past "Total current assets"/"Total current
            # liabilities", everything until the next statement is non-current.
            lowered = label.lower()
            if lowered.startswith("total current"):
                cnc = "non_current"
            elif lowered.startswith(("total assets", "total liabilities",
                                     "total equity")):
                cnc = ""
            continue
        display = f"[{section}] {label}" if section else label
        if cnc and section != "EQUITY":
            display += " (current)" if cnc == "current" else " (non-current)"
        # Every value-like cell in the row (numbers and printed dashes = 0),
        # left to right. Dashes count so "Notes payable | — | 5" keeps the
        # most-recent 0, not the prior-period 5 (NIKE).
        parsed = [v for cell in cells[1:]
                  if (v := _parse_cell(cell)) is not None]
        if parsed:
            if n_value_cols and len(parsed) >= n_value_cols:
                # The LAST n_value_cols value cells sit under the date
                # header; anything before them (a Notes column of note
                # references, e.g. NOS "Borrowings | 25 | 1,306,276 |
                # 1,357,611") is not a value. Pick the most-recent column.
                value = parsed[len(parsed) - n_value_cols + recent_pos]
            else:
                # No date header seen (or row prints fewer value cells than
                # the header names): legacy rule, first numeric/dash cell.
                value = parsed[0]
            items.append((display, value))
        else:
            # Value-less table row = an in-table section header
            # ("Liabilities:", "Current assets:", "Equity:").
            section, cnc = _update_section(label, section, cnc)
    if page_text:
        # BEFORE the parent collapse — the collapse verifies component sums,
        # which only works on correctly-paired values.
        items = _realign_with_page_text(items, page_text)
    return _collapse_jgaap_parents(items)


_TEXT_NUM_RE = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")


def _parse_text_number(tok: str):
    neg = tok.startswith("(") and tok.endswith(")")
    s = tok.strip("()").lstrip("-").replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if (neg or tok.startswith("-")) else v


def _label_pattern(label: str):
    toks = re.findall(r"[a-z0-9]+", label)
    if not toks:
        return None
    return re.compile(r"(?<![a-z0-9])" + r"\W{0,3}".join(map(re.escape, toks))
                      + r"(?![a-z0-9])")


def _realign_with_page_text(items: list[tuple[str, float]],
                            page_text: str) -> list[tuple[str, float]]:
    """LlamaParse sometimes shifts a table's VALUE column one row against the
    labels (MUFG tanshin: every asset label carried the NEXT row's number —
    "Tangible fixed assets" got Buildings' value). The value SET stays
    complete, so the tally still ties and cannot catch it; only the
    label↔value pairing is scrambled, differently on every parse. The Stage-1
    page TEXT is authoritative (same source as the code-read printed totals):
    there, each label is followed by its own printed numbers. Re-pair each
    extracted label with the value read from the text, walking the text with
    a forward-only cursor so repeated labels ("Lease assets" appears 4x)
    match their own rows, and picking the most-recent column with the same
    oldest-first logic the printed-totals reader uses.

    A row is CONFIRMED when its markdown value appears anywhere among its own
    row's printed numbers in the text (so a correctly-paired multi-column
    filing — Kawasaki's extra USD column, BMW, US filers — validates cleanly
    and is never touched). A row is MISALIGNED when its value appears nowhere
    in its text row; only then is the value replaced with the row's
    most-recent-column number. The rewrite fires only when >=5 rows and
    >=15% of the matched rows are misaligned — a one-off oddity never trips
    it. Every substituted value is a printed number from the page — nothing
    is derived or fabricated."""
    if not page_text or len(items) < 8:
        return items
    norm = re.sub(r"\s+", " ", page_text.lower().replace("’", "'"))
    from ..preprocessing import pdf_locator as _pl
    oldest_first = _pl._columns_oldest_first(page_text)
    pos = 0
    matched = 0
    repairs: dict[int, float] = {}
    spans: dict[int, tuple[int, int]] = {}  # idx -> (label start, numbers end)
    for idx, (display, value) in enumerate(items):
        label = _bare_label(display)
        if len(label) < 4:
            continue
        pat = _label_pattern(label)
        m = pat.search(norm, pos) if pat else None
        if not m:
            continue
        pos = m.end()
        # This row's numbers: numeric tokens before the next letter.
        w = re.match(r"[^a-z]*", norm[m.end():m.end() + 160])
        nums = [n for t in _TEXT_NUM_RE.findall(w.group(0) if w else "")
                if (n := _parse_text_number(t)) is not None]
        if not nums:
            continue
        spans[idx] = (m.start(), m.end() + (w.end() if w else 0))
        matched += 1
        if any(abs(n - (value or 0)) <= 0.6 for n in nums):
            continue  # confirmed — the value belongs to this row
        repairs[idx] = nums[-1] if oldest_first else nums[0]
    if not (len(repairs) >= 5 and matched and len(repairs) / matched >= 0.15):
        return items
    logger.warning(
        "Markdown label/value pairing contradicts the page text on "
        "%d/%d matched lines - realigning those values from the page "
        "text.", len(repairs), matched,
    )
    items = [(d, repairs.get(i, v)) for i, (d, v) in enumerate(items)]

    # DROPPED-ROW RECOVERY (same LlamaParse failure class, so only attempted
    # once the realign gate has fired): the parse also drops rows outright —
    # the tanshin's "Deferred tax assets" row vanished from the markdown,
    # which is exactly where the value shift started. Between two matched
    # items that are ADJACENT in the list, the page text should hold nothing;
    # a complete label+numbers sequence there is a dropped row — re-insert it
    # with its printed most-recent value. Only an exact single-row gap is
    # recovered; anything murkier is left for the tally to flag honestly.
    gap_re = re.compile(
        r"^\s*([a-z][a-z ,'()&./-]{3,80}?)\s*"
        r"((?:\(?-?\d[\d,]*(?:\.\d+)?\)?\s*)+)$")
    inserts = []
    keys = sorted(spans)
    for a, b in zip(keys, keys[1:]):
        if b != a + 1:
            continue  # an unmatched item sits between — gap text is its
        gm = gap_re.match(norm[spans[a][1]:spans[b][0]])
        if not gm or "total" in gm.group(1):
            continue
        nums = [n for t in _TEXT_NUM_RE.findall(gm.group(2))
                if (n := _parse_text_number(t)) is not None]
        if not nums:
            continue
        value = nums[-1] if oldest_first else nums[0]
        # Inherit the neighbour's section/current tags so the recovered row
        # is bucketed on the correct side.
        tag_pre = re.match(r"^\[[A-Z]+\]\s*", items[a][0])
        tag_suf = re.search(r"\s*\((?:non-)?current\)$", items[a][0])
        display = ((tag_pre.group(0) if tag_pre else "")
                   + gm.group(1).strip().capitalize()
                   + (tag_suf.group(0) if tag_suf else ""))
        inserts.append((a + 1, (display, value)))
        logger.warning("Recovered row dropped by the parse: %r = %s",
                       display, value)
    for at, row in reversed(inserts):
        items.insert(at, row)
    return items


_JGAAP_PARENTS = (
    # (bare label, keep_parent): tangible keeps the PARENT (one line → ppe);
    # intangible keeps the COMPONENTS (goodwill must split into its own memo
    # field, the rest → memo.intangibles).
    ("tangible fixed assets", True),
    ("intangible fixed assets", False),
)


def _bare_label(display: str) -> str:
    s = re.sub(r"^\[[A-Z]+\]\s*", "", display)
    s = re.sub(r"\s*\((?:non-)?current\)$", "", s)
    return s.strip().lower()


def _collapse_jgaap_parents(items: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Japanese-GAAP balance sheets print parent totals with component
    sub-lines: "Tangible fixed assets" followed by Buildings/Land/Lease
    assets/..., "Intangible fixed assets" followed by Software/Goodwill/...
    The parents don't say "Total", so the subtotal skip doesn't catch them,
    and the LLM kept counting both parent and components (MUFG tanshin:
    assets over by the whole hierarchy, differently scrambled every run).
    Resolve the hierarchy IN CODE: the components are the consecutive lines
    after the parent whose values SUM to the parent (rounding-aware, so this
    cannot fire on unrelated neighbours); keep exactly one representation per
    _JGAAP_PARENTS. A parent whose followers never sum to it is left alone."""
    out = list(items)
    for parent_label, keep_parent in _JGAAP_PARENTS:
        for i, (disp, val) in enumerate(out):
            if _bare_label(disp) != parent_label or not val:
                continue
            run, total = [], 0.0
            for j in range(i + 1, len(out)):
                total += out[j][1]
                run.append(j)
                tol = max(2, len(run))  # each printed line rounds ±0.5
                if abs(total - val) <= tol:
                    if keep_parent:
                        out = [it for k, it in enumerate(out)
                               if k not in set(run)]
                    else:
                        out = [it for k, it in enumerate(out) if k != i]
                    break
                if total > val + tol or len(run) >= 8:
                    break  # not a summing hierarchy — leave untouched
            break  # at most one hierarchy per parent kind
    return out

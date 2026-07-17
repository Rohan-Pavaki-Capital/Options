"""Stage 3 — standardize the balance-sheet markdown into the fixed schema.

Builds the messages, calls the chat LLM (Together AI, OpenAI-compatible),
strips markdown fences, parses and validates the JSON. If the output is not
valid JSON or is missing required keys, it re-prompts once.

NOTE: LlamaParse only parses PDFs to markdown; the schema-mapping reasoning
happens here, in a chat LLM.
"""

import copy
import json
import logging
import re
import unicodedata

from openai import OpenAI

from . import config
from .config import LLM_BASE_URL, LLM_MODEL, empty_result, require_together_key
from .prompt_eu import SYSTEM_PROMPT_EU
from .prompt_au import SYSTEM_PROMPT_AU

logger = logging.getLogger("balance_sheet.standardizer")

SYSTEM_PROMPT = """Balance-Sheet Standardizer — Goal Loop (run until totals tally)

You are a financial-statement standardizer working like a professional equity analyst.
You are given the markdown of ONE company's balance sheet AND a code-extracted LINE ITEMS
list. Map every line item into the FIXED schema below. Return ONLY a JSON object matching
the schema — no explanation, no markdown fences.

## USE THE EXTRACTED LINE-ITEM LIST ONLY (critical — prevents double-counting)
You will be given a LINE ITEMS list extracted in code from the most-recent column, with all
subtotal/total rows already removed. Map ONLY the lines in that list. Do NOT re-read numbers
from the markdown table, and NEVER map a "Total ..." or subtotal row (e.g. "Total inventory",
"Total current assets"). Each listed line is counted exactly once. If the list is present,
it is the single source of truth for both labels and values.

## HOW TO READ THE SCHEMA FIELD NAMES
The bucket names are canonical labels. Filings will almost never use these exact words — match
each line to the correct bucket BY MEANING (analyst judgement), not string matching. Examples:
- "Property, plant and equipment — net" → ppe
- "Operating right-of-use assets — net" → lease_assets
- "Notes and accounts receivable — trade" → accounts_trade_receivable
- "Prepaid pension assets" → pension_assets
- "Retirement and nonpension postretirement benefit obligations" → pension (liability)

## GOAL / FINISH CONDITION (loop until ALL are true)
1. sum(all asset buckets) + memo.cash_and_marketable_securities + memo.goodwill + memo.intangibles
   == printed "Total assets" (most-recent column), within filing rounding.
2. sum(all liability buckets) + memo.long_term_debt == printed "Total liabilities", within rounding.
3. Every listed line is counted EXACTLY ONCE (one bucket OR one memo field) — none dropped, none double-counted.
4. unit_label identified from the filing header.

ROUNDING TOLERANCE: filings round each line, so lines rarely sum to the printed total exactly.
Accept a small gap (≈ number of lines, or ±0.1%) as rounding. Do NOT chase a rounding-size gap,
and NEVER insert a plug/balancing figure. Re-map only when the gap is materially larger than rounding.

## FIXED SCHEMA — standardized balance sheet
### assets.non_current
- lease_assets, real_estate_assets, investment_assets, investment_in_other,
  assets_held_for_sale, asset_from_discontinued_business, pension_assets, other_assets, ppe
### assets.current
- lease_assets, inventory, accounts_trade_receivable, tax, other_current_assets
### liabilities.non_current
- pension, lease_liabilities, deferred_rev_and_tax, other_liabilities
### liabilities.current
- debt, lease_liabilities, accounts_trade_payable, deferred_rev_and_tax, other_current_liabilities

## MEMO — EXCLUDED FROM THE BALANCE SHEET (separate `memo_excluded` object)
Keep these OUT of every bucket above; they exist only so the totals reconcile:
- cash_and_marketable_securities = cash + cash equivalents + current marketable/short-term
  securities. Restricted cash does NOT go here → other_current_assets.
- goodwill = goodwill only.
- intangibles = intangible assets (net), excluding goodwill.
- long_term_debt = non-current interest-bearing debt. Current portion of debt → current.debt.

## METADATA (fill from the page — these fields NEVER change any number)
- company = the filer's name as printed on the statement/page header ; "" if not printed.
- period = the most-recent column's balance-sheet date in ISO format (e.g. "As of
  March 31, 2026 and 2025" -> "2026-03-31") ; "" if no date is printed.
- currency = ISO 4217 code from the unit wording or currency symbols ("Millions of yen" /
  "¥" -> "JPY", "$" -> "USD", "€" -> "EUR", "£" -> "GBP") ; "" if undeterminable.

## HOUSE CONVENTIONS (firm-specific — follow exactly; these override generic instinct)
1. INVESTMENT_ASSETS. Use investment_assets for holdings explicitly labeled as marketable
   securities / equity or debt investments held long-term, AND for a NON-CURRENT line literally
   titled "Other financial assets" / "Non-current financial assets" / "Financial assets"
   (long-term securities/derivatives held as investments). Do NOT put financing or lending
   receivables there. Specifically:
   - A NON-CURRENT "Other financial assets" / "Financial assets" line (NOT a financing/lending
     receivable, NOT a "... and sundry" line) → investment_assets. A CURRENT "Other financial
     assets" line → other_current_assets.
   - "Long-term financing receivables" → other_assets (NOT investment_assets).
   - "Investments and sundry assets" and any mixed "... and sundry/other assets" line → other_assets.
   - Equity-method stakes in associates / "investment in <named company>" → investment_in_other.
2. NON-CURRENT other_assets is the catch-all for: long-term financing receivables, non-current
   deferred costs, deferred tax ASSETS, and any other non-current line without a specific bucket.
   (Goodwill and intangibles are NOT here — they go to memo.)
3. TAX & DEFERRED-REVENUE LINES:
   - deferred_rev_and_tax (CURRENT) is for TAX-type items ONLY: a current liability named
     "Taxes" / "Income taxes payable" / "Current tax liabilities", plus any current deferred
     income TAX. NOT other_current_liabilities.
   - CURRENT contract liabilities / deferred revenue / "Deferred income" / unearned revenue →
     other_current_liabilities (NOT deferred_rev_and_tax). [Reviewer convention: current
     contract/deferred-revenue liabilities sit in other_current_liabilities, leaving
     deferred_rev_and_tax (current) for tax-type items only.]
   - A CURRENT asset "income taxes receivable" → tax (current asset bucket).
   - Non-current deferred income taxes (liability) → deferred_rev_and_tax (non-current).
4. CONTRACT ASSETS (current or non-current) → always accounts_trade_receivable. Treat them
   as trade-type receivables regardless of how the filing labels them (e.g. "Contract assets",
   "Unbilled receivables", "Costs and estimated earnings in excess of billings").
5. LONG-TERM DEBT IS MEMO-ONLY. Non-current interest-bearing debt (bonds, notes, term loans,
   long-term borrowings) goes to memo.long_term_debt and NOWHERE ELSE. Never also place it in
   other_liabilities or any non-current bucket. Counting it in both breaks the liability tally.

## CORE RULES
- Use ONLY the most-recent period column. Column order varies: most filings print the most
  recent period FIRST, but some print the OLDEST first (header "31-12-2024 | 31-12-2025") —
  the most-recent column is the one under the LATEST date, wherever it sits. A "Notes" column
  of note references may sit between the label and the values; note references are NEVER
  values. Copy numbers exactly (strip only "$" and commas);
  negatives stay negative. Never invent a number — every value is a printed line value, or the
  arithmetic SUM of printed line values when several lines share one bucket. When several lines
  share a bucket, output the computed SUM as a single JSON number — never an expression string.
  E.g. PP&E lines 46139, 445, 532, -34292 → "ppe": 12824 (NOT "46139 + 445 + 532 + -34292").
- Equity is NOT mapped anywhere (common stock, paid-in capital, retained earnings, treasury
  stock, AOCI, noncontrolling interests) — leave it out entirely.

## CURRENT vs NON-CURRENT
Respect the filing's own "Current" section headers — lines under them → a current bucket (or
the cash memo); everything else → non_current. If unclassified (REITs/banks), use judgement:
cash/receivables/inventory/short-term → current; property/long-term investments/intangibles → non-current.

## BUCKET PLACEMENT (match by meaning)
- Operating PP&E (net) → ppe. Real estate that IS the business (REITs) → real_estate_assets.
- Right-of-use lease assets → lease_assets (current if the filing lists it current).
- Trade receivables, notes receivable, current financing receivables held for investment,
  other trade-type receivables, contract assets → accounts_trade_receivable. Inventory → inventory.
- Deferred costs (current), prepaid expenses, restricted cash,
  other misc current assets → other_current_assets.
- HELD FOR SALE — respect the filing's section header: "Assets held for sale" under a
  NON-CURRENT header → assets_held_for_sale ; "Assets held for sale" under a CURRENT header
  → other_current_assets. "Liabilities held for sale / directly associated with assets held
  for sale" → other_current_liabilities (unchanged).
- current portion of long-term debt / short-term borrowings / notes payable / commercial paper
  → current.debt ; long-term debt → memo.long_term_debt (never lumped into current.debt).
- Trade payables → accounts_trade_payable. Accrued expenses, compensation & benefits, accrued
  interest, misc payables → other_current_liabilities. (Current "Taxes" → deferred_rev_and_tax, per House Convention 3.)
- Non-current: pension/retirement obligations → pension ; non-current lease liabilities →
  lease_liabilities ; everything else non-current misc → other_liabilities.

## SINGLE-COUNT RULE
Every listed line contributes to exactly one place — one bucket OR one memo field, never both.
In particular, a long-term debt line mapped to memo.long_term_debt must NOT also appear in
other_liabilities. Never map a subtotal AND its components (e.g. never map "Total inventory"
when "Finished goods" and "Work in process" are already mapped). If a line is in a specific
bucket it must not also sit in an other_* bucket or a memo field.

PARENT LINES WITH COMPONENT SUB-LINES (Japanese-GAAP balance sheets): a parent total may not
say "Total" — "Tangible fixed assets" is followed by its components (Buildings, Land, Lease
assets, Construction in progress, Other tangible fixed assets) and "Intangible fixed assets"
by its components (Software, Goodwill, Lease assets, Other intangible fixed assets). The
components sum to the parent; counting both is a double count. Handle EXACTLY like this:
- "Tangible fixed assets" (parent) → ppe. Skip ALL its components (the lease-assets component
  stays inside ppe — do not split it into lease_assets).
- "Intangible fixed assets": do NOT map the parent anywhere (NEVER into ppe). Map only its
  components: Goodwill → memo.goodwill; every other component (software, lease assets, other
  intangible fixed assets) → memo.intangibles. Nothing from this hierarchy goes to
  other_assets or lease_assets.

## OFFSETTING / CUSTODIAL BALANCES (banks, brokers, exchanges, clearing houses)
Performance-bond / guaranty-fund / margin / segregated customer balances appear on BOTH sides
in near-equal amounts. If you map such a balance as a liability, also map the matching asset.

## VERIFICATION (every pass, before returning)
1. Every current-section line is in a current bucket or the cash memo (current "assets held
   for sale" → other_current_assets); non-current lines are not. Only NON-CURRENT "assets held
   for sale" go to assets_held_for_sale.
2. sum(asset buckets) + cash + goodwill + intangibles == printed Total Assets (within rounding).
3. sum(liability buckets) + long_term_debt == printed Total Liabilities (within rounding).
3b. Confirm memo.long_term_debt lines are absent from other_liabilities and all non-current
    buckets (no debt line counted twice).
4. No line double-counted; none dropped; no equity mapped; no plug inserted. A parent line's
   components are skipped when the parent is mapped (Tangible fixed assets → ppe with NO
   component mapped elsewhere; Intangible fixed assets parent unmapped, components memo-only).
5. House Conventions 1–5 obeyed (investment_assets narrow; sundry/financing receivables in
   other_assets; current "Taxes" in deferred_rev_and_tax; contract assets in
   accounts_trade_receivable; long-term debt memo-only).

## DECISION
- All checks pass → return the full JSON.
- Gap > rounding → you double-counted or missed a line: re-map and re-verify.
- Given a CORRECTION note → fix ONLY the indicated issue, copy every other bucket unchanged,
  numbers as printed. Return the full corrected JSON.

Return the JSON now.
"""


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
           .replace(" ", " ").replace(" ", " ").strip())
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
    from . import pdf_locator as _pl
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


def _build_user_message(markdown: str, page_text: str | None = None) -> str:
    schema = json.dumps(empty_result(), indent=2)
    items = extract_line_items(markdown, page_text=page_text)
    items_block = ""
    if len(items) >= 5:
        listing = "\n".join(f"- {label}: {value}" for label, value in items)
        items_block = (
            "LINE ITEMS (extracted in code from the most-recent column; "
            "subtotal/total rows already removed). Bucket EVERY asset and "
            "liability line below into exactly one bucket or memo field "
            "using these EXACT values — do not re-read them from the "
            "markdown. The [ASSET]/[LIABILITY]/[EQUITY] tag is the filing "
            "section the line sits in: [ASSET] lines go ONLY into asset "
            "buckets/memo fields, [LIABILITY] lines ONLY into liability "
            "buckets/memo (never into an asset bucket like tax), and "
            "[EQUITY] lines stay out of the buckets entirely. Each line is "
            "also tagged (current) or (non-current) — map it into the "
            "matching current vs non-current bucket, and treat two "
            "identically-named lines with different tags as SEPARATE lines "
            "counted once each. A line with neither tag: use your own "
            "current-vs-non-current judgement rule:\n"
            f"{listing}\n\n"
        )
    return (
        "TARGET SCHEMA (return EXACTLY this shape; the bucket keys are FIXED — "
        "do not invent new keys):\n"
        f"{schema}\n\n"
        "NUMBERS: no digit-group commas inside a number. When several "
        "balance-sheet lines map into the same bucket (or memo field), do "
        "NOT add them yourself — write the printed values joined by ' + ' "
        'as a JSON STRING (e.g. "other_assets": "100 + 200 + 300") and the '
        "caller will compute the exact sum. Never any other arithmetic. "
        "Every number you write must "
        "be copied character-for-character from the most-recent column of "
        "the markdown — never write a number that does not appear there, and "
        "never repeat a line's value in a second bucket.\n\n"
        f"{items_block}"
        "MAPPING HINTS for lines with no named bucket (they must NEVER be "
        "dropped, or the totals will not reconcile):\n"
        "- Cash and cash equivalents + current marketable/short-term "
        "securities -> memo_excluded.cash_and_marketable_securities (NOT "
        "other_current_assets). Restricted cash, prepaid expenses, current "
        "derivative/receivable odds and ends -> current.other_current_assets.\n"
        "- Goodwill -> memo_excluded.goodwill ; intangible assets (net, "
        "excluding goodwill) -> memo_excluded.intangibles (NEVER into "
        "other_assets). Non-current derivatives, deferred tax assets -> "
        "non_current.other_assets.\n"
        "- Right-of-use assets -> lease_assets (non_current unless the "
        "filing shows a current portion).\n"
        "- Interest-bearing debt splits by the filing's classification: "
        "current portion of long-term debt / notes payable / short-term "
        "borrowings -> current.debt ; long-term debt / non-current "
        "borrowings -> memo_excluded.long_term_debt (NEVER into "
        "current.debt or other_liabilities).\n"
        "- Reconciliation to hit: sum(asset buckets) + memo cash + goodwill "
        "+ intangibles == printed total assets ; sum(liability buckets) + "
        "memo long_term_debt == printed total liabilities (within filing "
        "rounding).\n"
        "- Real estate blocks (Land / Buildings and improvements / less "
        "Accumulated depreciation) belong TOGETHER in exactly ONE bucket: "
        "real_estate_assets for REITs/property companies (include the "
        "negative accumulated depreciation), ppe otherwise. Never repeat or "
        "split the same property block across ppe AND real_estate_assets.\n"
        "- NEVER map subtotal or total rows (current-assets subtotal, total "
        "assets, total liabilities, total equity) into any bucket — "
        "individual line items only, or they will be double-counted.\n\n"
        "BALANCE SHEET MARKDOWN:\n"
        f"{markdown}"
    )


def _call_llm(messages: list[dict]) -> str:
    client = OpenAI(base_url=LLM_BASE_URL, api_key=require_together_key())
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0,
        max_tokens=4096,
        # JSON output mode + the provider's disable-reasoning switch:
        # thinking-by-default models (GLM, DeepSeek, Qwen3) otherwise burn the
        # budget on reasoning tokens and leave .content empty.
        response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    message = response.choices[0].message
    content = message.content or ""
    if not content:
        # Some reasoning models still return the reply in the reasoning field.
        content = (getattr(message, "reasoning_content", None)
                   or getattr(message, "reasoning", None) or "")
    return content


def _strip_fences(text: str) -> str:
    """Strip markdown fences / stray prose — keep the outermost JSON object."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in LLM output.")
    return text[start : end + 1]


# Unquoted number with digit-group commas, e.g.  "tax": 1,234,567
_GROUPED_NUM_RE = re.compile(r"(?<=[:\[,\s])(-?\d{1,3}(?:,\d{3})+)(?=\s*[,\}\]])")
# Literal addition/subtraction chain the model sometimes emits when several
# lines map into one bucket, e.g.  "other_assets": 178281 + 801709 + 89846
_EXPR_RE = re.compile(
    r"(?<=:)\s*(-?\d+(?:\.\d+)?(?:\s*[+-]\s*\d+(?:\.\d+)?)+)(?=\s*[,\n\}])"
)


def _repair_json_numbers(text: str) -> str:
    """Deterministic repair of two invalid-JSON number forms the LLM emits.
    Both keep the values exactly as the model wrote them: commas are stripped
    (the one allowed cleaning), and literal +/- chains are summed in code —
    the terms are the filing's own printed line values."""
    text = _GROUPED_NUM_RE.sub(lambda m: m.group(1).replace(",", ""), text)

    def _sum_expr(match: "re.Match") -> str:
        total = 0.0
        for term in re.findall(r"[+-]?\s*\d+(?:\.\d+)?", match.group(1)):
            total += float(term.replace(" ", ""))
        return f" {int(total)}" if total == int(total) else f" {total}"

    return _EXPR_RE.sub(_sum_expr, text)


def _validate(result: dict) -> None:
    """Ensure the exact schema shape — all fixed keys, no invented bucket keys."""
    template = empty_result()
    for top_key in template:
        if top_key not in result:
            raise ValueError(f"Missing required key: {top_key!r}")

    sections = {
        ("assets", "non_current"): config.ASSET_NON_CURRENT_KEYS,
        ("assets", "current"): config.ASSET_CURRENT_KEYS,
        ("liabilities", "non_current"): config.LIABILITY_NON_CURRENT_KEYS,
        ("liabilities", "current"): config.LIABILITY_CURRENT_KEYS,
    }
    for (group, sub), keys in sections.items():
        block = result.get(group, {}).get(sub)
        if not isinstance(block, dict):
            raise ValueError(f"Missing or invalid section: {group}.{sub}")
        for k in keys:
            if k not in block:
                raise ValueError(f"Missing bucket key: {group}.{sub}.{k}")
        extra = set(block) - set(keys)
        if extra:
            raise ValueError(f"Invented bucket keys in {group}.{sub}: {sorted(extra)}")

    for k in ("preferred_stock", "mezzanine_equity"):
        if k not in result["liabilities"]:
            raise ValueError(f"Missing key: liabilities.{k}")
    memo = result.get("memo_excluded")
    if not isinstance(memo, dict):
        raise ValueError("Missing or invalid section: memo_excluded")
    for k in config.MEMO_KEYS:
        if k not in memo:
            raise ValueError(f"Missing memo key: memo_excluded.{k}")
    extra = set(memo) - set(config.MEMO_KEYS)
    if extra:
        raise ValueError(f"Invented keys in memo_excluded: {sorted(extra)}")
    for k in ("total_assets", "total_liabilities"):
        if k not in result.get("filing_totals", {}):
            raise ValueError(f"Missing key: filing_totals.{k}")


def _parse_and_validate(raw: str) -> dict:
    text = _strip_fences(raw)
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = json.loads(_repair_json_numbers(text))
    _validate(result)
    # Normalize optional blocks the LLM may omit or malform.
    template = empty_result()
    result.setdefault("warnings", [])
    if not isinstance(result["warnings"], list):
        result["warnings"] = [str(result["warnings"])]
    if not isinstance(result.get("tally"), dict):
        result["tally"] = copy.deepcopy(template["tally"])
    return result


def _system_prompt(region: str | None) -> str:
    """Pick the Stage-3 system prompt by company origin: "au" -> the
    Australian (AASB/IFRS) prompt (prompt_au.py); "eu" -> the European/IFRS
    prompt (prompt_eu.py); anything else -> the existing US-oriented prompt,
    unchanged."""
    if region == "au":
        return SYSTEM_PROMPT_AU
    return SYSTEM_PROMPT_EU if region == "eu" else SYSTEM_PROMPT


def standardize(markdown: str, region: str | None = None,
                page_text: str | None = None) -> dict:
    """Map the balance-sheet markdown into the fixed schema. Re-prompts once
    if the LLM output fails to parse/validate; raises if it fails twice.
    `page_text` = Stage-1 page text, used to validate/realign the checklist's
    label-value pairing (see _realign_with_page_text)."""
    messages = [
        {"role": "system", "content": _system_prompt(region)},
        {"role": "user", "content": _build_user_message(markdown, page_text)},
    ]
    raw = _call_llm(messages)
    try:
        return _parse_and_validate(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("LLM output invalid (%s) — re-prompting once.", exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Your previous output was invalid: {exc}. Return ONLY a "
                    "valid JSON object matching the target schema exactly — "
                    "all fixed keys present, no extra keys, no markdown "
                    "fences, no prose."
                ),
            },
        ]
        raw = _call_llm(retry_messages)
        return _parse_and_validate(raw)  # let a second failure raise


def restandardize(markdown: str, previous_json: dict, gap_message: str,
                  region: str | None = None,
                  page_text: str | None = None) -> dict:
    """Tally-failure hook (Stage 4): re-call the LLM with the previous JSON
    and the exact gap so it can re-map missing/double-counted lines. Called
    in a loop by the pipeline until balanced or max retries."""
    messages = [
        {"role": "system", "content": _system_prompt(region)},
        {"role": "user", "content": _build_user_message(markdown, page_text)},
        {"role": "assistant", "content": json.dumps(previous_json)},
        {
            "role": "user",
            "content": (
                f"CORRECTION note: {gap_message} Correct the previous JSON with the SMALLEST "
                "possible edit: change ONLY the bucket(s) implicated by the "
                "gap(s) above and copy every other bucket value UNCHANGED "
                "from the previous JSON. When a bucket holds several lines, "
                "rewrite it as the printed values joined by ' + ' as a JSON "
                'STRING (e.g. "100 + 200"), copying each number character-'
                "for-character from the "
                "most-recent column of the markdown — never a value already "
                "placed in another bucket, never a subtotal row, never a "
                "number that is not printed there. Numbers exactly as printed "
                "(strip only '$' and commas), same fixed schema — "
                "preferred_stock and mezzanine_equity are TOP-LEVEL keys of "
                '"liabilities", never inside non_current or current. Return '
                "ONLY the corrected JSON object."
            ),
        },
    ]
    raw = _call_llm(messages)
    try:
        return _parse_and_validate(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        # Same one-shot schema repair standardize() gets: a correction that
        # dies on shape (e.g. preferred_stock nested under non_current) would
        # otherwise abort the whole tally loop.
        logger.warning("Correction output invalid (%s) — re-prompting once.",
                       exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Your previous output was invalid: {exc}. Return ONLY a "
                    "valid JSON object matching the target schema exactly — "
                    "all fixed keys present, no extra keys (preferred_stock "
                    "and mezzanine_equity are TOP-LEVEL keys of "
                    '"liabilities", never inside non_current or current), '
                    "no markdown fences, no prose."
                ),
            },
        ]
        raw = _call_llm(retry_messages)
        return _parse_and_validate(raw)  # let a second failure raise

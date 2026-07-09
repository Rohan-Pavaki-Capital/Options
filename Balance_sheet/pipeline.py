"""Pipeline orchestrator — Stage 1 (locate) -> Stage 2 (parse) ->
Stage 3 (standardize) -> Stage 4 (tally). Returns the final JSON.

Every stage is wrapped so a failure returns a JSON with "warnings"
(and an "error") rather than crashing.
"""

import copy
import logging
import os

from . import parser, pdf_locator, standardizer, tally
from .config import TALLY_MAX_RETRIES, empty_result

logger = logging.getLogger("balance_sheet.pipeline")


def _merge_balanced_sides(first: dict, retry: dict) -> dict:
    """The tally re-prompt may fix one side while regressing the other. Assets
    and liabilities buckets are disjoint, so keep — per side — whichever run
    lands closer to its printed total; the retry wins only where it improves.
    Each side's memo fields travel WITH the side (they are part of its sum).
    Returns (merged, kept) — kept records which run supplied each side, so
    the caller can merge the RAW (pre-coercion) outputs the same way."""
    def gap(run: dict, side: str) -> float:
        return abs(run["tally"][f"sum_{side}"] - run["filing_totals"][f"total_{side}"])

    merged = retry
    kept = {"assets": "retry", "liabilities": "retry"}
    if gap(first, "assets") < gap(retry, "assets"):
        kept["assets"] = "first"
        merged["assets"] = first["assets"]
        for k in tally.ASSET_MEMO_KEYS:
            merged["memo_excluded"][k] = first["memo_excluded"][k]
        merged["filing_totals"]["total_assets"] = first["filing_totals"]["total_assets"]
        logger.info("Retry regressed the assets side — keeping first run's assets.")
    if gap(first, "liabilities") < gap(retry, "liabilities"):
        kept["liabilities"] = "first"
        merged["liabilities"] = first["liabilities"]
        for k in tally.LIABILITY_MEMO_KEYS:
            merged["memo_excluded"][k] = first["memo_excluded"][k]
        merged["filing_totals"]["total_liabilities"] = first["filing_totals"]["total_liabilities"]
        logger.info("Retry regressed the liabilities side — keeping first run's liabilities.")
    tally.run_tally(merged)
    return merged, kept


def _merge_raw_sides(first_raw: dict, retry_raw: dict, kept: dict) -> dict:
    """Mirror _merge_balanced_sides on the RAW outputs, so the chain-level
    single-count check always inspects the chains that were actually kept."""
    merged = retry_raw
    if kept["assets"] == "first":
        merged["assets"] = first_raw["assets"]
        for k in tally.ASSET_MEMO_KEYS:
            merged["memo_excluded"][k] = first_raw["memo_excluded"][k]
    if kept["liabilities"] == "first":
        merged["liabilities"] = first_raw["liabilities"]
        for k in tally.LIABILITY_MEMO_KEYS:
            merged["memo_excluded"][k] = first_raw["memo_excluded"][k]
    return merged


def run_markdown_pipeline(markdown: str, source_pages: list | None = None,
                          printed_totals: dict | None = None,
                          unit_label: str | None = None) -> dict:
    """Stages 3-4 on balance-sheet markdown that is already in hand:
    LLM standardization, then the code tally with the LLM correction loop.
    A side still unbalanced after the retries is returned honestly unbalanced
    (warning + balanced=False) — a reconciling number is never fabricated.
    `printed_totals` are the code-read printed totals and `unit_label` the
    code-read scale wording (None values are ignored — the LLM's value is
    kept only when code found nothing)."""
    source_pages = source_pages or []
    printed_totals = printed_totals or {}
    # Code (Stage 1 page text, falling back to the markdown) is the source of
    # truth for the scale wording — the LLM value fills in only as a last resort.
    unit_label = unit_label or pdf_locator.extract_unit_label(markdown)

    def _apply_printed_totals(res: dict) -> None:
        for key in ("total_assets", "total_liabilities"):
            value = printed_totals.get(key)
            if value is not None and res["filing_totals"].get(key) != value:
                logger.info(
                    "Overriding LLM %s=%s with printed value %s",
                    key, res["filing_totals"].get(key), value,
                )
                res["filing_totals"][key] = value
        # Fully-unlabeled statement (e.g. APA): no printed 'Total liabilities'
        # and no 'Total liabilities and equity' to derive from, so code read
        # nothing and the LLM's (often wrong) value would otherwise stand.
        # Total equity IS labeled; since Total assets == Total liabilities +
        # equity, derive the split from code-read equity — the LLM never grades
        # its own liabilities tally.
        if printed_totals.get("total_liabilities") is None:
            te = printed_totals.get("total_equity")
            ta = res["filing_totals"].get("total_assets")
            if te and ta and 0 < te < ta:
                res["filing_totals"]["total_liabilities"] = ta - te
                logger.info(
                    "Derived total_liabilities = %s (total_assets %s - "
                    "code-read total_equity %s).", ta - te, ta, te,
                )

    # Stage 3 — LLM standardization into the fixed schema
    try:
        result = standardizer.standardize(markdown)
    except Exception as exc:
        logger.exception("Stage 3 (standardize) failed")
        result = empty_result()
        result["source_pages"] = source_pages
        if unit_label:
            result["unit_label"] = unit_label
        result["warnings"].append(f"Stage 3 (standardize) failed: {exc}")
        result["error"] = str(exc)
        return result
    # Code (not the LLM) is the source of truth for the pages used.
    result["source_pages"] = source_pages
    result.setdefault("warnings", [])

    # Stage 4 — tally + single-count check in code, with an LLM correction
    # loop: re-prompt with the exact gap and/or the exact duplicated line,
    # re-tally, repeat until both sides balance with no chain-level
    # duplicates or TALLY_MAX_RETRIES is reached.
    # The RAW copy keeps the "a + b" chain strings (coercion sums them away)
    # so the duplicate check can see each printed term the model used.
    raw = copy.deepcopy(result)
    tally.coerce_result_numbers(result)
    _apply_printed_totals(result)
    tally.run_tally(result)

    line_items = standardizer.extract_line_items(markdown)
    tagged = len(line_items) >= 5  # same reliability bar as the prompt checklist
    duplicates = tally.find_chain_duplicates(raw, line_items) if tagged else []

    for attempt in range(1, TALLY_MAX_RETRIES + 1):
        if tally.is_balanced(result) and not duplicates:
            break
        parts = []
        if not tally.is_balanced(result):
            parts.append(tally.build_gap_message(result))
        if duplicates:
            parts.append(tally.build_duplicate_message(duplicates))
        gap_message = " ".join(parts)
        logger.info(
            "Correction re-prompt %d/%d: %s",
            attempt, TALLY_MAX_RETRIES, gap_message,
        )
        try:
            # The RAW previous JSON goes back to the model — its chains let
            # the model remove a single duplicated term instead of guessing
            # a new bucket total.
            retry = standardizer.restandardize(markdown, raw, gap_message)
            retry["source_pages"] = source_pages
            retry_raw = copy.deepcopy(retry)
            tally.coerce_result_numbers(retry)
            _apply_printed_totals(retry)
            tally.run_tally(retry)
            result, kept = _merge_balanced_sides(result, retry)
            raw = _merge_raw_sides(raw, retry_raw, kept)
            duplicates = tally.find_chain_duplicates(raw, line_items) if tagged else []
        except Exception as exc:
            logger.exception("Tally re-prompt failed; keeping best result")
            result["warnings"].append(f"Tally re-prompt failed: {exc}")
            break

    if tagged:
        # Deterministic split fix BEFORE the final verdict: same-side move,
        # so the sums and balanced booleans are unaffected.
        tally.regroup_current_tax_liability(result, line_items)
    if not tally.is_balanced(result):
        # Honest failure: name the exact remaining gap in warnings and leave
        # the side unbalanced (assets_balanced / liabilities_balanced stay
        # False). A number is NEVER invented to force the totals to tie.
        tally.add_unbalanced_warnings(result)
    for dup in duplicates:
        # Unresolved single-count violation: name it loudly. Code never
        # strips the term — which chain is the correct home needs the
        # model's judgement, and forcing it would silently mis-bucket.
        fields = ", ".join(dict.fromkeys(dup["fields"]))
        result["warnings"].append(
            f"SINGLE-COUNT VIOLATION unresolved after retries ({dup['side']}): "
            f"printed line '{dup['label']}' = {dup['value']:,} is counted in "
            f"more than one place ({fields}). It must live in exactly one - "
            f"no value was auto-removed."
        )
    if tagged:
        tally.guard_equity_adjacent_buckets(result, line_items)
    tally.sanity_check_other_buckets(result)

    if unit_label and result.get("unit_label") != unit_label:
        logger.info("Overriding LLM unit_label=%r with code-read %r",
                    result.get("unit_label"), unit_label)
        result["unit_label"] = unit_label
    return result


def run_pipeline(pdf_path: str) -> dict:
    """Run the full 4-stage pipeline on a 10-Q/10-K PDF path."""
    result = empty_result()
    temp_pdf = None
    try:
        # Stage 1 — locate the balance-sheet page(s)
        try:
            located = pdf_locator.locate_balance_sheet(pdf_path)
        except Exception as exc:
            logger.exception("Stage 1 (locate) failed")
            result["warnings"].append(f"Stage 1 (locate) failed: {exc}")
            result["error"] = str(exc)
            return result
        temp_pdf = located["temp_pdf_path"]
        source_pages = located["page_numbers"]
        stage1_warnings = located.get("warnings", [])
        result["warnings"].extend(stage1_warnings)
        logger.info("Captured pages %s from %s", source_pages, pdf_path)

        # Printed totals read from the page text in CODE — the reconciliation
        # reference must not depend on LLM transcription. None = not found
        # (keep the LLM's value in that case).
        printed_totals = pdf_locator.extract_printed_totals(
            located.get("captured_text", "")
        )
        # Scale wording read from the page text in CODE too — LlamaParse can
        # drop the "($ in millions)" header line from the markdown entirely.
        unit_label = pdf_locator.extract_unit_label(
            located.get("captured_text", "")
        )

        # Stage 2 — LlamaParse to markdown
        try:
            markdown = parser.parse_to_markdown(temp_pdf)
        except Exception as exc:
            logger.exception("Stage 2 (LlamaParse) failed")
            result["source_pages"] = source_pages
            result["warnings"].append(f"Stage 2 (LlamaParse) failed: {exc}")
            result["error"] = str(exc)
            return result
        logger.info("Markdown length: %d chars", len(markdown))

        # Stages 3-4 — standardize + tally
        result = run_markdown_pipeline(markdown, source_pages, printed_totals,
                                       unit_label)
        result["warnings"] = stage1_warnings + result["warnings"]
        return result
    finally:
        if temp_pdf and os.path.exists(temp_pdf):
            try:
                os.remove(temp_pdf)
            except OSError:
                logger.warning("Could not delete temp PDF %s", temp_pdf)

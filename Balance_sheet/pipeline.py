"""Pipeline orchestrator — Stage 1 (locate) -> Stage 2 (parse) ->
Stage 3 (standardize) -> Stage 4 (tally). Returns the final JSON.

Every stage is wrapped so a failure returns a JSON with "warnings"
(and an "error") rather than crashing.
"""

import logging
import os

from . import parser, pdf_locator, standardizer, tally
from .config import empty_result

logger = logging.getLogger("balance_sheet.pipeline")


def _merge_balanced_sides(first: dict, retry: dict) -> dict:
    """The tally re-prompt may fix one side while regressing the other. Assets
    and liabilities buckets are disjoint, so keep — per side — whichever run
    lands closer to its printed total; the retry wins only where it improves."""
    def gap(run: dict, side: str) -> float:
        return abs(run["tally"][f"sum_{side}"] - run["filing_totals"][f"total_{side}"])

    merged = retry
    if gap(first, "assets") < gap(retry, "assets"):
        merged["assets"] = first["assets"]
        merged["filing_totals"]["total_assets"] = first["filing_totals"]["total_assets"]
        logger.info("Retry regressed the assets side — keeping first run's assets.")
    if gap(first, "liabilities") < gap(retry, "liabilities"):
        merged["liabilities"] = first["liabilities"]
        merged["filing_totals"]["total_liabilities"] = first["filing_totals"]["total_liabilities"]
        logger.info("Retry regressed the liabilities side — keeping first run's liabilities.")
    tally.run_tally(merged)
    return merged


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
        result["warnings"].extend(located.get("warnings", []))
        logger.info("Captured pages %s from %s", source_pages, pdf_path)

        # Printed totals read from the page text in CODE — the reconciliation
        # reference must not depend on LLM transcription. None = not found
        # (keep the LLM's value in that case).
        printed_totals = pdf_locator.extract_printed_totals(
            located.get("captured_text", "")
        )

        def _apply_printed_totals(res: dict) -> None:
            for key, value in printed_totals.items():
                if value is not None and res["filing_totals"].get(key) != value:
                    logger.info(
                        "Overriding LLM %s=%s with printed value %s",
                        key, res["filing_totals"].get(key), value,
                    )
                    res["filing_totals"][key] = value

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

        # Stage 3 — LLM standardization into the fixed schema
        try:
            result = standardizer.standardize(markdown)
        except Exception as exc:
            logger.exception("Stage 3 (standardize) failed")
            result = empty_result()
            result["source_pages"] = source_pages
            result["warnings"].append(f"Stage 3 (standardize) failed: {exc}")
            result["error"] = str(exc)
            return result
        # Code (not the LLM) is the source of truth for the pages used.
        result["source_pages"] = source_pages
        result.setdefault("warnings", [])

        # Stage 4 — tally check in code, with one LLM re-prompt on imbalance
        tally.coerce_result_numbers(result)
        _apply_printed_totals(result)
        tally.run_tally(result)

        if not tally.is_balanced(result):
            gap_message = tally.build_gap_message(result)
            logger.info("Tally failed — re-prompting LLM once: %s", gap_message)
            try:
                retry = standardizer.restandardize(markdown, result, gap_message)
                retry["source_pages"] = source_pages
                tally.coerce_result_numbers(retry)
                _apply_printed_totals(retry)
                tally.run_tally(retry)
                result = _merge_balanced_sides(result, retry)
            except Exception as exc:
                logger.exception("Tally re-prompt failed; keeping first result")
                result["warnings"].append(f"Tally re-prompt failed: {exc}")

        if not tally.is_balanced(result):
            tally.add_unbalanced_warnings(result)
        tally.sanity_check_other_buckets(result)

        return result
    finally:
        if temp_pdf and os.path.exists(temp_pdf):
            try:
                os.remove(temp_pdf)
            except OSError:
                logger.warning("Could not delete temp PDF %s", temp_pdf)

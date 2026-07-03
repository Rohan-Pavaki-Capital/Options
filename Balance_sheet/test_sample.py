"""Run the balance-sheet pipeline on local sample PDFs and print the JSON.

Usage:
    python -m Balance_sheet.test_sample [path-to-10Q-or-10K.pdf]
With no argument, runs every known sample below that exists on disk and
asserts its documented expectations.

Documented expectations
-----------------------
Diversified Healthcare Trust 10-Q (dollars in thousands):
    filing_totals.total_assets      == 4,267,552
    filing_totals.total_liabilities == 2,647,133
    both sides balanced.
Known trap: accrued interest (26,078) must land in other_liabilities, or the
liabilities sum to 2,621,055 and the tally fails — the re-prompt step should
catch and fix this.

Apple 10-Q, March 28 2026 (dollars in millions) — double-count regression:
    sum_assets == 371,082 and sum_liabilities == 264,591, both balanced
    ppe == 50,116, investment_assets == 78,088, other_assets == 98,764
Known trap: PP&E (50,116) used to be counted in 'ppe' AND folded into
other_assets (148,780 instead of 98,764), over-counting assets by ~50,016 —
the single-bucket rule + diagnosis re-prompt must prevent/self-correct it.

CME Group 10-Q, March 31 2026 (dollars in millions) — custodial-asset regression:
    filing_totals.total_assets == 201,993, total_liabilities == 175,375
    both sides balanced (asset buckets sum to 201,993, liability buckets to 175,375).
Known trap: the clearing-house performance-bond/guaranty-fund collateral appears
on BOTH sides (~165,922 liability, ~165,035 matching cash/securities asset). The
LLM used to map only the liability side and drop the asset — assets summed to
36,958 (gap -165,035) and the service returned it unbalanced. The custodial
asset must now land in other_assets/other_current_assets so assets tie.
"""

import json
import logging
import os
import sys

from Balance_sheet.pipeline import run_pipeline

DHC_SAMPLE_PDF = r"test_data\dhc_10q.pdf"
AAPL_SAMPLE_PDF = r"test_data\aapl_10q.pdf"
CME_SAMPLE_PDF = r"test_data\cme_10q.pdf"


def check_dhc(result: dict) -> list[str]:
    failures = []
    tally = result["tally"]
    totals = result["filing_totals"]
    if totals["total_assets"] != 4267552:
        failures.append(f"total_assets {totals['total_assets']:,} != 4,267,552")
    if totals["total_liabilities"] != 2647133:
        failures.append(f"total_liabilities {totals['total_liabilities']:,} != 2,647,133")
    if not tally["assets_balanced"]:
        failures.append("assets_balanced is False")
    if not tally["liabilities_balanced"]:
        failures.append("liabilities_balanced is False")
    return failures


def check_aapl(result: dict) -> list[str]:
    failures = []
    tally = result["tally"]
    non_current = result["assets"]["non_current"]
    if not tally["assets_balanced"]:
        failures.append("assets_balanced is False")
    if not tally["liabilities_balanced"]:
        failures.append("liabilities_balanced is False")
    if tally["sum_assets"] != 371082:
        failures.append(f"sum_assets {tally['sum_assets']:,} != 371,082")
    if tally["sum_liabilities"] != 264591:
        failures.append(f"sum_liabilities {tally['sum_liabilities']:,} != 264,591")
    if non_current["ppe"] != 50116:
        failures.append(f"ppe {non_current['ppe']:,} != 50,116")
    if non_current["investment_assets"] != 78088:
        failures.append(f"investment_assets {non_current['investment_assets']:,} != 78,088")
    if non_current["other_assets"] != 98764:
        failures.append(
            f"other_assets {non_current['other_assets']:,} != 98,764 "
            f"(148,780 would mean PP&E was double-counted)"
        )
    return failures


def check_cme(result: dict) -> list[str]:
    failures = []
    tally = result["tally"]
    totals = result["filing_totals"]
    assets = result["assets"]
    if totals["total_assets"] != 201993:
        failures.append(f"total_assets {totals['total_assets']:,} != 201,993")
    if totals["total_liabilities"] != 175375:
        failures.append(f"total_liabilities {totals['total_liabilities']:,} != 175,375")
    # CME prints millions with one decimal, so bucket sums carry decimals —
    # tie to the printed totals within the pipeline's own tolerance.
    if abs(tally["sum_assets"] - 201993) > 1:
        failures.append(f"sum_assets {tally['sum_assets']:,} not within 1 of 201,993")
    if abs(tally["sum_liabilities"] - 175375) > 1:
        failures.append(f"sum_liabilities {tally['sum_liabilities']:,} not within 1 of 175,375")
    if not tally["assets_balanced"]:
        failures.append("assets_balanced is False")
    if not tally["liabilities_balanced"]:
        failures.append("liabilities_balanced is False")
    # The ~165,035 custodial/performance-bond asset (dropped pre-fix) must now
    # sit in the other_* asset buckets.
    other_total = (assets["non_current"]["other_assets"]
                   + assets["current"]["other_current_assets"])
    if other_total < 165035:
        failures.append(
            f"other_assets + other_current_assets = {other_total:,} < 165,035 "
            f"(the custodial performance-bond asset is still missing)"
        )
    return failures


SAMPLES = [
    (DHC_SAMPLE_PDF, "dhc", check_dhc),
    (AAPL_SAMPLE_PDF, "aapl", check_aapl),
    (CME_SAMPLE_PDF, "cme", check_cme),
]


def _run_one(pdf_path: str) -> list[str]:
    result = run_pipeline(pdf_path)
    print(json.dumps(result, indent=2))

    tally = result.get("tally", {})
    print(f"\n--- Tally result: {pdf_path} ---")
    print(f"source pages          : {result.get('source_pages')}")
    print(f"sum_assets            : {tally.get('sum_assets'):,}")
    print(f"printed total_assets  : {result.get('filing_totals', {}).get('total_assets'):,}")
    print(f"assets_balanced       : {tally.get('assets_balanced')}")
    print(f"sum_liabilities       : {tally.get('sum_liabilities'):,}")
    print(f"printed total_liabs   : {result.get('filing_totals', {}).get('total_liabilities'):,}")
    print(f"liabilities_balanced  : {tally.get('liabilities_balanced')}")
    if result.get("warnings"):
        print(f"warnings              : {result['warnings']}")

    # Apply the documented expectations when the file is a known sample.
    name = os.path.basename(pdf_path).lower()
    failures = []
    for _path, tag, check in SAMPLES:
        if tag in name:
            failures = check(result)
            verdict = "PASS" if not failures else "FAIL: " + "; ".join(failures)
            print(f"expectations ({tag})    : {verdict}")
            break
    if not any(tag in name for _p, tag, _c in SAMPLES):
        balanced = tally.get("assets_balanced") and tally.get("liabilities_balanced")
        if not balanced:
            failures = ["not balanced"]
    return failures


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1:
        paths = [sys.argv[1]]
    else:
        paths = [p for p, _tag, _c in SAMPLES if os.path.isfile(p)]
        if not paths:
            print("No sample PDFs found (expected "
                  f"{DHC_SAMPLE_PDF}, {AAPL_SAMPLE_PDF} and/or {CME_SAMPLE_PDF}).")
            return 1

    all_failures = []
    for path in paths:
        all_failures.extend(_run_one(path))
    return 0 if not all_failures else 1


if __name__ == "__main__":
    sys.exit(main())

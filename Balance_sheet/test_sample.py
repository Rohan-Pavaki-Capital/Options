"""Run the balance-sheet pipeline on local sample PDFs and print the JSON.

Usage:
    python -m Balance_sheet.test_sample [path-to-10Q-or-10K.pdf]
With no argument, runs every known sample below that exists on disk and
asserts its documented expectations.

Documented expectations
-----------------------
Diversified Healthcare Trust 10-Q (dollars in thousands — the pipeline's
final step converts thousands to whole millions AFTER the printed-scale
tally, user decision 2026-07-07, so the returned values are in millions):
    filing_totals.total_assets      == 4,268   (printed 4,267,552 thousand)
    filing_totals.total_liabilities == 2,647   (printed 2,647,133 thousand)
    both sides balanced.
    real_estate_assets ~= 3,819 (REIT property stays real_estate, NOT ppe)
    memo.long_term_debt ~= 2,402 (senior notes/term debt is a memo field)
Known trap: accrued interest (printed 26,078) must land in other_liabilities,
or the liabilities sum to 2,621,055 at printed scale and the tally fails —
the re-prompt step should catch and fix this.

Apple 10-Q, March 28 2026 (dollars in millions) — double-count regression:
    sum_assets == 371,082 and sum_liabilities == 264,591, both balanced
    ppe == 50,116, investment_assets == 78,088 (non-current marketable securities)
    memo.cash_and_st_investments == 68,507 (cash 45,572 + current marketable 22,935)
    memo.goodwill_and_intangibles == 21,334 ("Intangible assets, net")
    memo.long_term_debt == 74,404 (non-current term debt)
    other_assets == 77,430 ("Other non-current assets" only)
Known trap: PP&E (50,116) used to be counted in 'ppe' AND folded into
other_assets, over-counting assets by ~50,016 — the single-bucket rule +
diagnosis re-prompt must prevent/self-correct it.

CME Group 10-Q, March 31 2026 (dollars in millions) — custodial-asset regression:
    filing_totals.total_assets == 201,993, total_liabilities == 175,375
    both sides balanced (buckets + memo sum to the printed totals).
    ppe ~= 355.4 ; real_estate_assets == 0 (operating property is ppe, not real estate)
    accounts_trade_receivable == 935.5
    memo.cash_and_st_investments ~= 2,515.4 (cash 2,391.2 + securities 124.2)
    other_current_assets >= 165,000 (other 515.0 + performance bonds 165,035.3 —
        custodial collateral is NOT memo cash and stays CURRENT per the headers)
    memo.goodwill_and_intangibles ~= 30,232.1 (17,175.3 + 2,550.8 + 10,506.0)
    other_assets (non-current) ~= 2,404.5 (misc other only)
Known traps: (1) the clearing-house performance-bond/guaranty-fund collateral
appears on BOTH sides (~165,922 liability, ~165,035 matching cash/securities
asset). The LLM used to map only the liability side and drop the asset — assets
summed to 36,958 (gap -165,035) and the service returned it unbalanced.
(2) mapping-quality regression: the LLM then swept current items + intangibles
+ the custodial asset into NON-current other_assets (197,672) and put operating
property (355.4) into real_estate_assets — tally passed but placement was wrong.

NIKE 10-Q, February 28 2026 (dollars in millions) — canonical manual-sheet target
(markdown fixture, Stages 3-4 only; balance sheet transcribed from the filing;
bucket placements matched to the analyst's manually standardized sheet,
user decision 2026-07-05):
    memo.cash_and_st_investments == 8,057 (cash 6,660 + ST investments 1,397)
    memo.goodwill_and_intangibles == 499 (intangibles 259 + goodwill 240)
    memo.long_term_debt == 7,030
    non_current: lease_assets == 2,886 ; ppe == 4,766 ; other_assets == 5,729
        ("Deferred income taxes and other assets" mixed line — NOT memo)
    current: inventory == 7,487 ; accounts_trade_receivable == 5,369 ;
        other_current_assets == 2,271 (prepaid only — cash is memo)
    current.debt == 999 (current portion of LT debt; notes payable 0)
    accounts_trade_payable == 2,888 ; current.lease_liabilities == 493
    current.deferred_rev_and_tax == 275 (income taxes payable)
    other_current_liabilities == 6,183 (accrued only)
    non_current.lease_liabilities == 2,656
    non_current.other_liabilities == 2,450 ("Deferred income taxes and other
        liabilities" combined line maps whole to other_liabilities;
        non_current.deferred_rev_and_tax stays 0)
    total liabilities tie to 22,974 (= printed L&E 37,064 - equity 14,090; the
        filing prints no explicit "Total liabilities" line) with NO auto-plug.
Known traps: the standardizer used to lump current portion 999 + long-term
7,030 into current.debt (8,029); later, before the memo section existed, cash
was folded into other_current_assets, goodwill/intangibles into other_assets,
and long-term debt into non_current.other_liabilities — the workbook's internal
logic handles those three groups, so they must be memo fields, not buckets.
"""

import json
import logging
import os
import sys

from Balance_sheet.pipeline import run_markdown_pipeline, run_pipeline

DHC_SAMPLE_PDF = r"test_data\dhc_10q.pdf"
AAPL_SAMPLE_PDF = r"test_data\aapl_10q.pdf"
CME_SAMPLE_PDF = r"test_data\cme_10q.pdf"
NIKE_SAMPLE_MD = r"test_data\nike_10q.md"

# NIKE prints Total assets but no explicit "Total liabilities" line — the
# code-read tally targets (most-recent column) are supplied here, exactly as
# pdf_locator.extract_printed_totals would for a PDF sample.
NIKE_PRINTED_TOTALS = {"total_assets": 37064, "total_liabilities": 22974}

_PLUG_MARKERS = ("Auto-plugged", "Auto-removed", "LIKELY WRONG-BUCKET")


def check_dhc(result: dict) -> list[str]:
    failures = []
    tally = result["tally"]
    totals = result["filing_totals"]
    non_current = result["assets"]["non_current"]
    # DHC prints thousands — the pipeline's final step converts the returned
    # values to whole millions (tally is verified at printed scale first).
    if totals["total_assets"] != 4268:
        failures.append(f"total_assets {totals['total_assets']:,} != 4,268 "
                        f"(printed 4,267,552 thousand -> millions)")
    if totals["total_liabilities"] != 2647:
        failures.append(f"total_liabilities {totals['total_liabilities']:,} != 2,647 "
                        f"(printed 2,647,133 thousand -> millions)")
    if not tally["assets_balanced"]:
        failures.append("assets_balanced is False")
    if not tally["liabilities_balanced"]:
        failures.append("liabilities_balanced is False")
    if abs(non_current["real_estate_assets"] - 3819) > 1:
        failures.append(
            f"real_estate_assets {non_current['real_estate_assets']:,} != ~3,819 "
            f"(REIT property must stay in real_estate_assets, not ppe)"
        )
    if abs(result["memo"]["long_term_debt"] - 2402) > 1:
        failures.append(
            f"memo.long_term_debt {result['memo']['long_term_debt']:,} != "
            f"~2,402 (senior notes/term debt is a memo field)"
        )
    if result.get("unit_label") != "in millions":
        failures.append(
            f"unit_label {result.get('unit_label')!r} != 'in millions' "
            f"(thousands filing must be converted to the standard scale)"
        )
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
    if non_current["other_assets"] != 77430:
        failures.append(
            f"other_assets {non_current['other_assets']:,} != 77,430 "
            f"('Other non-current assets' only — intangibles are memo, and "
            f"148,780 would mean PP&E was double-counted)"
        )
    # Cash (45,572) + current marketable securities (22,935) are memo lines —
    # the workbook consumes them separately; never in other_current_assets.
    memo = result["memo"]
    if memo["cash_and_st_investments"] != 68507:
        failures.append(
            f"memo.cash_and_st_investments {memo['cash_and_st_investments']:,} "
            f"!= 68,507 (cash 45,572 + current marketable securities 22,935)"
        )
    if memo["goodwill_and_intangibles"] != 21334:
        failures.append(
            f"memo.goodwill_and_intangibles {memo['goodwill_and_intangibles']:,} "
            f"!= 21,334 ('Intangible assets, net' is a memo line even without "
            f"the word goodwill)"
        )
    if memo["long_term_debt"] != 74404:
        failures.append(
            f"memo.long_term_debt {memo['long_term_debt']:,} != 74,404 "
            f"(non-current term debt is a memo field, not other_liabilities)"
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
    # Placement quality: current lines in current buckets, operating property
    # in ppe, intangibles+goodwill grouped in non-current other_assets.
    non_current = assets["non_current"]
    current = assets["current"]
    if abs(non_current["ppe"] - 355.4) > 1:
        failures.append(f"ppe {non_current['ppe']:,} != ~355.4")
    if non_current["real_estate_assets"] != 0:
        failures.append(
            f"real_estate_assets {non_current['real_estate_assets']:,} != 0 "
            f"(operating property belongs in ppe, not real_estate_assets)"
        )
    if current["accounts_trade_receivable"] != 935.5:
        failures.append(
            f"accounts_trade_receivable {current['accounts_trade_receivable']:,} != 935.5"
        )
    # Other current 515.0 + performance bonds 165,035.3 = 165,550.3 — the
    # custodial asset is CURRENT per the filing and is NOT memo cash.
    if current["other_current_assets"] < 165000:
        failures.append(
            f"other_current_assets {current['other_current_assets']:,} < 165,000 "
            f"(the performance-bond asset is missing from it)"
        )
    memo = result["memo"]
    if abs(memo["cash_and_st_investments"] - 2515.4) > 1:
        failures.append(
            f"memo.cash_and_st_investments {memo['cash_and_st_investments']:,} "
            f"!= ~2,515.4 (cash 2,391.2 + marketable securities 124.2)"
        )
    if abs(memo["goodwill_and_intangibles"] - 30232.1) > 1:
        failures.append(
            f"memo.goodwill_and_intangibles {memo['goodwill_and_intangibles']:,} "
            f"!= ~30,232.1 (intangibles 17,175.3 + 2,550.8 + goodwill 10,506.0)"
        )
    if abs(non_current["other_assets"] - 2404.5) > 1:
        failures.append(
            f"other_assets {non_current['other_assets']:,} != ~2,404.5 "
            f"(misc other non-current only — goodwill/intangibles are memo)"
        )
    return failures


def check_nike(result: dict) -> list[str]:
    failures = []
    tally = result["tally"]
    totals = result["filing_totals"]
    current = result["liabilities"]["current"]
    non_current = result["liabilities"]["non_current"]
    a_current = result["assets"]["current"]
    a_non_current = result["assets"]["non_current"]
    memo = result["memo"]
    if totals["total_liabilities"] != 22974:
        failures.append(f"total_liabilities {totals['total_liabilities']:,} != 22,974")
    if not tally["assets_balanced"]:
        failures.append("assets_balanced is False")
    if not tally["liabilities_balanced"]:
        failures.append("liabilities_balanced is False")
    # Memo lines — the workbook's internal logic consumes these; they must
    # never sit in the buckets (manual-sheet target, 2026-07-05).
    if memo["cash_and_st_investments"] != 8057:
        failures.append(
            f"memo.cash_and_st_investments {memo['cash_and_st_investments']:,} "
            f"!= 8,057 (cash 6,660 + short-term investments 1,397)"
        )
    if memo["goodwill_and_intangibles"] != 499:
        failures.append(
            f"memo.goodwill_and_intangibles {memo['goodwill_and_intangibles']:,} "
            f"!= 499 (intangibles 259 + goodwill 240)"
        )
    if memo["long_term_debt"] != 7030:
        failures.append(
            f"memo.long_term_debt {memo['long_term_debt']:,} != 7,030 "
            f"(long-term debt is a memo field, never other_liabilities)"
        )
    # Asset buckets — must match the analyst's manual sheet exactly.
    if a_non_current["lease_assets"] != 2886:
        failures.append(
            f"non_current.lease_assets {a_non_current['lease_assets']:,} != 2,886"
        )
    if a_non_current["ppe"] != 4766:
        failures.append(f"ppe {a_non_current['ppe']:,} != 4,766")
    if a_non_current["other_assets"] != 5729:
        failures.append(
            f"other_assets {a_non_current['other_assets']:,} != 5,729 "
            f"(mixed 'Deferred income taxes and other assets' line only — "
            f"goodwill/intangibles belong in memo)"
        )
    if a_current["inventory"] != 7487:
        failures.append(f"inventory {a_current['inventory']:,} != 7,487")
    if a_current["accounts_trade_receivable"] != 5369:
        failures.append(
            f"accounts_trade_receivable {a_current['accounts_trade_receivable']:,} != 5,369"
        )
    if a_current["other_current_assets"] != 2271:
        failures.append(
            f"other_current_assets {a_current['other_current_assets']:,} != 2,271 "
            f"(prepaid only — cash/ST investments belong in memo)"
        )
    # Liability buckets.
    if current["debt"] != 999:
        failures.append(
            f"current.debt {current['debt']:,} != 999 (8,029 would mean "
            f"long-term debt was lumped into current.debt)"
        )
    if non_current["other_liabilities"] != 2450:
        failures.append(
            f"non_current.other_liabilities {non_current['other_liabilities']:,} "
            f"!= 2,450 (the combined 'Deferred income taxes and other "
            f"liabilities' line maps whole to other_liabilities; long-term "
            f"debt goes to memo, not here)"
        )
    if current["accounts_trade_payable"] != 2888:
        failures.append(
            f"accounts_trade_payable {current['accounts_trade_payable']:,} != 2,888"
        )
    if current["lease_liabilities"] != 493:
        failures.append(
            f"current.lease_liabilities {current['lease_liabilities']:,} != 493"
        )
    if current["deferred_rev_and_tax"] != 275:
        failures.append(
            f"current.deferred_rev_and_tax {current['deferred_rev_and_tax']:,} "
            f"!= 275 (income taxes payable)"
        )
    if current["other_current_liabilities"] != 6183:
        failures.append(
            f"other_current_liabilities {current['other_current_liabilities']:,} "
            f"!= 6,183 (accrued only — income taxes payable goes to "
            f"current.deferred_rev_and_tax)"
        )
    if non_current["lease_liabilities"] != 2656:
        failures.append(
            f"non_current.lease_liabilities {non_current['lease_liabilities']:,} != 2,656"
        )
    if non_current["deferred_rev_and_tax"] != 0:
        failures.append(
            f"non_current.deferred_rev_and_tax {non_current['deferred_rev_and_tax']:,} "
            f"!= 0 (the combined line lives whole in other_liabilities)"
        )
    for bucket in ("preferred_stock", "mezzanine_equity"):
        if result["liabilities"][bucket] != 0:
            failures.append(
                f"{bucket} {result['liabilities'][bucket]:,} != 0 (NIKE's "
                f"redeemable preferred is nil; Class B common stock 3 is "
                f"ordinary equity, not mezzanine)"
            )
    plugs = [w for w in result.get("warnings", [])
             if any(m in w for m in _PLUG_MARKERS)]
    if plugs:
        failures.append(f"auto-plug fired (must tie by correct mapping): {plugs}")
    return failures


SAMPLES = [
    (DHC_SAMPLE_PDF, "dhc", check_dhc),
    (AAPL_SAMPLE_PDF, "aapl", check_aapl),
    (CME_SAMPLE_PDF, "cme", check_cme),
    (NIKE_SAMPLE_MD, "nike", check_nike),
]


def _run_one(pdf_path: str) -> list[str]:
    if pdf_path.lower().endswith(".md"):
        # Markdown fixture — Stages 3-4 only (no PDF locate/parse).
        with open(pdf_path, encoding="utf-8") as fh:
            markdown = fh.read()
        printed = NIKE_PRINTED_TOTALS if "nike" in pdf_path.lower() else None
        result = run_markdown_pipeline(markdown, printed_totals=printed)
    else:
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
            print("No samples found (expected "
                  f"{DHC_SAMPLE_PDF}, {AAPL_SAMPLE_PDF}, {CME_SAMPLE_PDF} "
                  f"and/or {NIKE_SAMPLE_MD}).")
            return 1

    all_failures = []
    for path in paths:
        all_failures.extend(_run_one(path))
    return 0 if not all_failures else 1


if __name__ == "__main__":
    sys.exit(main())

# XBRL Options Extractor — Simple Explanation

## What is this?

A tool that pulls stock options / RSU / PSU data for US companies **directly from the numbers inside a SEC filing** (10-K or 10-Q).

No PDF reading. No OCR. No AI guessing. The numbers are **exactly what the company itself tagged** in its filing.

It was built to compare against our existing PDF + AI pipeline. Both produce the same JSON and the same Excel layout, so you can open the two workbooks side by side.

## What is XBRL?

When a company files a 10-K, the document is not just text. Every number in it is wrapped in a hidden machine-readable **tag** (this is called Inline XBRL). Think of it as the company filling out a form behind the scenes:

```
Number:      9,659,000
What is it:  "Options Outstanding" (a standard tag name)
As of when:  2025-12-31
For which:   Employee Stock Options (not RSUs, not PSUs)
Unit:        shares
```

So instead of reading the table on the page, we read these tags. The company already told us what each number means.

## How it works (step by step)

```
ticker (e.g. MSFT)
      |
      v
1. FETCH the latest 10-K/10-Q from SEC EDGAR
   and read all its XBRL tags (thousands of facts)
      |
      v
2. KEEP only the share-based-comp tags
   (tag names are matched against known patterns,
    e.g. "OptionsOutstandingNumber", "OptionsGrantsInPeriod...")
      |
      v
3. PICK the right time period
   Only the period ending at the filing's report date is used.
   Old comparative periods are never reused as current data.
      |
      v
4. SORT facts into buckets
   Each tag says which award it belongs to
   (Stock Options / RSU / PSU / plan name).
      |
      v
5. MERGE related buckets
   Some companies tag totals in one place and details
   (prices, remaining life) in another — these are joined
   carefully, never across different award types.
      |
      v
6. CLEAN UP units
   "P4Y6M" -> 4.5 years, 0.35 -> 35%, negatives -> positive flows.
   The values themselves are never changed.
      |
      v
7. OUTPUT
   Same JSON schema + same Excel file as the PDF pipeline.
```

## What each tag tells us

Every tagged number carries four things:

1. **Tag name** — what the number is (outstanding count, exercise price, volatility, vesting period...). These come from a standard dictionary called `us-gaap` that all US filers use.
2. **Date / period** — when it applies (start of year, end of year, or the full year).
3. **Award type** — which plan it belongs to (Stock Options vs RSU vs PSU).
4. **Unit** — shares, dollars-per-share, or a percentage.

We just select the right combination of these four and place the value in our schema.

## What it fills (exact filed values)

- Full rollforward: opening, granted, exercised, vested, forfeited, closing, exercisable — current **and** prior year
- Weighted-average exercise prices and grant-date fair values
- Weighted-average remaining contractual life
- Valuation assumptions: volatility, dividend yield, risk-free rate, expected term
- Vesting period (when tagged as a number)

## What stays empty (on purpose)

Some things are only written as sentences in the filing, not as tagged numbers, so this tool cannot fill them:

- Vesting descriptions and performance conditions (narrative text)
- Exercise price range tables
- Grant-level tranche details
- Per-plan splits when the company only tags one combined total

This is the whole point of the comparison: XBRL gives perfect numbers but no story; the PDF+AI pipeline reads the story too.

## The two modes

- **`mode=rules` (default)** — strict. Every value is the exact tagged number or empty. No defaults, no guesses (no "4.0 years" fallback, no minimum strike).
- **`mode=ai`** — runs the exact same AI logic as the PDF workflow, but instead of PDF pages it reads the footnote text embedded in the XBRL. Useful because it also sees documents the PDF render can miss (e.g. IBM files its financials in a separate exhibit).

## Maturity (years) — how it is chosen

For options: remaining contractual life → vesting period → cost-recognition period.
For RSU/PSU: vesting period → cost-recognition period.

Every link in that chain is a real disclosed number from the filing — never a made-up constant.

## How to run it

API (server running):

```
POST /api/extract-from-xbrl        {"ticker": "MSFT", "form": "10-K"}
GET  /api/xbrl/excel/options?ticker=CAG
GET  /api/xbrl/excel/options?ticker=IBM&mode=ai
```

CLI (no server needed):

```
.rog\Scripts\python.exe "USA xbrl\xbrl_service.py" MSFT --form 10-K
```

Output files land in `USA xbrl/output/`.

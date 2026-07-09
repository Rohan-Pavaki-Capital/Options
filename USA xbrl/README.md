# USA XBRL — comparison prototype

Extracts share-based compensation (options/RSU) data for **US-listed companies**
directly from the **XBRL facts** of a SEC filing. No PDF render, no OCR, no LLM —
the numbers are exactly what the company tagged in its filing.

Built to compare against the current PDF/LLM pipeline (`/api/extract-from-edgar`).
Both produce the same JSON schema (`Anthropic/schema.py`) and the same Excel
layout (`format/json_to_excel.py`), so the workbooks are directly comparable.

## Usage

**API** (server running):

```
POST /api/extract-from-xbrl
{"ticker": "MSFT", "form": "10-K"}
```

Response is synchronous (no job polling) and includes the mapped extraction
JSON, a per-plan coverage report, and download links:

```
GET /api/xbrl/download/MSFT_10-K_xbrl_options.xlsx
GET /api/xbrl/download/MSFT_10-K_xbrl.json
```

**CLI** (no server needed):

```
.rog\Scripts\python.exe "USA xbrl\xbrl_service.py" MSFT --form 10-K
```

Outputs land in `USA xbrl/output/`.

## How to compare

1. Run the same ticker through both endpoints (`extract-from-edgar` and
   `extract-from-xbrl`).
2. Open the two workbooks side by side.
3. The `coverage` block in the XBRL response lists, per plan, which fields
   XBRL filled and which known-narrative fields it cannot fill.

## AI assembly mode (`?mode=ai`)

`GET /api/xbrl/excel/options?ticker=IBM&mode=ai` — same response format,
but runs the EXACT same AI logic as the PDF workflow (user decision
2026-07-09): `Anthropic.extract_with_claude` (identical prompts, schema,
two-pass extract+validate, rollforward checks) followed by the PDF
endpoint's reducer `core.excel_options.map_plans_to_excel` (including its
4.0 maturity default and 0.1 strike floor). The only difference from the
PDF endpoint is the input: the share-based-comp footnote text sourced from
the filing's XBRL TextBlocks instead of rendered PDF pages — which means
it also sees disclosures the PDF render misses (e.g. IBM's financials
exhibit). ~30-50s, one two-pass Sonnet extraction; requires
ANTHROPIC_API_KEY. Default `mode=rules` stays deterministic, LLM-free,
tagged-values-only.

Validation (2026-07-09): GIS mode=ai reproduces the PDF endpoint's cached
result exactly (4.4/4.0/4.0); IBM yields 4.0/7.6/3.0 — the same logic
reading IBM's actual footnote extracts the printed 7.6-year remaining
life, confirming earlier 4/4/4 outputs were the blind-spot fallback.

## What XBRL fills (exact filed values)

- Rollforward: opening/closing balance, granted, exercised, vested,
  forfeited/lapsed, exercisable at period end — current **and** prior year
- Weighted-average exercise prices and grant-date fair values
- Weighted-average remaining contractual life
- Valuation assumptions: volatility, dividend yield, risk-free rate,
  expected term
- Vesting period (when tagged as a discrete duration fact)

## Known gaps (stay null by design — this is the comparison point)

- Narrative fields: `vesting_description`, `performance_conditions`,
  plan descriptions
- Exercise price **range** tables (usually TextBlock-only in XBRL)
- `weighted_avg_share_price_at_exercise` (rarely disclosed/tagged in US filings)
- Grant-level `tranches`
- Per-plan splits when the filer tags only consolidated totals
  (PlanNameAxis missing)

## Excel twin endpoint (strict)

`GET /api/xbrl/excel/options?ticker=CAG` — same response format as
`GET /api/excel/options` (`count_mn`, `strike`, `maturity_years`, `kind`),
but **strict**: every field is the exact disclosed XBRL value or `null`.
Unlike the PDF endpoint's reducer, there is **no 4.0 maturity default and no
0.1 strike floor**. US dual-form behavior is mirrored (latest 10-Q first,
10-K fallback). Never 500s; failures return `option_plans: []` + `error`.

`maturity_years` chain — every link is a disclosed fact, never a constant.
**Remaining-life-first** (user decision 2026-07-09): options: remaining
contractual life → vesting period → unrecognized-cost recognition period;
RSU/PSU: vesting period → unrecognized-cost recognition period. Vesting also reads
self-describing custom tags like `...NumberOfAnniversariesOfGrantDate`
(IBM: "4 equal increments on the first 4 anniversaries" → 4 years), and
when vesting is tagged only as a min/max range (graded vesting), the
maximum bound = full vesting period is used (IBM RSUs: P1Y–P4Y → 4). The
recognition period (e.g. CAG's "1.7 years") is the filer's measure of the
remaining life of outstanding awards.

## Notes

- Percent facts follow the XBRL spec: decimal fractions are converted to
  percentages (0.35 → 35). Values are otherwise passed through unmodified.
- `forfeited_or_lapsed` uses the combined us-gaap tag when present; otherwise
  it is the sum of the separate forfeitures and expirations tags.
- Flow values are reported positive to match the schema convention.
- Only periods ending at the filing's period-of-report are used (annual for
  a 10-K, year-to-date for a 10-Q) — never older comparative periods; facts
  carrying extra axes (ranges, vesting tranches, equity components) are
  excluded so headline rollforward numbers aren't polluted.
- Exception: vesting-period / expected-term facts tagged per grant-year
  cohort (AwardDateAxis) are accepted ONLY when every cohort discloses the
  identical value (e.g. all cohorts P3Y) and no clean fact exists.

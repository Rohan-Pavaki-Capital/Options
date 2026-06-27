"""
Prompts for the Stage 3 Anthropic Claude Sonnet extraction.

Three prompts:
  SYSTEM_PROMPT      — defines extractor's role and rules
  EXTRACTION_PROMPT  — Pass 1: extract structured JSON
  VALIDATION_PROMPT  — Pass 2: validate Pass 1 against source
  
Updated to handle:
  - Tables (UK plc, US 10-K, IFRS style)
  - Prose disclosures (Singapore/Asian style)
  - Hybrid layouts (table + supplementary prose)
  - Multi-plan column tables
  - Per-tranche grant detail tables
  - Valuation model input tables (Black-Scholes/Monte Carlo)
"""

SYSTEM_PROMPT = """You are a financial data extraction specialist with deep expertise in share-based compensation disclosures across ALL global accounting standards and jurisdictions (IFRS 2, ASC 718, and local GAAP).

═══════════════════════════════════════════════════════════════
DATA APPEARS IN MULTIPLE FORMS — EXTRACT FROM ALL
═══════════════════════════════════════════════════════════════

Critical insight: Share-based payment data is NOT always in a table.
Companies disclose it in 4 distinct formats — sometimes mixed on the same page:

▶ FORMAT 1: STANDARD TABLE (Roll-forward)
   Column headers + numeric rows.
   Example:
     "                    LTIP    DBP    SAYE
      Opening:           4,603    341  2,595
      Granted:           1,467    150    489
      Exercised:         (248)  (139)  (596)
      Closing:           5,118    352  2,007"

▶ FORMAT 2: PROSE / PARAGRAPH DISCLOSURE (Asian/Singapore style)
   Numbers embedded in flowing text.
   Example:
     "As at 30 June 2025, the number of shares comprised in 
      contingent awards granted under the CapitaLand Investment 
      Founders Performance Share Plan 2021 is 11,306,567 
      (30 June 2024: 12,735,038), of which 301,095 
      (30 June 2024: 371,941) shares are to be cash-settled."
   
   → MUST extract: total = 11,306,567; prior_year_total = 12,735,038;
                  cash_settled = 301,095; prior_year_cash_settled = 371,941

▶ FORMAT 3: SUPPLEMENTARY PROSE (after a table)
   Critical data in sentences AFTER the main table.
   Example:
     [table with roll-forward...]
     "The options outstanding at 31 July 2024 have an exercise 
      price in the range of 1,550.0p to 2,535.0p and have a 
      weighted average contractual life of 2.7 years 
      (2023 – 3.3 years). The weighted average share price at 
      the date of exercise for share options exercised during 
      the year was 2,734.3p (2023 – 2,445.7p)."
   
   → MUST extract: range_low = 1550.0; range_high = 2535.0;
                  contractual_life = 2.7; prior_year_life = 3.3;
                  share_price_at_exercise = 2734.3

▶ FORMAT 4: TRANCHE/GRANT DETAIL TABLE
   Per-grant breakdown below the main roll-forward.
   Example:
     "Share option        Grant Price  Number    Exercise  Vesting
                              (p)      of shares    price  period
      SAYE - 1st Nov 2024   108.00    1,280,093   97.00     3
      PSP - 9th Jan 2020      0.13        7,337    0.13     3"

═══════════════════════════════════════════════════════════════
HOW TO READ EACH FORMAT
═══════════════════════════════════════════════════════════════

▶ FOR TABLES:
  1. Identify column headers (year, plan, metric type)
  2. For each row, match values to the correct column
  3. Multi-plan tables (LTIP|DBP|SAYE) → create separate plan entries
  4. Watch for parenthesized negatives: (704) means 704 (positive)
  5. Watch for "—" or "–" or blank = null
  6. Note units in column headers: "(£)", "(p)", "(pence)", "in thousands"

▶ FOR PROSE / PARAGRAPHS:
  1. Look for key phrases:
     - "number of shares comprised in contingent awards"
     - "is X (prior year: Y)"
     - "of which X are to be cash-settled"
     - "outstanding at [date]"
     - "exercise price in the range of X to Y"
     - "weighted average contractual life of X years"
     - "weighted average share price at the date of exercise was X"
     - "fair value of the awards granted was $X"
     - "the awards were valued using [Black-Scholes/Monte Carlo]"
  
  2. Parse number-with-comparative patterns:
     "X (prior year: Y)" or "X (30 June 2024: Y)" or "X (2023 – Y)"
     → Current = X, Prior year = Y
  
  3. Parse range patterns:
     "in the range of X to Y" → range_low = X, range_high = Y
     "ranged from X to Y"
     "X – Y" or "X to Y"
  
  4. Parse split patterns:
     "X, of which Y are cash-settled"
     → total = X, cash_settled = Y, equity_settled = X - Y
  
  5. Parse grant patterns:
     "X shares granted to employees of the Group and Y 
      shares granted to the employees of related corporations"
     → Capture both numbers if relevant

▶ FOR SUPPLEMENTARY PROSE (after tables):
  ALWAYS read prose AFTER tables carefully — it often contains:
  - Exercise price ranges (low/high)
  - Weighted average contractual life
  - Share price at exercise date
  - Fair value disclosures
  - Vesting condition descriptions
  - Performance metrics summary
  
  These details may not appear in the main table but ARE schema fields.

▶ FOR TRANCHE/GRANT TABLES:
  1. Each row = one tranche entry in tranches[] array
  2. Capture: grant_date, grant_price, shares_at_period_end, 
             exercise_price, vesting_period
  3. Look for grant date in row labels: "PSP - 9th January 2020"

═══════════════════════════════════════════════════════════════
WHAT YOU EXTRACT (THE SCHEMA TARGETS)
═══════════════════════════════════════════════════════════════

1. PLAN IDENTITY (from headers or section labels)
   - plan_name (preserve original)
   - plan_type (map to schema codes)
   - plan_description
   - is_cash_settled, is_nil_cost

2. ROLL-FORWARD (from tables OR prose like "outstanding X (prior Y)")
   - opening_balance, granted, exercised, lapsed/forfeited, 
     vested, closing_balance, exercisable

3. PRICE DATA (from columns OR prose like "range of X to Y")
   - weighted_avg_exercise_price + unit
   - exercise_price_range_low/high + unit
   - weighted_avg_grant_date_fair_value + unit
   - weighted_avg_share_price_at_exercise + unit

4. TIME/VESTING (from columns, separate tables, OR prose)
   - weighted_avg_remaining_contractual_life_years
   - vesting_period_years
   - performance_period_years
   - holding_period_years

5. PERFORMANCE
   - performance_conditions (short summary)
   - maximum_payout_pct

6. CONTINGENT AWARDS (typically from prose disclosure)
   - total_contingent_awards
   - contingent_cash_settled
   - contingent_equity_settled

7. VALUATION INPUTS (from Black-Scholes/Monte Carlo input tables)
   - valuation_model
   - stock_price, strike_price, volatility, dividend yield, 
     risk-free rate, expected life, fair value per option

8. TRANCHES (from per-grant detail tables)
   - One entry per grant date
   - grant_date, grant_price, shares_at_period_end,
     exercise_price, vesting_period_years, fair_value

9. PRIOR YEAR (from comparative columns OR "(prior year: X)" prose)
   - All same fields as current year

10. COMPANY METADATA
    - company_name, report_period, currency, reporting_standard

═══════════════════════════════════════════════════════════════
WHAT YOU IGNORE (DO NOT EXTRACT)
═══════════════════════════════════════════════════════════════

Even if these appear on the page, IGNORE them:

▷ ACCOUNTING POLICY TEXT
  - "The fair value of equity-settled share options granted is 
     recognised as an employee expense..."
  - "A deferred tax asset is recognised on..."
  - "Where the Company grants options over its own shares..."

▷ EXPENSE LINE ITEMS (P&L charges, not options data)
  - "Share-based payment expense: £4.5m"
  - "Total charge for the year:..."

▷ PERFORMANCE CONDITION FULL DETAILS
  - Long lists of TSR peer companies
  - Detailed ESG metric thresholds
  - Bullet lists of performance conditions
  → Only capture a SHORT summary in performance_conditions

▷ SHARE CAPITAL DATA (different from options)
  - "Shares issued during the year: 52,000"
  - "Buyback and cancellation: 1,631,263"
  - "Treasury shares held"

▷ DIRECTOR-LEVEL HOLDINGS
  - Individual named director awards
  - Personal beneficial interests

▷ CROSS-REFERENCES
  - "See note 23 for details..."
  - "Refer to the Remuneration Report..."

▷ NARRATIVE INTRODUCTIONS
  - "The Group operates a long-term incentive plan ('LTIP')..."
  - "Awards are granted at the discretion of..."
  - Plan eligibility descriptions

▷ FORWARD-LOOKING STATEMENTS
  - "Future awards may be granted..."
  - "The Committee intends to..."

▷ FOOTNOTE LETTERS AND PAGE FURNITURE
  - "(a) Represents the number of shares..."
  - Page numbers, headers like "FINANCIAL STATEMENTS / NOTES"

═══════════════════════════════════════════════════════════════
THE EXTRACTION MINDSET
═══════════════════════════════════════════════════════════════

On any given page:
  • 30-60% may be narrative/policy → IGNORE
  • 20-50% may be data in TABLES → EXTRACT
  • 10-30% may be data in PROSE → EXTRACT (this is often missed!)
  
Your job:
  ✓ Find NUMERIC FACTS wherever they live (tables, prose, footnotes)
  ✓ Match each fact to the correct schema field
  ✓ Handle BOTH tabular AND prose formats with equal skill
  ✓ Skip everything else

═══════════════════════════════════════════════════════════════
CRITICAL EXTRACTION RULES (NEVER VIOLATE)
═══════════════════════════════════════════════════════════════

1. OUTPUT FORMAT
   - Return ONLY valid JSON
   - No markdown, no explanations

2. PLAN SEPARATION
   - Each distinct plan = separate entry in plans[]
   - Multi-column tables (LTIP|DBP|SAYE) → 3 separate entries
   - Combined plans named together ("LTIP, DBP") → 1 entry

3. NUMBER NORMALIZATION
   - exercised/lapsed/forfeited stored as POSITIVE
   - (704) → 704
   - -596 → 596
   - The field name implies the direction

4. NEVER INVENT DATA
   - null for missing fields
   - "—" or "–" or blank = null
   - DON'T infer values that aren't disclosed
   - DON'T copy prior year as current year if current is blank

5. UNITS HANDLING
   - "in thousands" → store AS SHOWN, set units_label = "thousands"
     (so "4,603" in thousands stays as 4603, not 4,603,000)
   - 1,632.0p means 1632.0 PENCE (not 1.632 pounds)
   - "(£)" means pounds; "(p)" means pence
   - "$" means dollars (assume USD unless otherwise specified)

6. PROSE NUMBER EXTRACTION
   - "X (prior year: Y)" → current = X, prior_year = Y
   - "of which Y" → split component = Y
   - "in the range of X to Y" → low = X, high = Y
   - "is 11,306,567" → exact number 11306567 (remove commas)

7. PRIOR YEAR DATA
   - Side-by-side columns → populate prior_year object
   - Inline references like "(2023 – X)" or "(prior year: Y)" → prior_year
   - DON'T mix current and prior in same field

8. CONTINGENT AWARDS (Asian/Singapore RSU style)
   - For nil-cost plans (RSU, PSP, Founders PSP)
   - Use total_contingent_awards (not opening_balance)
   - Capture cash-settled / equity-settled split if disclosed
   - exercise_price stays null (correct — these are free shares)

9. TRANCHE DETAILS
   - Per-grant tables → tranches[] array
   - Each grant date row = one tranche entry
   - Capture all disclosed details per tranche

10. VALUATION INPUTS
    - Look for tables/sections with: volatility, risk-free rate,
      dividend yield, expected life, fair value
    - If per-tranche, also populate tranches[].valuation fields

11. PLAN TYPE CODES
    LTIP, PSP, RSU, PSU, SAYE, CSOP, ESOP, DEFERRED_BONUS,
    FOUNDERS_PSP, RSP, SRSOS, WARRANT, SAR, ESPP, OTHER

12. COMPANY METADATA
    - From page headers, footers, or report titles
    - report_period: "Year ended X" or "Six-month period ended X"
    - currency: detect from £/$/¥/€/S$/HK$ symbols or unit labels

13. LANGUAGE & NUMBER FORMAT (CRITICAL FOR NON-ENGLISH SOURCES)
    - ALL free-text output fields MUST be in English. If the source is in
      another language (e.g. German), TRANSLATE plan_name, plan_description,
      vesting_description, performance_conditions, valuation_model, and any
      narrative text into clear, natural English.
    - You MAY keep the original-language name in parentheses inside
      plan_description, e.g. "Stock Option Plan 2018 (Aktienoptionsplan 2018)".
    - NEVER translate, round, or reformat numbers, dates, currency codes, or
      units. Keep plan_type codes and ISO currency codes (GBP/USD/EUR/...) as-is.
    - Convert localized number formats to standard numeric form:
      German "1.234.567,89" → 1234567.89  (period = thousands, comma = decimal).
      European decimal commas ("1.234,56") become "1234.56". This is the single
      most important rule for German/European sources — get the numbers right.
    - For an English source this rule is a NO-OP: leave the already-English
      text and standard-format numbers exactly as they are.

14. AGGREGATE / COMBINED ROLL-FORWARD TABLES (NEVER DROP THE NUMBERS)
    Some issuers (common in US 10-Ks) disclose ONE combined roll-forward for all
    equity awards under a GENERIC label -- "Stock Awards", "Share-based awards",
    "Equity awards", "RSUs and PSUs" -- even though the surrounding NARRATIVE
    names several individual plans (e.g. 2024 Plan, RPSRs, RSRs, Director
    Program).
    - When the roll-forward table is NOT split per named plan, DO NOT leave the
      roll-forward null just because it does not map 1-to-1 to a narrative plan.
      Create ONE aggregate plan entry whose plan_name is the table's own label
      (e.g. "Stock Awards (aggregate)") and populate its roll-forward from the
      table. Keep the narrative-only plans as their own entries (descriptions,
      vesting, shares-available); they simply carry null roll-forwards.
    - VERTICAL MULTI-YEAR ROLL-FORWARDS: the movement rows may be stacked down a
      SINGLE value column across consecutive years, not in side-by-side columns:
        Outstanding at January 1, 2023 ... Granted ... Vested ... Forfeited ...
        Outstanding at December 31, 2023 ... Granted ... Vested ... Forfeited ...
        Outstanding at December 31, 2024 ... Granted ... Vested ... Forfeited ...
        Outstanding at December 31, 2025
      The LAST opening-to-closing block is the CURRENT year; the immediately
      preceding block is prior_year. Map opening_balance / granted / vested /
      forfeited_or_lapsed / closing_balance from the CURRENT block only.
    - VALUE COLUMN MAY BE FAIR VALUE, NOT EXERCISE PRICE: nil-cost RSU/PSU roll-
      forwards often pair the share counts with "Weighted-Average Grant Date Fair
      Value Per Share" -> weighted_avg_grant_date_fair_value (NOT exercise price;
      leave the exercise-price fields null), and "Weighted-Average Remaining
      Contractual Term" -> weighted_avg_remaining_contractual_life_years.

15. ONE PLAN ENTRY PER ACTIVITY TABLE (THE INVERSE OF RULE 14 — NEVER COLLAPSE)
    Many filings (common in US 20-Fs/10-Ks) name only ONE umbrella plan in the
    narrative (e.g. "2022 Omnibus Incentive Plan") but then disclose SEVERAL
    SEPARATE roll-forward/activity tables for different award types — e.g.
    "Equity-settled RSUs", "Cash-settled RSUs", "PSUs", "Options",
    "Restricted Shares", "Warrants" — each with its own
    opening/granted/forfeited/vested(or exercised)/closing rows.
    - Create ONE plan entry PER activity table. NEVER merge distinct award-type
      tables into a single entry, and NEVER return only the umbrella narrative
      plan with null roll-forward fields when activity tables are present.
    - plan_name: combine umbrella plan + award type, e.g.
      "2022 Omnibus Incentive Plan — Equity-settled RSUs". plan_type from the
      award type (RSU, PSU, WARRANT, ...). Set is_cash_settled = true for
      cash-settled tables.
    - HARD CHECK before returning: for EVERY roll-forward/activity table you
      saw on the source pages, some plan entry must carry that table's numbers.
      A table whose numbers appear in no plan entry means your output is WRONG —
      go back and add the missing entry.
    - Rule 14 (aggregate) applies ONLY when there is a single combined table.
      Multiple per-award-type tables → multiple entries (this rule wins).
"""


EXTRACTION_PROMPT = """Extract share-based compensation data from these financial report pages.

═══════════════════════════════════════════════════════════════
EXTRACTION WORKFLOW (FOLLOW IN ORDER)
═══════════════════════════════════════════════════════════════

STEP 1: SCAN ALL DATA FORMS
Look at every element on the page:
  □ Main tables (roll-forward, valuation inputs)
  □ Secondary tables (tranches, grants)
  □ Paragraph text with embedded numbers
  □ Footnotes with numeric content
  □ Column headers and row labels

STEP 2: IDENTIFY PLANS
Count distinct plans on the page:
  • Each table column with a plan name = 1 plan
  • Each prose disclosure with a plan name = 1 plan
  • Same plan in table + prose = 1 plan (merge data)

STEP 3: EXTRACT FROM TABLES
For each table:
  • Identify column headers (year, plan, metric type)
  • Match each cell to schema fields
  • Watch for units in headers
  • Handle parenthesized negatives as positive
  
STEP 4: EXTRACT FROM PROSE
For each paragraph with numbers:
  • Identify which plan it describes
  • Extract numeric facts using patterns:
    - "X (prior year: Y)" → current and prior
    - "in the range of X to Y" → low and high
    - "of which Y are cash-settled" → component split
    - "weighted average contractual life of X years" → time field
    - "fair value of $X" → valuation field
  
STEP 5: MERGE TABLE + PROSE DATA
If same plan has data in both table AND prose:
  • Table provides roll-forward, prices
  • Prose provides ranges, contractual life, share prices at exercise
  • Combine into ONE plan entry

STEP 6: EXTRACT TRANCHES (if present)
For per-grant tables (typically below main roll-forward):
  • Each row = one tranche entry
  • Capture grant_date, grant_price, shares_at_period_end,
    exercise_price, vesting_period

STEP 7: EXTRACT VALUATION INPUTS (if present)
For Black-Scholes/Monte Carlo tables:
  • Populate valuation_inputs object
  • If per-tranche, also populate tranches[].valuation fields

═══════════════════════════════════════════════════════════════
EXTRACTION EXAMPLES (LEARN FROM THESE)
═══════════════════════════════════════════════════════════════

═══ EXAMPLE 1: Multi-Plan Table (UK style) ═══

SOURCE:
"Number of share options                Long-term     Deferred    Save As
 In thousands                        incentive plan  bonus scheme  You Earn
 At 1 January 2024                        4,603          341       2,595
 Granted                                  1,467          150         489
 Lapsed                                    (704)           -        (481)
 Exercised                                 (248)        (139)       (596)
 At 31 December 2024                      5,118          352       2,007
 Exercisable at 31 December 2024          1,139            -           -
 Weighted average remaining 
   contractual life (years)                  7.6          1.3         2.0
 Range of exercise prices (£)                 -            -    4.68 – 9.68"

EXTRACTION:
3 separate plan entries:

Plan 1 (LTIP):
  units_label: "thousands"
  opening_balance: 4603, granted: 1467, lapsed: 704, exercised: 248,
  closing_balance: 5118, exercisable: 1139
  weighted_avg_remaining_contractual_life_years: 7.6
  exercise_price_range: null (— means not applicable)

Plan 2 (Deferred Bonus / DBP):
  units_label: "thousands"
  opening_balance: 341, granted: 150, exercised: 139,
  closing_balance: 352
  weighted_avg_remaining_contractual_life_years: 1.3

Plan 3 (SAYE):
  units_label: "thousands"
  opening_balance: 2595, granted: 489, lapsed: 481, exercised: 596,
  closing_balance: 2007
  weighted_avg_remaining_contractual_life_years: 2.0
  exercise_price_range_low: 4.68, exercise_price_range_high: 9.68
  exercise_price_range_unit: "pounds"


═══ EXAMPLE 2: US RSU Activity (with prose context) ═══

SOURCE:
"We issue restricted stock units... with 3-year cliff or 4-year graduated 
 vesting... The weighted average grant date fair value of restricted 
 stock units was $165.21, $160.91, and $208.80 in 2024, 2023, and 2022.

 Restricted Stock Unit Activity        Restricted   Grant Date
                                        Stock(a)    Fair Value(b)
 February 3, 2024                        3,796 $       171.61
 Granted                                 2,477         165.21
 Forfeited                                (377)        171.47
 Vested                                 (1,347)        167.39
 February 1, 2025                        4,549 $       169.59
 
 (a) Represents the number of shares in thousands..."

EXTRACTION:
Plan 1 (RSU):
  plan_type: "RSU"
  units_label: "thousands"
  opening_balance: 3796, granted: 2477, forfeited_or_lapsed: 377,
  vested: 1347, closing_balance: 4549
  weighted_avg_grant_date_fair_value: 169.59  (closing balance row)
  fair_value_unit: "dollars"
  vesting_description: "3-year cliff or 4-year graduated"
  is_nil_cost: true (RSUs have no exercise price)
  
  Tranches: omit unless per-grant detail shown
  (Don't extract $165.21, $160.91, $208.80 as historical averages — 
   these are aggregate per-year averages, not data points)


═══ EXAMPLE 3: Prose Disclosure (Singapore/Asian style) ═══

SOURCE:
"As at 30 June 2025, the number of shares comprised in contingent 
 awards granted under the CapitaLand Investment Founders Performance 
 Share Plan 2021 is 11,306,567 (30 June 2024: 12,735,038), of which 
 301,095 (30 June 2024: 371,941) shares are to be cash-settled. 
 The number of shares comprised 9,788,545 (30 June 2024: 11,027,620) 
 shares granted to the employees of the Group and 1,518,022 
 (30 June 2024: 1,707,418) shares granted to the employees of the 
 related corporations."

EXTRACTION:
Plan 1 (Founders Performance Share Plan):
  plan_name: "CapitaLand Investment Founders Performance Share Plan 2021"
  plan_type: "FOUNDERS_PSP"
  is_nil_cost: true
  total_contingent_awards: 11306567
  contingent_cash_settled: 301095
  contingent_equity_settled: 11005472  (11,306,567 - 301,095)
  prior_year:
    total_contingent_awards: 12735038
    contingent_cash_settled: 371941
    contingent_equity_settled: 12363097

Note: opening_balance, granted, exercised, closing_balance ALL null
      (no roll-forward disclosed — only point-in-time totals)


═══ EXAMPLE 4: Hybrid (Table + Supplementary Prose) ═══

SOURCE:
"SRSOS                              2024            2024       2023       2023
                            Weighted avg     Number of  Weighted avg  Number of
                            exercise price    options   exercise price  options
                                     (p)         No.          (p)          No.
 Outstanding at beginning      1,686.5        753,984     2,445.4     442,082
 Granted                       1,632.0        232,528     1,550.0     684,517
 Forfeited                     1,707.6       (163,423)    2,337.5    (371,508)
 Exercised                     2,268.9        (52,927)    1,892.8      (1,107)
 Outstanding at end            1,625.5        770,162     1,686.5     753,984
 Exercisable at end            2,338.2          6,767     2,528.0         356

 The options outstanding at 31 July 2024 have an exercise price in the 
 range of 1,550.0p to 2,535.0p (2023 – 1,550.0p to 2,535.0p) and have a 
 weighted average contractual life of 2.7 years (2023 – 3.3 years). The 
 weighted average share price at the date of exercise for share options 
 exercised during the year was 2,734.3p (2023 – 2,445.7p)."

EXTRACTION:
Plan 1 (SRSOS):
  plan_type: "SRSOS"
  
  From table (current year):
    opening_balance: 753984, granted: 232528, forfeited_or_lapsed: 163423,
    exercised: 52927, closing_balance: 770162, exercisable: 6767
    weighted_avg_exercise_price: 1625.5  (closing row)
    weighted_avg_exercise_price_unit: "pence"
  
  From PROSE (also for current year):
    exercise_price_range_low: 1550.0
    exercise_price_range_high: 2535.0
    exercise_price_range_unit: "pence"
    weighted_avg_remaining_contractual_life_years: 2.7
    weighted_avg_share_price_at_exercise: 2734.3
    weighted_avg_share_price_at_exercise_unit: "pence"
  
  From table (prior year columns):
    prior_year:
      opening_balance: 442082, granted: 684517, forfeited: 371508,
      exercised: 1107, closing: 753984, exercisable: 356
      weighted_avg_exercise_price: 1686.5  (closing row)
  
  From PROSE (also for prior year):
    prior_year:
      exercise_price_range_low: 1550.0
      exercise_price_range_high: 2535.0
      weighted_avg_remaining_contractual_life_years: 3.3


═══ EXAMPLE 5: Tranche Detail Table ═══

SOURCE:
"Share option           Grant Price   Number of shares    Exercise    Vesting
                            (p)         at year end       price (p)   period
                                                                      (years)
 SAYE - 1st Nov 2024     108.00         1,280,093          97.00         3
 
 PSP - 9th Jan 2020        0.13             7,337           0.13         3
 PSP - 30th Oct 2020       0.13            15,195           0.13         3
 PSP - 21st Dec 2021       0.13            23,861           0.13         3"

EXTRACTION:
Plan (SAYE):
  tranches: [
    {
      grant_date: "2024-11-01",
      grant_price: 108.00,
      grant_price_unit: "pence",
      shares_at_period_end: 1280093,
      exercise_price: 97.00,
      exercise_price_unit: "pence",
      vesting_period_years: 3
    }
  ]

Plan (PSP):
  tranches: [
    {
      grant_date: "2020-01-09",
      grant_price: 0.13, grant_price_unit: "pence",
      shares_at_period_end: 7337,
      exercise_price: 0.13, exercise_price_unit: "pence",
      vesting_period_years: 3
    },
    {
      grant_date: "2020-10-30",
      grant_price: 0.13,
      shares_at_period_end: 15195,
      exercise_price: 0.13,
      vesting_period_years: 3
    },
    {
      grant_date: "2021-12-21",
      grant_price: 0.13,
      shares_at_period_end: 23861,
      exercise_price: 0.13,
      vesting_period_years: 3
    }
  ]


═══ EXAMPLE 6: Black-Scholes Valuation Inputs ═══

SOURCE:
"                              2024              2024             2024
 Scheme description          LTIP              DBP            3yr SRSOS
 Valuation model         Monte Carlo           n/a          Black Scholes
 Grant date              24-Oct-23           14-Nov-23        22-Nov-23
 Risk free interest rate    0.0%              0.0%              4.3%
 Exercise price                —                 —            1,632.0p
 Share price at grant      2,036.0p          2,350.0p          2,378.0p
 Expected dividend yield    0.0%              5.0%              5.0%
 Expected life             3 years           4 years      3 years 2 months
 Vesting date             24-Oct-26         14-Nov-27        01-Feb-27
 Expected volatility        30%               30%               30%
 Fair value of option     1,376.6p           1,744.0p           744.0p"

EXTRACTION:
For LTIP plan (or merge into existing LTIP entry):
  valuation_model: "Monte Carlo"
  tranches: [
    {
      grant_date: "2023-10-24",
      vesting_date: "2026-10-24",
      shares_at_period_end: null,  (not shown in this table)
      exercise_price: null,
      fair_value_per_option: 1376.6,
      fair_value_unit: "pence",
      vesting_period_years: 3,
      volatility_pct: 30,
      risk_free_rate_pct: 0.0,
      dividend_yield_pct: 0.0,
      expected_life_years: 3
    }
  ]
  valuation_inputs: {  (use the most representative tranche)
    stock_price: 2036.0,
    stock_price_unit: "pence",
    strike_price: null,
    volatility_pct: 30,
    dividend_yield_pct: 0.0,
    risk_free_rate_pct: 0.0,
    expected_life_years: 3,
    fair_value_per_option: 1376.6,
    fair_value_unit: "pence"
  }


═══ EXAMPLE 7: Aggregate Stock Awards Roll-Forward (US 10-K, vertical multi-year, nil-cost) ═══

SOURCE:
"Stock Awards
 Stock award activity for the years ended December 31, 2023, 2024 and 2025
 is presented in the table below.

                                     Stock      Weighted-Average   Weighted-Average
                                    Awards       Grant Date Fair  Remaining Contractual
                                 (in thousands)  Value Per Share    Term (in years)
 Outstanding at January 1, 2023       561   $        344                 1.4
 Granted                              216            478
 Vested                              (249)           315
 Forfeited                            (29)           373
 Outstanding at December 31, 2023     499   $        417                 1.3
 Granted                              238            465
 Vested                              (221)           366
 Forfeited                            (47)           450
 Outstanding at December 31, 2024     469   $        462                 1.4
 Granted                              231            463
 Vested                              (195)           456
 Forfeited                            (59)           469
 Outstanding at December 31, 2025     446   $        464                 1.4

 [Narrative elsewhere on the page names the 2024 Plan, Restricted Performance
  Stock Rights (RPSRs) and Restricted Stock Rights (RSRs).]"

EXTRACTION:
The table is a SINGLE combined roll-forward (no per-plan split) -> create ONE
aggregate entry. The value column is GRANT DATE FAIR VALUE, not an exercise
price (nil-cost awards). Use the LAST year block (2025) as current and the
preceding block (2024) as prior_year:

Plan (Stock Awards — aggregate):
  plan_name: "Stock Awards (aggregate)"
  plan_type: "OTHER"
  is_nil_cost: true
  units_label: "thousands"
  opening_balance: 469, granted: 231, vested: 195,
  forfeited_or_lapsed: 59, closing_balance: 446
  weighted_avg_grant_date_fair_value: 464   (closing-row fair value)
  fair_value_unit: "dollars"
  weighted_avg_remaining_contractual_life_years: 1.4
  weighted_avg_exercise_price: null   (nil-cost — no exercise price)
  prior_year:
    opening_balance: 499, granted: 238, vested: 221,
    forfeited_or_lapsed: 47, closing_balance: 469
    weighted_avg_grant_date_fair_value: 462
    weighted_avg_remaining_contractual_life_years: 1.4

The individually NAMED plans (2024 Plan, RPSRs, RSRs) remain SEPARATE entries
carrying their descriptions / vesting / shares-available; their roll-forward
fields stay null because the table does not break the movements out per plan.
(Arithmetic check: 469 + 231 - 195 - 59 = 446 ✓)


═══ EXAMPLE 8: One Umbrella Plan, MULTIPLE Activity Tables (US 20-F) ═══

SOURCE (spread across several pages):
"[Narrative, earlier page:] Under the 2022 Omnibus Incentive Plan, employees,
 officers and directors may be granted cash-based and share-based awards...

 [Later page:] A summary of the Company's Equity-settled Restricted Share
 Units activity for the year ended December 31, 2025 is as follows:
                                          Number of    Weighted Average Grant
                                            RSUs       Date Fair Value per RSU
 Unvested RSUs as of December 31, 2024   6,665,511   $        5.50
 Granted                                 3,279,410           10.09
 Forfeited                                (642,150)           6.57
 Vested                                 (3,052,043)           5.63
 Unvested RSUs as of December 31, 2025   6,250,728   $        7.73

 A summary of the Company's Cash-settled RSUs activity for the year ended
 December 31, 2025 is as follows:
                                          Number of    Weighted Average Grant
                                       Cash-settled RSUs  Date Fair Value
 Unvested as of December 31, 2024           94,279   $        5.38
 Granted                                    37,016            9.73
 Forfeited                                  (1,672)           5.44
 Vested                                    (41,324)           5.28
 Unvested as of December 31, 2025           88,299   $        7.25

 [Further pages: a PSU activity table and a warrants table...]"

EXTRACTION:
WRONG (common failure): one entry "2022 Omnibus Incentive Plan" with only
plan_description filled and ALL roll-forward fields null. The tables were
provided — collapsing them loses the data.

CORRECT — one plan entry per activity table:

Plan 1 (Equity-settled RSUs):
  plan_name: "2022 Omnibus Incentive Plan — Equity-settled RSUs"
  plan_type: "RSU"
  is_nil_cost: true, is_cash_settled: false
  opening_balance: 6665511, granted: 3279410,
  forfeited_or_lapsed: 642150, vested: 3052043,
  closing_balance: 6250728
  weighted_avg_grant_date_fair_value: 7.73  (closing row)
  fair_value_unit: "dollars"
  plan_description / vesting_description: from the umbrella narrative

Plan 2 (Cash-settled RSUs):
  plan_name: "2022 Omnibus Incentive Plan — Cash-settled RSUs"
  plan_type: "RSU"
  is_nil_cost: true, is_cash_settled: true
  opening_balance: 94279, granted: 37016,
  forfeited_or_lapsed: 1672, vested: 41324,
  closing_balance: 88299
  weighted_avg_grant_date_fair_value: 7.25
  fair_value_unit: "dollars"

Plan 3 (PSUs), Plan 4 (Warrants), ...: same pattern — one entry per table.

(Arithmetic check: 6,665,511 + 3,279,410 - 642,150 - 3,052,043 = 6,250,728 ✓)

═══════════════════════════════════════════════════════════════
JSON SCHEMA TO POPULATE
═══════════════════════════════════════════════════════════════

{schema}

═══════════════════════════════════════════════════════════════
SOURCE PAGES
═══════════════════════════════════════════════════════════════

{text}

═══════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════

Return ONLY the JSON object. Start with {{ and end with }}.

Scan EVERY paragraph and EVERY table.
Numbers may live in prose, not just tables.
Look for "(prior year: X)", "in the range of X to Y", "is X (Y: Z)" patterns.
Extract from ALL formats: tables, prose, footnotes, hybrid layouts."""


VALIDATION_PROMPT = """You previously extracted share-based compensation data from financial report pages. Now validate and correct your extraction.

═══════════════════════════════════════════════════════════════
SOURCE TEXT (ORIGINAL PAGES)
═══════════════════════════════════════════════════════════════
{text}

═══════════════════════════════════════════════════════════════
YOUR EXTRACTED JSON (CHECK THIS)
═══════════════════════════════════════════════════════════════
{extracted}

═══════════════════════════════════════════════════════════════
VALIDATION CHECKS (PERFORM IN ORDER)
═══════════════════════════════════════════════════════════════

1. ROLL-FORWARD ARITHMETIC
   For each plan, verify:
     opening_balance + granted - exercised - forfeited_or_lapsed ≈ closing_balance
   Tolerance: 1 unit or 1% (rounding)
   If math fails, re-read source — likely a number was misread from wrong column.

2. COMPLETENESS CHECK
   - Every PLAN with disclosed data captured in plans[]?
   - Multi-plan tables (LTIP|DBP|SAYE) split into SEPARATE entries?
   - All ROWS of multi-row tables read?
   - Prior year COLUMNS populated where present?

3. PROSE DATA EXTRACTION CHECK ★ MOST IMPORTANT
   Re-read every PARAGRAPH on the page. Did you miss:
   - "X (prior year: Y)" patterns?
   - "in the range of X to Y"?
   - "weighted average contractual life of X years"?
   - "weighted average share price at the date of exercise was X"?
   - "of which Y are cash-settled"?
   - "is X (date: Y)" disclosures?
   - "fair value of the awards granted was $X"?
   
   These often contain CRITICAL data that's NOT in any table.

4. SUPPLEMENTARY PROSE CHECK
   After every table, check for follow-up sentences. Did you extract:
   - exercise_price_range from prose?
   - weighted_avg_remaining_contractual_life from prose?
   - weighted_avg_share_price_at_exercise from prose?

5. CONTINGENT AWARDS PROSE CHECK
   If page has Asian/Singapore prose disclosures:
   - Did you extract total_contingent_awards?
   - Did you extract contingent_cash_settled?
   - Did you populate prior_year from "(prior date: Y)" patterns?

6. TRANCHE EXTRACTION CHECK
   If page has per-grant tables (with dates like "1st Nov 2024"):
   - Did you create tranches[] entries?
   - Did you capture grant_date, exercise_price, shares_at_period_end,
     vesting_period for each row?

7. VALUATION INPUTS CHECK
   If page has Black-Scholes / Monte Carlo input tables:
   - Did you capture valuation_model?
   - Did you populate valuation_inputs object?
   - Did you also populate per-tranche valuation fields if shown?

8. COLUMN ALIGNMENT
   - Did you mix current year and prior year columns?
   - Did you mix data between different plan columns?

9. UNITS CORRECTNESS
   - "(p)" = pence; "(£)" = pounds; "$" = dollars
   - 1632.0p means 1632.0 PENCE (not 1.632 pounds)
   - "in thousands" → store AS SHOWN, set units_label = "thousands"
   - Currency normalized (GBP, USD, SGD, etc.)

10. SIGN CONVENTION
    - exercised, forfeited_or_lapsed, vested are POSITIVE
    - (1,088) → store as 1088

11. NULL VS ZERO
    - "—" or "–" or blank = null
    - Explicit "0" = 0
    - Don't change null to 0

12. CHECK FOR HALLUCINATED DATA
    - Any values not present in source?
    - Any fields populated where source shows "—"?
    - REMOVE invented data (set to null)

13. CHECK FOR MISSED DATA
    - Any tables not extracted?
    - Any prose paragraphs with numbers not extracted?
    - Any plans mentioned but missing from plans[]?

14. FILTER NON-OPTIONS DATA
    - Did you accidentally extract SHARE CAPITAL data? REMOVE
    - Did you extract EPS dilution? REMOVE
    - Did you extract DIRECTOR individual holdings? REMOVE
    - Did you extract SBP expense (P&L line)? REMOVE

═══════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════

Return the CORRECTED JSON object only.
- If extraction was perfect, return unchanged
- If errors found, return the corrected version
- Maintain exact schema structure
- No explanations, no markdown
- Start with {{ and end with }}

Pay SPECIAL attention to data hidden in PROSE/PARAGRAPHS that may have 
been missed in Pass 1. This is the most common extraction failure."""
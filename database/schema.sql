-- Options Extractor schema for NeonDB (Postgres)
-- All statements are idempotent — safe to run multiple times.
--
-- Design:
--   * Frequently-queried fields become typed columns (cheap to filter/index).
--   * raw_json still holds the full payload — nothing is lost.
--   * Nested arrays (tranches) get their own child table.

-- ─────────────────────────────────────────────────────────────────────
-- extractions: one row per uploaded PDF
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS extractions (
    id                  SERIAL PRIMARY KEY,
    company_name        TEXT,
    report_period       TEXT,
    currency            TEXT,
    reporting_standard  TEXT,
    source_pdf          TEXT,
    total_plans         INTEGER,
    extraction_cost_usd NUMERIC(12, 6),
    raw_json            JSONB NOT NULL,
    excel_file          BYTEA NOT NULL,
    excel_filename      TEXT  NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Additive columns for existing installs (PG 9.6+).
ALTER TABLE extractions ADD COLUMN IF NOT EXISTS total_pdf_pages         INTEGER;
ALTER TABLE extractions ADD COLUMN IF NOT EXISTS pages_processed         INTEGER[];
ALTER TABLE extractions ADD COLUMN IF NOT EXISTS extraction_model        TEXT;
ALTER TABLE extractions ADD COLUMN IF NOT EXISTS classifier_model        TEXT;
ALTER TABLE extractions ADD COLUMN IF NOT EXISTS extraction_mode         TEXT;
ALTER TABLE extractions ADD COLUMN IF NOT EXISTS validation_warnings_cnt INTEGER;

CREATE INDEX IF NOT EXISTS idx_extractions_company ON extractions (company_name);
CREATE INDEX IF NOT EXISTS idx_extractions_created ON extractions (created_at DESC);


-- ─────────────────────────────────────────────────────────────────────
-- plans: one row per equity plan inside an extraction
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plans (
    id                                  SERIAL PRIMARY KEY,
    extraction_id                       INTEGER NOT NULL REFERENCES extractions(id) ON DELETE CASCADE,
    plan_name                           TEXT,
    plan_type                           TEXT,
    plan_description                    TEXT,

    -- Roll-forward (current period)
    opening_balance                     NUMERIC,
    granted                             NUMERIC,
    exercised                           NUMERIC,
    forfeited_or_lapsed                 NUMERIC,
    vested                              NUMERIC,
    closing_balance                     NUMERIC,
    exercisable_at_period_end           NUMERIC,

    -- Prices / fair value
    weighted_avg_exercise_price         NUMERIC,
    weighted_avg_exercise_price_unit    TEXT,
    weighted_avg_grant_date_fair_value  NUMERIC,
    fair_value_unit                     TEXT,
    exercise_price_range_low            NUMERIC,
    exercise_price_range_high           NUMERIC,
    exercise_price_range_unit           TEXT,
    weighted_avg_remaining_contractual_life_years NUMERIC,
    weighted_avg_share_price_at_exercise NUMERIC,
    weighted_avg_share_price_at_exercise_unit    TEXT,

    -- Plan attributes
    units_label                         TEXT,
    vesting_period_years                NUMERIC,
    performance_period_years            NUMERIC,
    holding_period_years                NUMERIC,
    vesting_description                 TEXT,
    performance_conditions              TEXT,
    is_nil_cost                         BOOLEAN,
    is_cash_settled                     BOOLEAN,

    -- Contingent awards (Singapore-style disclosures)
    total_contingent_awards             NUMERIC,
    contingent_cash_settled             NUMERIC,
    contingent_equity_settled           NUMERIC,

    -- Valuation inputs (Black-Scholes / Monte Carlo)
    valuation_model                     TEXT,
    valuation_volatility_pct            NUMERIC,
    valuation_risk_free_rate_pct        NUMERIC,
    valuation_dividend_yield_pct        NUMERIC,
    valuation_expected_life_years       NUMERIC,
    valuation_stock_price               NUMERIC,
    valuation_stock_price_unit          TEXT,
    valuation_strike_price              NUMERIC,
    valuation_fair_value_per_option     NUMERIC,
    valuation_fair_value_unit           TEXT,

    -- Prior-year mirrored roll-forward (for YoY queries)
    prior_opening_balance               NUMERIC,
    prior_granted                       NUMERIC,
    prior_exercised                     NUMERIC,
    prior_vested                        NUMERIC,
    prior_forfeited_or_lapsed           NUMERIC,
    prior_closing_balance               NUMERIC,
    prior_weighted_avg_exercise_price   NUMERIC,
    prior_weighted_avg_grant_date_fair_value NUMERIC,

    raw_json                            JSONB
);

-- Additive columns for existing installs.
ALTER TABLE plans ADD COLUMN IF NOT EXISTS exercisable_at_period_end           NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS weighted_avg_exercise_price_unit    TEXT;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS fair_value_unit                     TEXT;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS exercise_price_range_low            NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS exercise_price_range_high           NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS exercise_price_range_unit           TEXT;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS weighted_avg_remaining_contractual_life_years NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS weighted_avg_share_price_at_exercise NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS weighted_avg_share_price_at_exercise_unit TEXT;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS units_label                         TEXT;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS performance_period_years            NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS holding_period_years                NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS vesting_description                 TEXT;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS performance_conditions              TEXT;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS total_contingent_awards             NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS contingent_cash_settled             NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS contingent_equity_settled           NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS valuation_volatility_pct            NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS valuation_risk_free_rate_pct        NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS valuation_dividend_yield_pct        NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS valuation_expected_life_years       NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS valuation_stock_price               NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS valuation_stock_price_unit          TEXT;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS valuation_strike_price              NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS valuation_fair_value_per_option     NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS valuation_fair_value_unit           TEXT;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS prior_opening_balance               NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS prior_granted                       NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS prior_exercised                     NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS prior_vested                        NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS prior_forfeited_or_lapsed           NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS prior_closing_balance               NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS prior_weighted_avg_exercise_price   NUMERIC;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS prior_weighted_avg_grant_date_fair_value NUMERIC;

CREATE INDEX IF NOT EXISTS idx_plans_extraction ON plans (extraction_id);
CREATE INDEX IF NOT EXISTS idx_plans_name       ON plans (plan_name);
CREATE INDEX IF NOT EXISTS idx_plans_type       ON plans (plan_type);


-- ─────────────────────────────────────────────────────────────────────
-- plan_tranches: one row per tranche inside a plan
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plan_tranches (
    id                       SERIAL PRIMARY KEY,
    plan_id                  INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    tranche_index            INTEGER NOT NULL,
    grant_date               TEXT,
    shares_granted           NUMERIC,
    shares_at_period_end     NUMERIC,
    grant_price              NUMERIC,
    grant_price_unit         TEXT,
    exercise_price           NUMERIC,
    exercise_price_unit      TEXT,
    vesting_period_years     NUMERIC,
    fair_value_per_option    NUMERIC,
    fair_value_unit          TEXT,
    raw_json                 JSONB
);

CREATE INDEX IF NOT EXISTS idx_tranches_plan ON plan_tranches (plan_id);

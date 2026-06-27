OUTPUT_SCHEMA = """{
  "company_name": "string|null",
  "report_period": "string|null",
  "currency": "string|null",
  "reporting_standard": "string|null — IFRS|US_GAAP|local_GAAP",
  "plans": [
    {
      "plan_name": "string — in English (translate if source is non-English); keep original-language name in plan_description if useful",
      "plan_type": "LTIP|PSP|RSU|PSU|SAYE|CSOP|ESOP|DEFERRED_BONUS|FOUNDERS_PSP|RSP|SRSOS|WARRANT|SAR|ESPP|OTHER",
      "plan_description": "string|null — short description if disclosed",
      "is_cash_settled": "boolean|null — true if cash-settled, false if equity-settled",
      "is_nil_cost": "boolean|null — true if no exercise price (RSU/PSP), false if has exercise price",
      
      "units_label": "string|null — 'thousands' or 'millions' if applicable",
      
      "opening_balance": "number|null",
      "granted": "number|null",
      "exercised": "number|null — POSITIVE even if source shows (negative)",
      "forfeited_or_lapsed": "number|null — POSITIVE",
      "vested": "number|null",
      "settled_in_cash": "number|null — for cash-settled awards",
      "closing_balance": "number|null",
      "exercisable_at_period_end": "number|null",
      
      "weighted_avg_exercise_price": "number|null",
      "weighted_avg_exercise_price_unit": "string|null — pence|pounds|dollars|cents|euros|etc",
      "exercise_price_range_low": "number|null",
      "exercise_price_range_high": "number|null",
      "exercise_price_range_unit": "string|null",
      
      "weighted_avg_grant_date_fair_value": "number|null",
      "fair_value_unit": "string|null",
      "weighted_avg_share_price_at_exercise": "number|null",
      "weighted_avg_share_price_at_exercise_unit": "string|null",
      
      "weighted_avg_remaining_contractual_life_years": "number|null",
      "vesting_period_years": "number|null",
      "vesting_description": "string|null — e.g., 'three-year cliff', 'graded over 4 years'",
      "performance_period_years": "number|null",
      "performance_conditions": "string|null — TSR, EPS, ROCE, etc.",
      "holding_period_years": "number|null — post-vesting holding requirement",
      "maximum_payout_pct": "number|null — e.g., 300 for 300% of baseline",
      
      "total_contingent_awards": "number|null",
      "contingent_cash_settled": "number|null",
      "contingent_equity_settled": "number|null",
      
      "valuation_model": "string|null — Black-Scholes|Monte Carlo|Binomial|Lattice",
      "valuation_inputs": {
        "stock_price": "number|null",
        "stock_price_unit": "string|null",
        "strike_price": "number|null",
        "strike_price_unit": "string|null",
        "expiration_years": "number|null",
        "expected_life_years": "number|null",
        "volatility_pct": "number|null",
        "dividend_yield_pct": "number|null",
        "risk_free_rate_pct": "number|null",
        "calculated_call_value": "number|null",
        "calculated_put_value": "number|null",
        "fair_value_per_option": "number|null",
        "fair_value_unit": "string|null"
      },
      
      "tranches": [
        {
          "grant_date": "string|null — YYYY-MM-DD or 'DD-MMM-YY' as shown",
          "vesting_date": "string|null",
          "expiration_date": "string|null",
          "shares_at_period_end": "number|null",
          "shares_granted": "number|null",
          "grant_price": "number|null",
          "grant_price_unit": "string|null",
          "exercise_price": "number|null",
          "exercise_price_unit": "string|null",
          "fair_value_per_option": "number|null",
          "fair_value_unit": "string|null",
          "vesting_period_years": "number|null",
          "valuation_model": "string|null",
          "volatility_pct": "number|null",
          "risk_free_rate_pct": "number|null",
          "dividend_yield_pct": "number|null",
          "expected_life_years": "number|null"
        }
      ],
      
      "prior_year": {
        "opening_balance": "number|null",
        "granted": "number|null",
        "exercised": "number|null",
        "forfeited_or_lapsed": "number|null",
        "vested": "number|null",
        "closing_balance": "number|null",
        "exercisable_at_period_end": "number|null",
        "weighted_avg_exercise_price": "number|null",
        "weighted_avg_exercise_price_unit": "string|null",
        "weighted_avg_grant_date_fair_value": "number|null",
        "weighted_avg_remaining_contractual_life_years": "number|null",
        "total_contingent_awards": "number|null"
      }
    }
  ]
}"""
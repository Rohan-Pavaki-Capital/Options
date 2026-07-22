# OperatingMargine

FastAPI service that returns an LLM-judged **target pre-tax operating margin**
(EBIT % of sales, year 10) and **convergence year** for a Damodaran-style DCF
valuation workbook. Stateless, no database.

## Setup

```bash
cd "Operating Margine"
python -m venv .venv
.venv\Scripts\activate        # Windows (use `source .venv/bin/activate` on Unix)
pip install -r requirements.txt
copy .env.example .env         # then fill in your keys
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TOGETHER_API_KEY` | yes | Together AI API key |
| `FIRECRAWL_API_KEY` | no | Only needed if you pass `context_url` in requests |
| `MODEL_NAME` | no | Defaults to `meta-llama/Llama-3.3-70B-Instruct-Turbo` |

## Run

Two ways to reach the endpoint:

1. **Mounted in the main backend (default).** `backend.py` at the project root
   mounts `margin_route.py` on the same server as the options pipeline,
   Balance Sheet, etc. — it appears in `/docs` under the **Operating Margin**
   section as `POST /api/operating-margin`.

2. **Standalone**, from this folder:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Standalone paths: `POST /operating-margin`, health check `GET /health`.

## Classification — Peter Lynch's six types

| Type | Trigger (hard thresholds) | Target-margin rule |
|---|---|---|
| `SLOW_GROWER` | revenue CAGR < 8%, stable positive margins | median of last 5–10y historical margins |
| `STALWART` | revenue CAGR 8–15%, consistently positive | median of last 5y, ±2pts for margin trend |
| `FAST_GROWER` | revenue CAGR > 15% (hist. or forward) | profitable → current margin drifting to anchor; pre-profit + EPS positive by 3rd forecast year → anchor (US default); EPS negative all 3 years → ≤0.5× anchor, confidence low |
| `CYCLICAL` | alternating expansion/contraction in 10y history | mid-cycle = 10y average incl. troughs (never latest, never peak) |
| `TURNAROUND` | sustained company-specific losses + visible restructuring | 0.5–0.7× anchor, confidence low/medium |
| `ASSET_PLAY` | value driven by balance-sheet assets, not operations | `past_avg_margin` unchanged, `margin_driver` = false |

Tie-break when thresholds overlap: CYCLICAL > TURNAROUND > FAST_GROWER >
STALWART > SLOW_GROWER. Loss-making commercial-stage biotech with ramping
revenue is FAST_GROWER, not TURNAROUND. Every response carries
`margin_driver` (false only for ASSET_PLAY) and `convergence_year` = 3.

Payload sketches per type: SLOW_GROWER/STALWART/CYCLICAL only need
`historicals` showing the pattern (low growth / 8–15% growth / boom-bust
margins) with positive forward EPS; TURNAROUND needs a revenue collapse in
`historicals`; ASSET_PLAY needs `past_avg_margin` set and asset dominance
noted via `context_url`. The full worked example below is the FAST_GROWER
pre-profit case (the key test).

## Example request (biotech: EPS negative in years 1-2, positive in year 3 → FAST_GROWER at anchor)

```bash
curl -X POST http://localhost:8000/operating-margin \
  -H "Content-Type: application/json" \
  -d '{
    "ticker": "XBIO",
    "company_name": "Example Bio Inc",
    "country": "United States",
    "industry_us": "Drugs (Biotechnology)",
    "industry_global": "Drugs (Biotechnology)",
    "currency": "USD",
    "units": "millions",
    "historicals": [
      {"period": "TTM",  "revenue": 480.0, "op_income": -95.0,  "op_margin": -0.198},
      {"period": "FY24", "revenue": 410.0, "op_income": -120.0, "op_margin": -0.293},
      {"period": "FY23", "revenue": 265.0, "op_income": -160.0, "op_margin": -0.604},
      {"period": "FY22", "revenue": 140.0, "op_income": -185.0, "op_margin": -1.321},
      {"period": "FY21", "revenue": 45.0,  "op_income": -170.0, "op_margin": -3.778}
    ],
    "forward_estimates": [
      {"period": "FY25", "revenue_est": 560.0,  "eps_est": -0.85},
      {"period": "FY26", "revenue_est": 690.0,  "eps_est": -0.30},
      {"period": "FY27", "revenue_est": 850.0,  "eps_est": 0.42},
      {"period": "FY28", "revenue_est": 1010.0, "eps_est": 1.15}
    ],
    "rd_adjusted_margins": [-0.05, -0.12, -0.31],
    "revenue_cagr_5y": 0.62,
    "revenue_cagr_10y": null,
    "nol_carryforward": 610.0,
    "past_avg_margin": -1.24,
    "damodaran_us_margin": 0.2237,
    "damodaran_global_margin": 0.1985
  }'
```

EPS turns positive in the 3rd forecast year (FY27) with revenue growing >15%,
so FAST_GROWER sub-case (b) applies — expected response: `classification` =
FAST_GROWER, `target_margin` == `damodaran_us_margin` (0.2237),
`convergence_year` == 3 (always; fixed in code), `margin_driver` == true:

```json
{
  "status": "ok",
  "ticker": "XBIO",
  "classification": "FAST_GROWER",
  "target_margin": 0.2237,
  "convergence_year": 3,
  "confidence": "medium",
  "margin_driver": true,
  "damodaran_anchor_used": 0.2237,
  "comps_used": ["Alnylam ~$2.2B revenue ~15% margin", "BioMarin ~$2.8B revenue ~20% margin"],
  "rationale": "EPS turns positive in FY27 (0.42) while revenue grows from 480 TTM to 850 ...",
  "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
  "timestamp": "2026-07-22T10:00:00+00:00"
}
```

## Optional qualitative context

Add `"context_url": "https://ir.examplebio.com/overview"` to the request body.
The page is scraped via Firecrawl, truncated to 3,000 characters, and appended
to the prompt as QUALITATIVE CONTEXT. Any scrape failure is silent — the
judgment proceeds without it.

## Behavior notes

- LLM: Together AI (OpenAI-compatible), temperature 0, max_tokens 500,
  JSON-forced output.
- `convergence_year` is ALWAYS 3 — fixed in code (validator), never an LLM
  decision.
- Validation (deterministic — never relies on the LLM obeying the prompt):
  - bounds `-0.05 ≤ target_margin ≤ 0.60`, and at most 1.5× the *effective*
    anchor = max(US, Global, `mature_state_anchor`) — so a distorted
    Damodaran anchor (e.g. biotech 0.0108) can't auto-reject correct answers
  - enum checks (six Lynch types), `margin_driver`/`anchor_bypassed` bools
  - DISTORTED ANCHOR RULE: if `damodaran_us_margin < 0.05`, classification
    is FAST_GROWER and `mature_state_anchor` is provided, `target_margin`
    must be ≥ 0.5 × `mature_state_anchor` × 0.6 (≈0.09 for a 0.295 anchor)
  - comp consistency: comp margins are parsed from `comps_used` (`~NN%`);
    the target must lie within [min comp − 10pts, max comp + 10pts]
  - comp scale: comp revenues parsed from `comps_used` (`~$N.NB`/`~$NNNM`);
    every parseable comp must be within 0.3×–3× of the subject's TTM revenue
  - all-negative forward EPS: target may not exceed 0.6× the effective anchor
  - ASSET_PLAY: `target_margin` must equal `past_avg_margin`, `margin_driver`
    must be false

  On any reject the LLM is retried ONCE with "YOUR PREVIOUS RESPONSE WAS
  REJECTED BECAUSE: ..." appended; a second failure returns
  `status: "error"`, `error_code: "llm_rule_violation"` with the failed
  check in the detail — callers (VBA) should leave the target cell (B25)
  untouched on error.

## Regression test

`python test_ions.py` (offline validator tests always run; the live
end-to-end test runs only when `TOGETHER_API_KEY` is set). The key case is
the real IONS failure: anchors 0.0108 / −0.0043, `mature_state_anchor`
0.295, EPS turning positive FY2028 — must return FAST_GROWER,
`anchor_bypassed` true, target in [0.12, 0.30], `convergence_year` 3, all
comps within 0.3×–3× of TTM revenue 1,058.2.

**If the live regression test only passes intermittently, the model is too
weak for this rubric.** Switch `MODEL_NAME` in `.env` to a stronger
JSON-disciplined model on Together AI, e.g. `deepseek-ai/DeepSeek-V3` or
`Qwen/Qwen2.5-72B-Instruct-Turbo`, and re-run until it passes consistently.
- Each request logs ticker, classification, target margin, and latency.

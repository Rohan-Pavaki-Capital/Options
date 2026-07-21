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

## Example request (biotech: negative margins, growing revenue → RAMPING)

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
    "rd_adjusted_margins": [-0.05, -0.12, -0.31],
    "revenue_cagr_5y": 0.62,
    "revenue_cagr_10y": null,
    "consensus_growth_5y": 0.28,
    "nol_carryforward": 610.0,
    "past_avg_margin": -1.24,
    "damodaran_us_margin": 0.2237,
    "damodaran_global_margin": 0.1985
  }'
```

Expected shape of the response (values illustrative):

```json
{
  "status": "ok",
  "ticker": "XBIO",
  "classification": "RAMPING",
  "target_margin": 0.20,
  "convergence_year": 8,
  "confidence": "medium",
  "damodaran_anchor_used": 0.2237,
  "comps_used": ["Vertex Pharmaceuticals ~35%", "Alnylam ~15%", "BioMarin ~20%"],
  "rationale": "Revenue is scaling rapidly while margins remain negative ...",
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
- Validation: bounds check (`-0.05 ≤ target_margin ≤ 0.60`, and at most
  1.5× the higher Damodaran anchor), `convergence_year` 1–10, enum checks.
  On failure the LLM is retried once with the validation error appended;
  a second failure returns `status: "error"` with code and detail.
- Each request logs ticker, classification, target margin, and latency.

# Balance_sheet — 10-Q/10-K Balance-Sheet Standardizer

Extracts a company's balance sheet from a 10-Q/10-K PDF and maps it into a
**fixed Damodaran-style template** as JSON, with the standardized numbers
**reconciled (tallied) against the filing's printed totals**.

## Pipeline (4 stages)

1. **Locate** (`pdf_locator.py`, PyMuPDF) — finds the balance-sheet page(s) by
   title variants ("CONDENSED CONSOLIDATED BALANCE SHEET", "STATEMENTS OF
   FINANCIAL POSITION", …), captures the matched page + the next page
   (extending by one if "Total assets" is present but the equity/liabilities
   total is not), and exports them to a small temp PDF.
2. **Parse** (`parser.py`, LlamaParse) — sends only the captured page(s) to
   LlamaParse in markdown mode; returns raw markdown.
3. **Standardize** (`standardizer.py`, Together AI LLM) — maps every line into
   the fixed bucket schema. Strict JSON output; numbers copied exactly as
   printed (only `$` and `,` stripped, never scaled); most-recent period
   column only. Invalid output is re-prompted once.
4. **Tally** (`tally.py`, pure code) — sums the buckets and compares against
   the filing's printed `Total assets` / `Total liabilities` (tolerance 1).
   On imbalance the LLM is re-called **once** with the exact gap; if still
   unbalanced, the JSON is returned with `balanced=false` and a `warnings`
   entry naming the gap — never silently accepted.

`preferred_stock` / `mezzanine_equity` are captured but sit outside the
liabilities tally (they're outside the printed "Total liabilities").

## Setup

```bash
pip install -r requirements.txt   # project-root requirements (adds llama-parse)
```

Add to the project-root `.env` (never hardcoded):

```
LLAMAPARSE_API_KEY=llx-...        # LlamaParse / LlamaCloud (PDF -> markdown)
TOGETHER_API_KEY=...              # Together AI (standardization LLM)
# optional override; defaults to meta-llama/Llama-3.3-70B-Instruct-Turbo
BALANCE_SHEET_MODEL=meta-llama/Llama-3.3-70B-Instruct-Turbo
```

If `TOGETHER_API_KEY` is absent the pipeline **fails loudly** — the
standardization step is never silently skipped. Note LlamaParse only converts
PDF pages to markdown; the schema-mapping reasoning is done by the chat LLM.
Provider/model live in `config.py` (`LLM_BASE_URL` / `LLM_MODEL`).

## Run

From Python:

```python
from Balance_sheet import run_pipeline
result = run_pipeline(r"test_data\dhc_10q.pdf")
```

CLI test script:

```bash
python -m Balance_sheet.test_sample test_data/dhc_10q.pdf
```

API — wired into the main backend (recommended): one call with the same
input fields as `/api/fetch-filing` fetches the company's most recent filing
and returns the final standardized JSON:

```bash
# main backend (backend.py)
curl -X POST http://localhost:8000/api/balance-sheet/standardize \
  -H "Content-Type: application/json" \
  -d '{"ticker": "DHC", "company_name": "Diversified Healthcare Trust", "country": "USA"}'
# optional: "form": "10-Q" (US only; default = truly latest 10-Q/10-K)
```

Standalone app (local PDF path or upload, no fetch):

```bash
uvicorn Balance_sheet.api:app --port 8010
# by path on disk:
curl -X POST -F "pdf_path=C:/path/to/filing.pdf" http://localhost:8010/api/balance-sheet/standardize
# or upload:
curl -X POST -F "file=@filing.pdf" http://localhost:8010/api/balance-sheet/standardize
```

## Output shape

```json
{
  "company": "", "period": "", "currency": "",
  "unit_label": "thousands",
  "source_pages": [4, 5],
  "assets":      { "non_current": { ... }, "current": { ... } },
  "liabilities": { "non_current": { ... }, "current": { ... },
                   "preferred_stock": 0, "mezzanine_equity": 0 },
  "filing_totals": { "total_assets": 0, "total_liabilities": 0 },
  "tally": { "sum_assets": 0, "sum_liabilities": 0,
             "assets_balanced": false, "liabilities_balanced": false },
  "warnings": []
}
```

`unit_label` is for labelling only — numbers are **never** scaled; they are
exactly as printed in the filing.

## Documented example (test expectation)

For a **Diversified Healthcare Trust 10-Q** (dollars in thousands):

| Check | Expected |
|---|---|
| `filing_totals.total_assets` | 4,267,552 |
| `filing_totals.total_liabilities` | 2,647,133 |
| sum of asset buckets | 4,267,552 (`assets_balanced = true`) |
| sum of liability buckets | 2,647,133 (`liabilities_balanced = true`) |

Known trap: **accrued interest (26,078) must land in `other_liabilities`**,
otherwise liabilities sum to 2,621,055 and the tally fails — the Stage-4
re-prompt passes the exact gap back to the LLM to catch and fix this.

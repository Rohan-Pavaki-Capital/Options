# Balance Sheet Module — Simple Documentation (BS.md)

This document explains, in simple language, what the `Balance_sheet` folder does, how it works, and what each file is for.

---

## 1. What does this module do?

You give it a company's filing report (a 10-Q or 10-K PDF, or a European annual report). It:

1. **Finds** the balance-sheet page(s) inside the PDF.
2. **Reads** those pages and converts the table into text (markdown).
3. **Sorts** every line of the balance sheet into a fixed set of standard buckets (a Damodaran-style template) using an AI model.
4. **Checks the math** — it adds up the buckets and compares the sum with the totals printed in the filing ("Total assets", "Total liabilities"). If the sums don't match, it asks the AI to fix its mapping. If it still doesn't match, it honestly reports the gap — it never invents a number to force the totals to match.

The final answer is a JSON object with the standardized balance sheet, with all values converted to **millions**.

There is also a second, simpler mode (`raw_excel.py`) that skips the AI completely and just copies the balance sheet into an Excel file exactly as printed.

---

## 2. The pipeline — 4 stages

```
PDF file
   |
   v
Stage 1: LOCATE   (pdf_locator.py)  - find the balance-sheet pages
   |
   v
Stage 2: PARSE    (parser.py)       - convert those pages to markdown text
   |
   v
Stage 3: STANDARDIZE (standardizer.py) - AI maps each line into fixed buckets
   |
   v
Stage 4: TALLY    (tally.py)        - code checks the sums against printed totals
   |
   v
Final JSON (values in millions)
```

### Stage 1 — Locate (`pdf_locator.py`)

- Opens the PDF with **PyMuPDF** and looks for a page whose text contains a balance-sheet title, e.g. "Condensed Consolidated Balance Sheet", "Statements of Financial Position", or French titles like "Bilan consolidé" (for French filings).
- Confirms the page is the real statement (not a table of contents or a summary page) by checking that it also contains "Total assets" and a total-equity/liabilities line.
- If the statement spans two pages (assets on one page, liabilities on the next), it captures both.
- Saves just those page(s) into a small temporary PDF — so later stages only work on 1–2 pages, not the whole filing.
- Also reads two things **directly from the page text in code** (so the AI can never get them wrong):
  - the **printed totals** ("Total assets", "Total liabilities") — these become the reference the math check uses;
  - the **unit label** ("in thousands" / "in millions" / "En millions d'euros").
- Handles tricky filings: companies with unlabeled total rows (APA, L'Oréal), columns printed oldest-year-first (NOS), and French-language reports (Eiffage).

### Stage 2 — Parse (`parser.py`)

- Sends the small temp PDF (only the captured pages) to **LlamaParse**, a service that turns PDF tables into clean markdown text.
- Returns the raw markdown. Note: LlamaParse only converts the layout — it does no financial reasoning.

### Stage 3 — Standardize (`standardizer.py`)

- First, **code** (not the AI) extracts the line items from the markdown table: each label plus its value from the most-recent column, with all "Total ..." rows removed. Each line is tagged with the section it sits in ([ASSET] / [LIABILITY] / [EQUITY], current / non-current).
- This checklist is given to an AI model (via **Together AI**), which only has to decide *which bucket each label belongs in* — it never re-reads numbers from the table, so it cannot hallucinate values or pick the wrong year's column.
- The AI must return strict JSON in the fixed schema (see section 3). Numbers are copied exactly as printed — only "$" and commas are stripped, values are never scaled or recalculated by the AI.
- If several lines belong in one bucket, the AI writes them as a string like `"100 + 200"` and the **code** does the addition.
- If the AI's output is broken (invalid JSON, missing keys, invented keys), it is asked once more to fix it. Two failures in a row = error.
- There are **two prompts**: the default US one, and a European/IFRS one (`prompt_eu.py`) used when the caller passes `region="eu"`. The EU prompt knows IFRS habits: non-current items listed first, "Total equity and liabilities" instead of "Total liabilities", captions like "Provisions" and "Trade and other receivables", etc.

### Stage 4 — Tally (`tally.py`)

Pure code — no AI involved in the checking itself.

- Converts every bucket value to a real number.
- Adds up each side:
  - asset buckets + memo cash + goodwill + intangibles should equal printed **Total assets**;
  - liability buckets + memo long-term debt should equal printed **Total liabilities**.
- A small gap is allowed for rounding (filings round every printed line), but nothing bigger.
- If a side doesn't balance, code diagnoses the likely cause (a line counted twice, or a line missed) and sends the AI a **correction note with the exact gap**. This retry loop runs at most `TALLY_MAX_RETRIES` times (default 2).
- It also catches **single-count violations**: the same printed line value used in two different buckets.
- If it still doesn't balance after the retries, the result is returned with `balanced = false` and a clear warning naming the gap. **A number is never made up to force the totals to tie.**
- Extra safety guards run at the end:
  - a current "Taxes payable" line dropped into the wrong bucket is moved automatically (a same-side move, so the totals don't change);
  - ordinary equity lines wrongly placed into `preferred_stock` / `mezzanine_equity` are removed;
  - a warning fires if everything was dumped into an "other_*" bucket.
- Finally, **all values are converted to millions** (thousands ÷ 1,000, billions × 1,000). The original scale is kept in `original_unit_label`. If the scale wasn't found, values are left as-is with a warning.

---

## 3. The fixed schema (the buckets)

Every balance-sheet line must land in exactly **one** of these buckets. The keys are fixed — no new keys may be invented. The AI matches lines by **meaning**, not exact words (e.g. "Operating right-of-use assets" → `lease_assets`).

**Assets — non-current:**
`lease_assets`, `real_estate_assets`, `investment_assets`, `investment_in_other`, `assets_held_for_sale`, `asset_from_discontinued_business`, `pension_assets`, `other_assets`, `ppe`

**Assets — current:**
`lease_assets`, `inventory`, `accounts_trade_receivable`, `tax`, `other_current_assets`

**Liabilities — non-current:**
`pension`, `lease_liabilities`, `deferred_rev_and_tax`, `other_liabilities`

**Liabilities — current:**
`debt`, `lease_liabilities`, `accounts_trade_payable`, `deferred_rev_and_tax`, `other_current_liabilities`

**Memo (kept OUT of the buckets, but counted in the math check):**

- `cash_and_marketable_securities` — cash + cash equivalents + current marketable securities
- `goodwill` — goodwill only
- `intangibles` — intangible assets (net), excluding goodwill
- `long_term_debt` — non-current interest-bearing debt (bonds, term loans, ...)

These four sit in a separate `memo_excluded` object. The reconciliation is:

```
sum(asset buckets) + cash + goodwill + intangibles == printed Total assets
sum(liability buckets) + long_term_debt            == printed Total liabilities
```

**Special fields:** `preferred_stock` and `mezzanine_equity` are captured but sit **outside** the liabilities math check (they are outside the printed "Total liabilities" in the filing).

**Equity is never mapped anywhere** — common stock, retained earnings, treasury stock, etc. all stay out.

---

## 4. Output shape

```json
{
  "company": "", "period": "", "currency": "",
  "unit_label": "in millions",
  "source_pages": [4, 5],
  "assets":      { "non_current": { ... }, "current": { ... } },
  "liabilities": { "non_current": { ... }, "current": { ... },
                   "preferred_stock": 0, "mezzanine_equity": 0 },
  "memo_excluded": { "cash_and_marketable_securities": 0, "goodwill": 0,
                     "intangibles": 0, "long_term_debt": 0 },
  "filing_totals": { "total_assets": 0, "total_liabilities": 0 },
  "tally": { "sum_assets": 0, "sum_liabilities": 0,
             "assets_balanced": false, "liabilities_balanced": false },
  "warnings": []
}
```

- `source_pages` — which filing pages were used (1-based).
- `tally.assets_balanced` / `liabilities_balanced` — did the math check pass?
- `warnings` — every problem is named here in plain text; nothing fails silently.
- All values are in **millions** in the final output; `original_unit_label` records the filing's native scale when a conversion happened.

---

## 5. Files in this folder

| File | What it does |
|---|---|
| `__init__.py` | Package entry — exposes `run_pipeline`. |
| `config.py` | All settings: API keys from `.env`, model choice, balance-sheet title variants, the fixed bucket keys, the rounding tolerance, and the empty output template. |
| `pdf_locator.py` | Stage 1 — finds the balance-sheet pages, exports them to a temp PDF, reads printed totals and unit label from the page text. |
| `parser.py` | Stage 2 — sends the temp PDF to LlamaParse and returns markdown. |
| `standardizer.py` | Stage 3 — extracts the line-item checklist in code, builds the prompt, calls the AI, validates/repairs the JSON. |
| `prompt_eu.py` | The European/IFRS version of the Stage-3 prompt (used when `region="eu"`). |
| `tally.py` | Stage 4 — math check, gap diagnosis, correction messages, safety guards, final conversion to millions. |
| `pipeline.py` | The orchestrator — runs Stages 1→4, wires the retry loop, merges the best result when a retry fixes one side but breaks the other. |
| `api.py` | Small standalone FastAPI app to run the pipeline on a local PDF (path or upload). |
| `raw_excel.py` | Alternative mode: Stages 1–2 only, then writes the balance sheet to Excel **exactly as printed** — no AI, no standardizing. |
| `test_sample.py` | Test script — runs known sample filings (DHC, Apple, CME, NIKE) and checks the documented expected values. |
| `README.md` | The original technical README. |

---

## 6. Setup

1. Install requirements (from the project root):

   ```bash
   pip install -r requirements.txt
   ```

2. Add these keys to the project-root `.env` file (never hardcoded):

   ```
   LLAMAPARSE_API_KEY=llx-...     # LlamaParse (PDF -> markdown)
   TOGETHER_API_KEY=...           # Together AI (the standardizing model)
   # optional model override:
   BALANCE_SHEET_MODEL=deepseek-ai/DeepSeek-V4-Pro
   # optional retry-count override:
   BALANCE_SHEET_TALLY_RETRIES=2
   ```

   If `TOGETHER_API_KEY` is missing, the pipeline **stops with a clear error** — the standardization step is never silently skipped.

---

## 7. How to run

**From Python:**

```python
from Balance_sheet import run_pipeline
result = run_pipeline(r"test_data\dhc_10q.pdf")          # US filing
result = run_pipeline(r"path\to\eu_report.pdf", region="eu")  # European filing
```

**Test script (runs the known samples and checks expected values):**

```bash
python -m Balance_sheet.test_sample                      # all samples
python -m Balance_sheet.test_sample test_data/dhc_10q.pdf  # one file
```

**Through the main backend (recommended) — asynchronous job:**

The full run takes 30–90 seconds, so the endpoint returns a `job_id` immediately and you poll for the result.

```bash
# 1) start the job
curl -X POST http://localhost:8000/api/balance-sheet/standardize \
  -H "Content-Type: application/json" \
  -d '{"ticker": "DHC", "company_name": "Diversified Healthcare Trust", "country": "USA"}'
# -> 202 {"job_id": "<id>", "status": "pending"}

# 2) poll until status is no longer "pending"
curl "http://localhost:8000/api/balance-sheet/status?job_id=<id>"
# done:  {"status": "done", ...full standardized JSON...}
# error: {"status": "error", "error": "...", "error_code": "..."}
```

Note: jobs live in memory inside the backend process, so the app must run with **one worker** (`uvicorn backend:app --workers 1`). With multiple workers the poll could hit a process that doesn't know the job.

**Standalone app (local PDF, no fetching):**

```bash
uvicorn Balance_sheet.api:app --port 8010
curl -X POST -F "pdf_path=C:/path/to/filing.pdf" http://localhost:8010/api/balance-sheet/standardize
# or upload the file:
curl -X POST -F "file=@filing.pdf" http://localhost:8010/api/balance-sheet/standardize
```

---

## 8. Key design principles (why it is built this way)

- **Code is the source of truth for numbers.** Printed totals, the unit label, the source pages, and the line-item values are all read by code. The AI only decides *which bucket a label belongs in*.
- **Never invent numbers.** Every value in the output is a printed line value (or a code-computed sum of printed values). If the totals don't tie, the result says so honestly instead of plugging the difference.
- **Never scale numbers during mapping.** Values are copied exactly as printed; the one conversion (to millions) happens at the very end, after the math check has already passed in the filing's own units.
- **Every line counted exactly once.** A line goes into one bucket or one memo field — never two. Code actively detects double-counting and asks the AI to fix it.
- **Fail loudly.** Missing API key, unfound balance sheet, unbalanced totals — everything produces a clear error or warning, never a silent skip.

---

## 9. Known tricky cases (covered by tests)

| Sample | Trap it guards against |
|---|---|
| DHC 10-Q | Accrued interest (26,078) must land in `other_liabilities`, or liabilities don't tally. REIT property stays in `real_estate_assets`, not `ppe`. |
| Apple 10-Q | PP&E was once counted in `ppe` AND inside `other_assets` — the double-count diagnosis catches this. |
| CME 10-Q | Clearing-house performance-bond collateral appears on **both** sides (~165 billion each way); the matching asset must be mapped too, and it is **current**. |
| NIKE 10-Q | Current portion of debt (999) vs long-term debt must not be lumped together; the filing prints no "Total liabilities" line, so it is derived from printed "Total liabilities & equity" minus printed equity. |
| European filings (BMW, L'Oréal, Eiffage, NOS) | IFRS wording, French language, unlabeled total rows, columns printed oldest-first — all handled in Stage 1 and the EU prompt. |

# Balance_sheet — Balance-Sheet Standardization Pipeline

Extracts a company's balance sheet from a 10-Q/10-K filing PDF and maps it into a
**fixed Damodaran-style template** as JSON, with the standardized numbers
**reconciled (tallied) against the filing's printed totals**. Built for equity-analyst
use: numbers are copied exactly as printed — never scaled, converted, or rounded.

- Package: [`Balance_sheet/`](Balance_sheet/)
- One-call API (wired into the main backend): `POST /api/balance-sheet/standardize`
- Core entry point: `Balance_sheet.run_pipeline(pdf_path) -> dict`

---

## 1. Workflow overview

```
  {ticker, company_name, country}                    a PDF already on disk
                 |                                            |
                 v                                            |
   /api/fetch-filing routing (backend.py)                     |
   fetches the MOST RECENT filing PDF                         |
   (US: latest of 10-Q/10-K; per-market                       |
    logic for 13 other markets)                               |
                 |                                            |
                 +----------------------+---------------------+
                                        v
              +--------------------------------------------------+
              | STAGE 1  pdf_locator.py          (PyMuPDF, code) |
              |  find the balance-sheet page(s), export temp PDF |
              |  + read the PRINTED totals from the page text    |
              +--------------------------------------------------+
                                        v
              +--------------------------------------------------+
              | STAGE 2  parser.py               (LlamaParse)    |
              |  parse ONLY those pages to markdown (tables)     |
              +--------------------------------------------------+
                                        v
              +--------------------------------------------------+
              | STAGE 3  standardizer.py         (Together LLM)  |
              |  code extracts the line-item checklist;          |
              |  LLM classifies each line into a fixed bucket    |
              +--------------------------------------------------+
                                        v
              +--------------------------------------------------+
              | STAGE 4  tally.py + pipeline.py  (pure code)     |
              |  exact sums vs printed totals (tolerance 1)      |
              |  unbalanced -> diagnosis -> ONE targeted         |
              |  LLM re-prompt -> per-side best-of merge         |
              |  still unbalanced -> warnings + diagnosis        |
              +--------------------------------------------------+
                                        v
                            final standardized JSON
```

**Design principle — the LLM never does arithmetic.** Code extracts the printed
line values and the printed totals, code computes every sum, and code checks the
reconciliation. The LLM's only job is *classification*: which printed line belongs
in which bucket.

---

## 2. Folder structure

```
Balance_sheet/
  __init__.py        exposes run_pipeline
  config.py          .env loading, keys, model, title variants, tolerance, fixed schema
  pdf_locator.py     Stage 1: page finder + temp PDF export + printed-totals reader
  parser.py          Stage 2: LlamaParse wrapper (async-safe) -> markdown
  standardizer.py    Stage 3: line-item extraction, prompts, LLM call, JSON repair/validation
  tally.py           Stage 4: coercion, exact sums, balance booleans, gap diagnosis, sanity guard
  pipeline.py        orchestrates stages 1-4, re-prompt + per-side merge, never crashes
  api.py             standalone FastAPI app (local path / upload input)
  README.md          package-level quick reference
  test_sample.py     documented acceptance tests (DHC + Apple)
```

The one-call endpoint lives in the main `backend.py` (see §7), reusing the exact
`/api/fetch-filing` routing via the shared `_fetch_filing_pdf()` helper.

---

## 3. Setup

```bash
pip install -r requirements.txt   # project-root requirements (adds llama-parse)
```

`.env` in the project root (loaded with python-dotenv — keys are never hardcoded):

```
LLAMAPARSE_API_KEY=llx-...   # LlamaParse / LlamaCloud (PDF -> markdown)
TOGETHER_API_KEY=...         # Together AI (standardization LLM)
# optional model override (defaults to meta-llama/Llama-3.3-70B-Instruct-Turbo)
BALANCE_SHEET_MODEL=meta-llama/Llama-3.3-70B-Instruct-Turbo
```

- Missing `TOGETHER_API_KEY` → the pipeline **fails loudly**; standardization is
  never silently skipped.
- Missing `LLAMAPARSE_API_KEY` → Stage 2 fails loudly with a clear message.
- Provider/base-URL/model are constants in `config.py` (`LLM_BASE_URL`, `LLM_MODEL`)
  so the LLM can be swapped. Note: `BALANCE_SHEET_MODEL` is deliberately separate
  from the options pipeline's `TOGETHER_MODEL`.

---

## 4. The four stages in detail

### Stage 1 — Locate the balance-sheet pages (`pdf_locator.py`, PyMuPDF)

1. Scan every page's text (case-insensitive) for the title variants in
   `config.TITLE_VARIANTS`:
   `CONDENSED CONSOLIDATED BALANCE SHEET`, `CONSOLIDATED BALANCE SHEET`,
   `BALANCE SHEET`, `STATEMENTS OF FINANCIAL POSITION`,
   `CONSOLIDATED STATEMENTS OF FINANCIAL POSITION`.
2. Capture the matched page **plus the next page** (balance sheets often span two).
3. A title match without `Total assets` nearby is treated as a table-of-contents
   hit and skipped.
4. If `Total assets` is present but no equity/liabilities total line is
   (`Total liabilities and shareholders/stockholders...`, `Total equity`, ...),
   **extend by one more page**.
5. Export the captured pages to a small temporary PDF (only these pages go to
   LlamaParse — cheap and clean) and return 1-based page numbers for traceability.

**`extract_printed_totals(captured_text)`** also reads the filing's printed
`Total assets` / `Total liabilities` directly from the page text (first number
after the label = most-recent column; `Total liabilities and .../,` excluded).
These code-read values **override whatever the LLM transcribes** — the
reconciliation reference never depends on LLM transcription. If a filing has no
explicit `Total liabilities` line, the LLM's value is kept.

### Stage 2 — Parse to markdown (`parser.py`, LlamaParse)

- Sends only the 1-3 captured pages, `result_type="markdown"` (table-friendly).
- Async-safe: LlamaParse's sync `load_data()` silently returns nothing inside a
  plain worker thread (e.g. FastAPI's `run_in_threadpool`), so the wrapper drives
  `aload_data()` with an explicitly created event loop (`_run_async`) that works
  from any context — plain script, worker thread, or inside a running loop.
- Empty markdown raises; the import is lazy so the rest of the package works even
  if `llama-parse` is not installed.
- Note: LlamaParse output is **non-deterministic** — the same PDF can yield
  slightly different markdown between runs.

### Stage 3 — Standardize (`standardizer.py`, Together AI LLM)

The user message the LLM receives contains, in order:

1. **The fixed target schema** (exact JSON shape — bucket keys are fixed, no new
   keys allowed).
2. **NUMBERS rules** — no digit-group commas; when several lines map into one
   bucket, write the printed values joined by `' + '` (e.g. `"other_assets": 100
   + 200`) and **the caller computes the exact sum**; every number must be copied
   character-for-character from the most-recent column; never repeat a line's
   value in a second bucket.
3. **The line-item checklist** — `extract_line_items(markdown)` parses the
   markdown table rows *in code*: label + first numeric cell (= most-recent
   column), `(x)` handled as negative, any row whose label starts with `total`
   skipped. When ≥5 items are found, they are given to the LLM as the exact
   values to bucket. This is the key reliability mechanism: the model
   classifies labels — it does not re-read the table, so wrong-column reads,
   hallucinated values, and subtotal mapping are structurally prevented.
4. **MAPPING HINTS** for lines with no named bucket: cash/short-term
   investments/prepaids → `other_current_assets`; goodwill/intangibles/deferred
   tax assets → `other_assets`; right-of-use assets → `lease_assets`; long-term
   debt (net of current portion) → `non_current.other_liabilities` (there is no
   non-current debt bucket); Land/Buildings/accumulated-depreciation blocks stay
   **together in exactly one bucket** (`real_estate_assets` for REITs including
   the negative depreciation, `ppe` otherwise); subtotal/total rows are never
   mapped.
5. **The balance-sheet markdown** itself (for period/currency/unit context).

The system prompt (fixed) carries the standardization rules: most-recent column
only; numbers exactly as printed (strip only `$` and `,`); every line into
exactly one bucket, leftovers into the closest `other_*`; current vs non-current
by the filing's own sub-headers (judgement for unclassified REIT/bank sheets);
all interest-bearing debt tranches combined into `debt`; equity never mapped
into liability buckets; and the **SINGLE-BUCKET RULE** — every source line
contributes to exactly one bucket, `other_*` holds only lines not captured by a
specific bucket, never a subtotal and its components, self-verify sums before
returning.

Output handling:
- Markdown fences / stray prose stripped (outermost `{...}` kept).
- `_repair_json_numbers()` deterministically fixes the two invalid-JSON number
  forms the model emits: unquoted digit-group commas (`1,234,567`) and literal
  `a + b + c` chains (summed exactly in code).
- Validation checks every fixed key exists and **rejects invented bucket keys**.
- Invalid output → **one** re-prompt with the parse error; a second failure
  surfaces as a Stage-3 error in `warnings`.

### Stage 4 — Tally, diagnosis, self-correction (`tally.py` + `pipeline.py`, code)

1. **Coercion** — every bucket value forced to int/float. Allowed cleaning is
   stripping `$` and `,` only; quoted `"a + b"` chains are summed exactly;
   anything non-numeric becomes 0 **with a warning**.
2. **Printed-totals override** — Stage 1's code-read totals replace the LLM's
   `filing_totals` when available.
3. **Exact sums** —
   `sum_assets` = all asset buckets; `sum_liabilities` = non-current + current
   liability buckets. `preferred_stock` / `mezzanine_equity` are captured but
   sit **outside** the liabilities sum (they are outside the filing's printed
   `Total liabilities`). Balanced = |sum − printed| ≤ `TALLY_TOLERANCE` (= 1).
4. **Gap diagnosis** (per unbalanced side), attached as `tally["diagnosis"]`
   (a list — both sides can fail):

   ```json
   { "side": "assets", "gap": 50016,
     "likely_double_counted_bucket": "ppe", "bucket_value": 50116,
     "type": "double_count" }
   ```

   - `gap > 0` (over-count) → double-count signature: scan the **specific**
     buckets for a value ≈ |gap| (within `max(tolerance, 1% of gap)` — printed
     values can differ slightly from the gap). A hit names the suspect bucket.
   - `gap < 0` (under-count) → a line was not mapped.
5. **One targeted re-prompt** — the LLM gets its previous JSON plus a message
   built from the diagnosis, e.g. *"Assets summed to 421,098 but printed Total
   Assets is 371,082 (over by 50,016). This ~= ppe (50,116), so that amount
   appears in TWO buckets..."* — with instructions to make the **smallest
   possible edit**, copy every other bucket unchanged, and never invent numbers.
   Never more than one re-prompt.
6. **Per-side best-of merge** — asset and liability buckets are disjoint, so the
   pipeline keeps, per side, whichever run (first vs retry) lands **closer to its
   printed total**. A retry that fixes one side can never regress the other.
7. **Honest failure** — if still unbalanced: `assets_balanced` /
   `liabilities_balanced` stay `false`, `warnings` names the exact gap and the
   diagnosis, and `tally["diagnosis"]` stays in the output. Imbalance is never
   silently accepted, and no bucket is ever force-set in code to make totals
   match — correction happens only by re-mapping.
8. **Sanity guard** (`sanity_check_other_buckets`) — if an `other_*` bucket holds
   >60% of its side's printed total while **no specific bucket is filled** (the
   signature of the LLM dumping everything, or a subtotal, into `other_*`), a
   warning is added. Warning only — never an error.

Every stage is wrapped: any failure returns the JSON template with `warnings`
(and an `error` field) instead of crashing. Captured pages and markdown length
are logged for debugging; `source_pages` in the output is set by code, not the LLM.

---

## 5. The fixed target schema

Every balance-sheet line maps into exactly one of these buckets (keys are FIXED):

| Section | Buckets |
|---|---|
| Assets — non-current | `lease_assets`, `real_estate_assets`, `investment_assets`, `investment_in_other`, `assets_held_for_sale`, `asset_from_discontinued_business`, `pension_assets`, `other_assets`, `ppe` |
| Assets — current | `lease_assets`, `inventory`, `accounts_trade_receivable`, `tax`, `other_current_assets` |
| Liabilities — non-current | `pension`, `lease_liabilities`, `deferred_rev_and_tax`, `other_liabilities` |
| Liabilities — current | `debt`, `lease_liabilities`, `accounts_trade_payable`, `deferred_rev_and_tax`, `other_current_liabilities` |
| Equity-adjacent | `preferred_stock`, `mezzanine_equity` (filled only if explicitly shown; excluded from the liabilities tally) |

### Output JSON shape

```json
{
  "company": "", "period": "", "currency": "",
  "unit_label": "",              // "thousands"/"millions" — labelling ONLY, never used to scale
  "source_pages": [],            // 1-based pages used (set by code)
  "assets":      { "non_current": { ... }, "current": { ... } },
  "liabilities": { "non_current": { ... }, "current": { ... },
                   "preferred_stock": 0, "mezzanine_equity": 0 },
  "filing_totals": { "total_assets": 0, "total_liabilities": 0 },   // printed values (code-read)
  "tally": {
    "sum_assets": 0, "sum_liabilities": 0,
    "assets_balanced": false, "liabilities_balanced": false,
    "diagnosis": [ /* present only when a side is unbalanced */ ]
  },
  "warnings": []
}
```

---

## 6. Hard rules (DO NOT)

- Do **not** convert or scale units — numbers exactly as printed; only `$` and
  `,` are ever stripped.
- Do **not** use anything but the most-recent period column.
- Do **not** drop any balance-sheet line — everything maps into a bucket
  (leftovers into the closest `other_*`, once).
- Do **not** map equity lines into liability buckets.
- Do **not** invent schema keys (validation rejects them).
- Do **not** double-count: one source line → exactly one bucket; never a
  subtotal and its components.
- Do **not** loop the correction re-prompt more than once.
- Do **not** fix a tally by force-setting bucket values in code — only by
  correcting the mapping.

---

## 7. How to run

### One call, input fields → final JSON (main backend)

Same inputs as `/api/fetch-filing`; fetches the most recent filing, runs the
pipeline, returns the JSON:

```bash
curl -X POST http://localhost:8000/api/balance-sheet/standardize \
  -H "Content-Type: application/json" \
  -d '{"ticker": "DHC", "company_name": "Diversified Healthcare Trust", "country": "USA"}'
# optional: "form": "10-Q" | "10-K"  (US only; default = truly latest of the two)
```

Caveats: the call blocks through fetch + parse + LLM; through a Cloudflare
quick-tunnel the ~100s edge limit applies (call directly for slow markets).
The backend runs without `--reload` — restart it after code changes.

### Standalone app (local PDF path or upload, no fetch)

```bash
uvicorn Balance_sheet.api:app --port 8010
curl -X POST -F "pdf_path=C:/path/to/filing.pdf" http://localhost:8010/api/balance-sheet/standardize
curl -X POST -F "file=@filing.pdf"               http://localhost:8010/api/balance-sheet/standardize
```

### Python

```python
from Balance_sheet import run_pipeline
result = run_pipeline(r"test_data\dhc_10q.pdf")
```

### Tests

```bash
python -m Balance_sheet.test_sample                 # runs all known samples present on disk
python -m Balance_sheet.test_sample path\to\a.pdf   # run one file
```

---

## 8. Documented acceptance tests

Both pass live (exit code 0), including all bucket-level assertions.

**Diversified Healthcare Trust 10-Q** (`test_data/dhc_10q.pdf`, $ thousands):

| Check | Expected |
|---|---|
| `filing_totals.total_assets` | 4,267,552 |
| `filing_totals.total_liabilities` | 2,647,133 |
| both sides | balanced |

Known trap: accrued interest (26,078) must land in `other_liabilities`, or
liabilities sum to 2,621,055 and the tally fails.

**Apple 10-Q, March 28 2026** (`test_data/aapl_10q.pdf`, $ millions) — the
double-counting regression case:

| Check | Expected |
|---|---|
| `sum_assets` / `sum_liabilities` | 371,082 / 264,591, both balanced |
| `ppe` | 50,116 |
| `investment_assets` | 78,088 |
| `other_assets` | 98,764 (**not** 148,780 — that value means PP&E was double-counted) |

Known trap: PP&E used to be counted in `ppe` AND folded into `other_assets`
(over-count +50,016); the single-bucket rule, the code-extracted line-item
checklist, and the diagnosis re-prompt prevent/self-correct it.

---

## 9. Known limitations

1. **The tally proves the sums, not the sorting.** A line in the wrong bucket
   that doesn't change the side total (e.g. current vs non-current swapped)
   still reports balanced. Spot-check buckets the first time you run a new
   company type.
2. **LlamaParse is non-deterministic**, and the line-item checklist requires a
   proper markdown table. If parsing yields a non-table layout, the checklist is
   skipped and mapping falls back to raw markdown — the weaker mode.
3. **Coverage tested:** US 10-Q (REIT, big tech), Canadian IFRS retailer. Banks,
   insurers, and filings with real preferred/mezzanine equity are untested; IFRS
   annual reports outside 10-Q/10-K scope (e.g. Novo) partially map and warn
   honestly rather than fail.
4. **Tests hit live APIs** (EDGAR, LlamaParse, Together) — they cost money and
   are not CI-able as-is; there is no mocked offline regression test for the LLM
   stage.
5. **No result caching** — every call pays the full fetch + parse + LLM cost.
6. Occasional unbalanced runs on hard layouts remain possible; when they happen
   the JSON says so explicitly (`balanced=false` + warnings + diagnosis) instead
   of pretending.

## 10. Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `Stage 2 ... LLAMAPARSE_API_KEY is missing` | Add the key to `.env`. |
| `Stage 3 ... TOGETHER_API_KEY is missing` | Add the key to `.env` (never silently skipped). |
| `LlamaParse returned empty markdown` | Usually an async-context issue (fixed by `_run_async`) or a scanned/no-text page. |
| `No balance-sheet page found` | None of the title variants matched a page that also contains "Total assets" (non-English filings are out of scope). |
| Warning `Over by X ~= <bucket> (Y); likely double-counted...` | Diagnosis of a double-count that survived the one re-prompt — inspect that bucket vs the `other_*` buckets. |
| Warning `Under by X; a line was not mapped` | A printed line is missing from every bucket. |
| Warning `Sanity check: other_* = ... and no specific buckets are filled` | The model likely dumped a subtotal (or everything) into `other_*`. |
| New route 404s on the live backend | The backend runs without `--reload` — restart it. |

# Fallback & Error Messages

Every message the app shows the user when an extraction **fails**, **abstains**, or finds **no pages** — with the scenario that triggers it and the recommended action.

## How errors reach the user

There are two display paths, both rendered in the red banner at the top of the input screen:

1. **Input-validation / fetch-time errors (HTTP 4xx/5xx)** — raised by the API endpoint *before or while* starting a job. The frontend shows `errData.detail` (`App.jsx`). The job never starts.
2. **Job failures (during processing)** — the background pipeline raises an exception; the outer handler records `update_job(status="failed", error=str(e)[:500])` ([backend.py:1439](backend.py#L1439)). The frontend polls, sees `status="failed"`, and shows `data.error` (truncated to **500 chars**), defaulting to **`"Extraction failed"`** if empty ([App.jsx:48](Frontend/src/App.jsx#L48)).

---

## 1. Input validation (HTTP 400 — shown instantly, no job created)

| Message | Scenario | Tab(s) |
|---|---|---|
| `Only PDF files are accepted` | Uploaded file is not a `.pdf` | Upload |
| `File too large (max 100 MB)` | Uploaded PDF exceeds 100 MB | Upload |
| `ticker is required` | EDGAR / scrape-test submitted with empty ticker | US · EDGAR |
| `company_name or ticker is required` | Both fields empty | Diamond, Singapore, Testing |
| `ticker or company_name is required` | Both fields empty | UK, Denmark, Korea, Brazil, Taiwan, Canada, China, India, Malaysia, Thailand |
| `ticker (IDX code, e.g. BBCA) is required` | Empty ticker (IDX has no name resolver) | Indonesia |
| `ticker, companyId or company_name is required` | All empty | Israel |
| `ticker, stock code or company_name is required` | All empty | Hong Kong |
| `company_name, LEI, ISIN or ticker is required` | All empty | EU · ESEF |

**Action:** fill the required field(s) and resubmit.

---

## 2. Company-resolution failures (HTTP 404 — fetch could not identify the issuer)

| Message | Scenario |
|---|---|
| `Could not find a UK company for '<input>': <reason>` | Companies House had no match for the ticker/name |
| `Could not find a <Market> company for '<input>': <reason>` | Same pattern for other dedicated markets when the resolver finds no match (e.g. Korea/DART, Brazil/CVM, Taiwan/TWSE) |
| `EU search failed: <reason>` (HTTP 502) | The EU ESEF autocomplete/search backend errored |

**Action:** check the ticker/name spelling; try the company's full legal name; or use the Upload tab.

---

## 3. Diamond / Singapore scraper failures (job fails during Stage 0)

These come from the universal IR-scraper (`diamond_route.py` + `ir_resolve_proto.py` + `ir_fetch_proto.py`). The user sees them prefixed with `Diamond fetch failed:`.

| Message | Scenario | Tab |
|---|---|---|
| `Diamond fetch failed: could not confidently identify the company's IR site (best guess was <url>, but confidence was too low to trust). Refusing to extract from a possibly-wrong company — try the per-market tab or the Upload tab.` | The resolver found a candidate site but **could not verify** it belongs to this exact company (wrong-entity / hallucination guard). It abstains rather than fetch a possibly-wrong company. | Diamond, Singapore |
| `Diamond fetch failed: IR-scraper: could not resolve an IR site` | No candidate site at all (no Wikidata / Clearbit / search hit). | Diamond, Singapore |
| `Diamond fetch failed: IR-scraper: no gate-passing annual report at <ir_url>` | The IR site was found, but no downloadable annual-report PDF passed the gate (too short < 40pp, wrong document type, interim/quarterly only, or no share-based-payment content). | Diamond, Singapore |
| `Diamond fetch failed: Could not find an annual report on this company's investor-relations site. Please use the Upload tab to submit the PDF directly. (details: …)` | **Singapore only** — scraper missed and the US-EDGAR fallback is **disabled** for SGX issuers (so it never returns a wrong US filing). | Singapore |
| `Diamond fetch failed: Diamond could not fetch a report: <attempt errors>` | Generic: every tier (dedicated source → IR-scraper → EDGAR fallback) failed; the message lists each attempt's error. | Diamond |

**Singapore-specific note:** an *unknown* SGX code entered with **no company name** abstains with the resolver confidence `ABSTAIN (unknown SGX code — enter the company name)`. Entering the **full company name** (with or without the code) is the reliable fix.

**Action:** add/correct the company name; pick the company's dedicated market tab; or use the Upload tab.

---

## 4. Dedicated-market fetch failures (job fails during Stage 0)

Each market's fetcher raises a `... fetch failed: <reason>` (or `... resolve failed: <reason>`) which the user sees verbatim. One row per source:

| Message prefix | Source |
|---|---|
| `EDGAR fetch failed: <reason>` | US · SEC EDGAR |
| `Canada (SEC MJDS) fetch failed: <reason>` | Canada (40-F/20-F/10-K) |
| `Companies House fetch failed: <reason>` | United Kingdom |
| `Denmark (CVR) fetch failed: <reason>` | Denmark |
| `EU (ESEF) resolve failed: <reason>` / `EU (ESEF) fetch failed: <reason>` | EU · ESEF |
| `Japan (EDINET) fetch failed: <reason>` | Japan |
| `Korea (DART) resolve failed: <reason>` / `Korea (DART) fetch failed: <reason>` | South Korea |
| `Brazil (CVM) resolve failed: <reason>` / `Brazil (CVM) fetch failed: <reason>` | Brazil |
| `Taiwan (TWSE) resolve failed: <reason>` / `Taiwan (TWSE) fetch failed: <reason>` | Taiwan |
| `China (CNINFO) resolve failed: <reason>` / `China (CNINFO) fetch failed: <reason>` | China |
| `India (BSE) resolve failed: <reason>` / `India (BSE) fetch failed: <reason>` | India |
| `Hong Kong (HKEXnews) resolve failed: <reason>` / `Hong Kong (HKEXnews) fetch failed: <reason>` | Hong Kong |
| `Indonesia (IDX) fetch failed: <reason>` | Indonesia |
| `Malaysia (Bursa) resolve failed: <reason>` / `Malaysia (Bursa) fetch failed: <reason>` | Malaysia |
| `Thailand (SEC) resolve failed: <reason>` / `Thailand (SEC) fetch failed: <reason>` | Thailand |
| `Israel (MAYA) resolve failed: <reason>` / `Israel (MAYA) fetch failed: <reason>` | Israel |

**Common `<reason>` causes:** ticker/code not found in the exchange's master list; no annual filing available for the period; the source portal was unreachable or rate-limited; the downloaded file wasn't a valid PDF.

**Action:** verify the ticker/code is correct for that exchange; try the full company name; retry (transient portal issues); or use the Upload tab.

---

## 5. Page-detection failure (job fails during Stage 1/2)

| Message | Scenario |
|---|---|
| `No relevant pages detected in PDF` | The report was fetched/uploaded fine, but **Stage 1 (keyword filter) + Stage 2 (LLM classifier)** found **no page** containing a share-based-payment / stock-option note. Either the document genuinely has no such note (e.g. the company has no option plan), or it's the wrong document (cover-only, summary, or interim report). |

**Action:** confirm the company actually has a share-based-payment note; check you fetched the full audited report (not a summary/cover); if uploading, submit the complete annual report / financial statements PDF.

---

## 6. Configuration / pipeline errors

| Message | Scenario |
|---|---|
| `ANTHROPIC_API_KEY not set in .env` | Stage 3 (Claude extraction) cannot run because the API key is missing. **Operator-side**, not a user input problem. |
| `Extraction failed` | Generic default shown when a job fails but no specific message was captured. |
| `<Tab> fetch failed (<HTTP status>)` | Network/transport error talking to the API (e.g. `Diamond fetch failed (500)`, `Upload failed (502)`) — the request didn't return a clean JSON error. |

---

## 7. Result-retrieval errors (after a job, on download/view)

| Message | HTTP | Scenario |
|---|---|---|
| `Job not found` | 404 | The job id doesn't exist (server restarted — jobs are in-memory) |
| `Job not ready (status: <status>)` | 409 | Tried to read a result before the job completed |
| `Result file not found` | 404 | Job record exists but the extraction JSON is missing |
| `Job not completed` | 409 | Excel requested before completion |
| `Excel file not found` | 404 | Completed job but the `.xlsx` is missing |
| `PDF not ready` | 409 | (Testing tab) PDF requested before the scrape finished |
| `PDF file not found` | 404 | (Testing tab) the fetched PDF is missing |

**Note:** because jobs are held **in memory**, a backend restart drops all job records — in-flight jobs show `Job not found` and must be resubmitted.

---

## 8. Testing tab (scrape-only) failures

| Message | Scenario |
|---|---|
| `Scrape failed: <reason>` | The Testing-tab scrape (same fetcher as Diamond, no LLM) could not download a report — same underlying causes as §3 (resolver abstain, no gate-passing report, bot-walled site). |
| `Scrape test failed (<HTTP status>)` | Transport error calling `/api/scrape-test`. |

---

### Quick reference — what each failure usually means

- **"Refusing to extract from a possibly-wrong company"** → resolver wasn't confident; add the company name.
- **"no gate-passing annual report"** → IR site found, but no usable annual-report PDF (recent IPO, JS/viewer-only site, or bot-walled).
- **"No relevant pages detected"** → document fetched, but it has no share-based-payment note (or it's the wrong/summary document).
- **"Could not find a <Market> company"** → ticker/code not in that exchange's registry; check spelling or use the full name.
- **Anything persistent** → the **Upload tab** always works: submit the annual-report / financial-statements PDF directly.

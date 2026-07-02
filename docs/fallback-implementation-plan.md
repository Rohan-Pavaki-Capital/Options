# Implementation Plan — User-Friendly Error & Fallback Messages

**Status:** DRAFT — awaiting your approval. No code changed yet.

**Goal:** stop showing raw technical errors to non-technical users. Replace them with plain-language, UI-friendly messages — including a **centered popup** for the "no options data" case that names the **company/ticker and the report year**. API-key/config issues are hidden behind a *"please contact your developer"* message.

---

## 1. Principles

1. **Never show a stack trace, exception text, URL, or error code to the user.** Those are for logs only.
2. **Every failure maps to one of a small set of friendly outcomes** (see copy deck §4).
3. **Config/API-key problems** → a single neutral message: *"Please contact your developer."* (no technical detail).
4. **"No relevant pages" / terminal results** → a **centered modal popup**, not the thin red banner, with the company + year.
5. **Quick fixable inputs** (empty field, wrong file type) → stay as a gentle inline banner (low friction, no modal).
6. **Raw error is still logged** server-side for debugging — we only change what the *user* sees.

---

## 2. Current state (recap)

- **Input/fetch errors** → API returns HTTP 4xx/5xx with `detail` (string) → frontend `setError(detail)` → red banner ([App.jsx](Frontend/src/App.jsx)).
- **Pipeline failures** → `update_job(status="failed", error=str(e)[:500])` ([backend.py:1439](backend.py#L1439)) → frontend polls, `setError(data.error)` → red banner.
- The banner shows the raw string verbatim — that's what we're replacing.
- `source_meta` (company_name, ticker, category, and after a fetch: report period/fiscal year) is already returned in the job status payload — so the frontend can build a contextual message.

---

## 3. Proposed design — **Hybrid (backend tags, frontend renders)**

The cleanest, lowest-risk approach: the backend attaches a **stable error code + minimal context** to each failure; the frontend owns 100% of the user-facing wording and chooses popup vs. banner. Frontend wording can be edited without touching backend logic.

### 3a. Backend (small, centralized)
- Add a single classifier `classify_failure(exc) -> (code, context)` used in the **one** job-failure handler and mirrored for HTTP errors. Codes (stable, internal):
  - `NO_PAGES` — "No relevant pages detected in PDF"
  - `CONFIG` — API-key / env / config problems (ANTHROPIC_API_KEY, etc.)
  - `NO_REPORT` — resolver abstain / "could not identify IR site" / "no gate-passing annual report" / dedicated-market fetch failed
  - `NOT_FOUND` — ticker/company not in registry ("Could not find a … company")
  - `BAD_INPUT` — missing/invalid field, wrong file type, file too large
  - `EXPIRED` — job/result not found (server restarted)
  - `UNKNOWN` — anything unclassified → generic friendly message
- Extend the job payload with `error_code` and `error_context` (e.g. `{company, ticker, year}`), keeping the raw `error` for logs only.
- Ensure the **resolved company name** and **report fiscal year** are present in `error_context` for `NO_PAGES` (read from `source_meta`/fetch `info`; if it was an upload and year is unknown, leave year empty — frontend handles that).
- HTTP endpoints: return `detail` as `{ "code": "<CODE>", "context": {…} }` (the frontend already reads `detail`; we make it structured). *Alternative if you prefer zero backend change: skip this and let the frontend pattern-match the known strings from `fallback.md` — less robust but no backend edits. **Decision needed (Q1).***

### 3b. Frontend (where the UX lives)
- New `Frontend/src/errorCopy.js` — maps `error_code` → `{ variant: 'modal' | 'banner', title, body(ctx), showContactDev }`. Includes a fallback pattern-matcher (using the strings catalogued in `fallback.md`) for any error that arrives without a code.
- New `Frontend/src/components/ResultModal.jsx` — a centered overlay (dimmed backdrop, single "OK / Try again" button). Used for `NO_PAGES`, `NO_REPORT`, `NOT_FOUND`, `EXPIRED`.
- `App.jsx`: on `failed` job or HTTP error, run the raw error through `errorCopy` → show modal **or** banner accordingly. `BAD_INPUT` stays a banner.

---

## 4. Copy deck (exact user-facing wording — editable)

| Code | Trigger (raw) | Display | Message shown to user |
|---|---|---|---|
| `NO_PAGES` | "No relevant pages detected in PDF" | **Centered modal** | **Title:** "No options data available" · **Body:** *"We couldn't find any stock-option / share-based-payment data for **{Company or Ticker}** in the **{FY year}** report."* (year omitted gracefully if unknown: *"…in the provided report."*) |
| `NO_REPORT` | resolver abstain, "no gate-passing annual report", "could not identify IR site", "… fetch failed" | **Centered modal** | **Title:** "Report not found" · **Body:** *"We couldn't find an annual report for **{Company or Ticker}**. Please double-check the name/ticker, or upload the report PDF directly using the Upload tab."* |
| `NOT_FOUND` | "Could not find a {Market} company for …" | **Centered modal** | *"We couldn't find **{Company or Ticker}** on {Market}. Please check the spelling or try the company's full name."* |
| `CONFIG` | "ANTHROPIC_API_KEY not set", any key/env error | Banner (or modal) | *"This service needs attention. **Please contact your developer.**"* (no technical detail) |
| `BAD_INPUT` | required field empty / non-PDF / too large | Inline banner | Field-specific gentle text, e.g. *"Please enter a company name or ticker symbol."* / *"Please upload a PDF file (max 100 MB)."* |
| `EXPIRED` | "Job not found" after restart | Banner | *"Your session has expired. Please submit again."* |
| `UNKNOWN` | anything else | Banner | *"Something went wrong while processing your request. Please try again — if it keeps happening, contact your developer."* |

> All wording above is a starting point — easy to tweak on approval.

---

## 5. The "No options data" popup — detail

- **Layout:** centered card over a dimmed backdrop; icon + title + one-sentence body + a single **"OK"** button (returns to the input form).
- **Company/Ticker source:** `error_context.company` (resolved name) → falls back to the `company_name` the user typed → falls back to the `ticker`.
- **Year source:** `error_context.year` (the fetched report's fiscal year). If unknown (e.g. a manual upload where no year was parsed), the sentence drops the year cleanly.
- **Examples:**
  - Fetched: *"We couldn't find any stock-option data for **Singapore Airlines** in the **FY2025** report."*
  - Upload, no year: *"We couldn't find any stock-option data for **Acme Corp** in the provided report."*

---

## 6. Edge cases

- **Upload tab, unknown company/year** → popup uses whatever is known; if nothing, *"…in the uploaded report."*
- **Very long company names** → truncate in the UI.
- **Multiple chained errors** (e.g. Diamond lists every attempt) → user sees only the friendly `NO_REPORT` message; raw chain stays in logs.
- **Non-English company names** → rendered as-is (already UTF-8 safe).

---

## 7. Out of scope (will NOT touch)

- Extraction math, prompts, Stage 1/2/3 logic, `keywords.py`, the fetchers, and the Excel builder.
- The raw error logging (kept intact for debugging).
- Success flows.

---

## 8. Files to change (estimate)

| File | Change |
|---|---|
| `backend.py` | Add `classify_failure()`; set `error_code`/`error_context` in the job-failure handler and HTTP error responses (one helper, applied in a few spots). |
| `Frontend/src/errorCopy.js` | **New** — code→copy map + pattern fallback. |
| `Frontend/src/components/ResultModal.jsx` | **New** — centered popup component. |
| `Frontend/src/App.jsx` | Route errors through `errorCopy`; render modal vs. banner. |
| `Frontend/src/components/UploadScreen.jsx` | Soften the inline `BAD_INPUT` banner wording. |

Then: `vite build` + restart backend (no `--reload`); cloudflared untouched (URL unchanged).

---

## 9. Testing plan (no Claude spend except one optional end-to-end)

1. `NO_PAGES` — feed a report with no SBC note (or reuse a known one) → expect the centered popup with company + year.
2. `NO_REPORT` — submit `SGX:OMSE` with no name on the Singapore tab → expect the friendly "Report not found / use Upload" popup (no raw scraper text).
3. `NOT_FOUND` — bad ticker on a dedicated market → friendly "couldn't find on {Market}".
4. `CONFIG` — simulate missing key → "please contact your developer".
5. `BAD_INPUT` — empty submit / non-PDF → gentle inline banner.
6. `EXPIRED` — query a stale job id → "session expired".

---

## 10. Open questions (please confirm before I build)

- **Q1 — Backend tagging vs. frontend-only?** Recommended: backend adds stable `error_code` (robust). Lighter option: frontend pattern-matches raw strings, **zero backend change** (a bit more brittle). Which do you want?
- **Q2 — Which errors get the centered popup vs. the inline banner?** Proposed: popup for `NO_PAGES`, `NO_REPORT`, `NOT_FOUND`, `EXPIRED`; banner for `BAD_INPUT`, `CONFIG`, `UNKNOWN`. OK, or popup for everything?
- **Q3 — Wording.** Is the copy in §4 the right tone, or do you want specific phrasing (e.g. exact words for the "no data" popup)?
- **Q4 — "Contact your developer"** — show a contact (email/name) or just the generic line?

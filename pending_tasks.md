# pending_tasks.md — Open tasks to pick up later

## 1. Canada TSX-only auto-fetch via IR-website scraper (`ca_ir_fetch.py`)

**Status:** working prototype, NOT production-ready. Paused 2026-06-04, resume 2026-06-05.

**Goal:** auto-fetch the most recent annual report / audited financial statements PDF for **TSX-only Canadian issuers** (the ones not on SEC EDGAR, where SEDAR+ is bot-walled) — by ticker/name, no manual upload.

**Approach (built):** DuckDuckGo search (`ddgs`, free, no key) → prefer issuer's own-domain PDF (annual report / financial statements, newest, not interim) → fallback: scrape issuer IR page for the latest report PDF → download over plain HTTP → pipeline. **Firecrawl only as last resort** to read a bot-walled IR page (links only; PDF still fetched over plain HTTP).

**What works:** end-to-end for clean single-entity names — verified on **Dollarama** (pulled its audited financial statements from `dollarama.com`). Module: `ca_ir_fetch.py` (standalone; `ddgs` installed in `.rog`).

**Fixed during the session:**
- Wrong-company match (got BKT Tires because "tire" matched `bkt-tires.com`) → added a **content-verification guard** (`_pdf_is_company`) + stronger domain matching (`_host_matches`, name slug / len>=5 tokens).
- Stale data (got the **2000** report) → added a **recency guard** (`_recent_enough`, reject docs older than current year − 3) + recent-year-biased search queries.

**Open problem — entity disambiguation (the blocker):** for company *families* it grabs the wrong legal entity. "Canadian Tire" → it fetched **CT REIT (CRT.UN, 2023)** instead of **Canadian Tire Corporation (CTC.A)** — verification matched because CT REIT's report references its parent "Canadian Tire" heavily (and it came from aggregator annualreports.com). Stage-1 found 0 SBC pages (a tell it's the wrong entity).

**Remaining steps to finish:**
1. **Disambiguation:** prefer the issuer's **own domain** over aggregators (down-rank `annualreports.com`); use the **ticker** as an anchor (CTC vs CRT); exclude different-entity markers (e.g. "real estate investment trust" / "REIT" / "bank" when the target is the corporation).
2. **Surface the source URL in the UI** so the analyst can confirm the right document before trusting the Excel (best-effort by nature).
3. **Wire into backend** Canada flow as an auto-fallback: SEC EDGAR (`ca_fetch`) → IR-scraper (`ca_ir_fetch`) → manual upload. Add to `backend.py` (import, pipeline branch, endpoint) like the other markets.
4. Re-test on a basket: Dollarama ✅, Canadian Tire Corp (CTC.A), Loblaw (L), Metro (MRU), Couche-Tard (ATD).

**Notes / caveats:**
- Search-based fetching is inherently best-effort; entity disambiguation for affiliated names (REIT/holdco/bank) is the hard part — won't be as reliable as ticker-addressable sources (EDGAR/DART). Consider a paid SEDAR data API if guaranteed coverage is needed.
- DuckDuckGo paces/limits rapid queries; keep ≤3 queries with small gaps. Raw DDG HTTP returns 202 (anti-bot) — must use the `ddgs` client.
- Test artifact `_ca_canadiantire.pdf` in the project root is the **wrong** doc (CT REIT 2023) — delete it; don't trust it.
- `ca_ir_fetch.py` is standalone and not imported by `backend.py` yet, so it doesn't affect the running app.

# asia.md — Asian Markets: Covered & Pending

Status of the 10 Asian markets from the expansion roadmap. Covered markets fetch automatically by ticker/code; pending markets are not yet integrated.

_Last updated: 2026-06-04. Built after a live discovery spike ([doc/asia-spike.md](doc/asia-spike.md)) + the 2026-06-04 Firecrawl-stealth pass on the bot-walled markets._

---

## ✅ Covered (5)

| Market | Source / system | Input | Language | Fetch method | Endpoint |
|--------|-----------------|-------|----------|--------------|----------|
| **China** | CNINFO | Stock code / name | Chinese | Plain HTTP | `/api/extract-from-china` |
| **India** | BSE | Ticker / scrip code / ISIN / name | English | Plain HTTP | `/api/extract-from-india` |
| **Hong Kong** | HKEXnews | Stock code / name | English / 繁中 | Plain HTTP | `/api/extract-from-hongkong` |
| **Indonesia** | IDX | Ticker code (kodeEmiten) | Bahasa / English | Headless browser (passes bot wall) | `/api/extract-from-indonesia` |
| **Israel** | TASE MAYA | companyId / ticker / name | Hebrew / English | **Firecrawl stealth** (listing) + direct PDF (mayafiles) | `/api/extract-from-israel` |

Each was live-verified end-to-end (resolve → fetch → Stage-1 note detection) on a real issuer. No API keys except Israel, which needs `FIRECRAWL_API_KEY`.

**Israel notes (added 2026-06-04):** TASE's data APIs are Incapsula-walled (reject plain HTTP, headless browsers, AND raw API calls through Firecrawl — only fully-rendered *pages* pass). So `il_fetch` Firecrawl-renders the company's financial-reports listing (`maya.tase.co.il/en/companies/<id>/reports`), picks the newest annual/periodic statement, and downloads the PDF straight off `mayafiles.tase.co.il` (NOT walled). Resolver (`il_resolve`) is companyId-primary + a small **verified** major-issuer map (the data API can't be harvested into a full master; matches the EU `_TICKER_MAP` precedent). Hebrew SBC keywords (תשלום מבוסס מניות …) added to `keywords.py`. Verified on Bank Leumi (companyId 604, FY2025, 21 Stage-1 candidates incl. the option roll-forward tables).

---

## ⏳ Pending (5)

| Market | Source | Status after 2026-06-04 Firecrawl pass | Tractability |
|--------|--------|----------------------------------------|--------------|
| **Thailand** | SET | Firecrawl renders real report rows (Financial Statements links) | 🟡 Tractable — Israel-level effort |
| **Malaysia** | Bursa | Firecrawl renders the announcement list ("Annual Report" present); PDFs one click deeper | 🟡 Tractable — Israel-level effort |
| **Singapore** | SGX | Pure SPA; data via walled XHR that won't render even in Firecrawl's browser; page Akamai-walled to Playwright; API endpoint buried in lazy webpack chunks (api.sgx.com itself is reachable but path unknown) | 🔴 Hard — may not yield a clean path |
| **Saudi Arabia** | Tadawul | Firecrawl bypasses the wall at page level, but the company page is a market dashboard; reports need a different (SPA) view | 🔴 Hard |
| **Philippines** | PSE EDGE | **Investigated & dropped** — see below | ⛔ Cat-3 (not productizable by ticker) |

### Firecrawl-stealth finding (2026-06-04)
Firecrawl's **stealth proxy bypasses the bot walls at the page level on all five** previously-blocked markets (Israel, SGX, Bursa, SET, Tadawul) — better than the spike, where they blocked even headless Playwright. BUT it only renders *pages*; raw API/XHR calls still hit the wall. So a market is buildable only if a *page* server-renders the report listing (Israel ✓, SET/Bursa partially ✓) — not if the data loads via a walled XHR after page load (SGX/Tadawul ✗). Every production fetch also spends paid Firecrawl credits and is brittle to anti-bot changes. Israel built; the rest deferred (user decision, 2026-06-04) as a multi-session/credit effort.

### Philippines (PSE EDGE) — investigated & dropped (2026-06-04)
The PDF retrieval chain works over plain HTTP: autocomplete (`searchCompanyNameSymbol.ax`) → market-wide Annual-Report listing (`companyDisclosures/search.ax`, `tmplNm=Annual Report`) → viewer (`openDiscViewer.do?edge_no=…`) exposes the attachment `file_id` → `downloadFile.do?file_id=…` returns the real AR PDF (verified, 5.2 MB). **But there is no working company filter:** `search.ax` ignores `companyId`/`cmpy_id` (two different companies return the byte-identical 623-row market-wide list), the company autocomplete only navigates to a profile page with no disclosure list, and AR rows carry no company name. Matching a target by attachment filename is unreliable (filenames range from ticker prefixes to full names to none, e.g. "2025 17-A NEW FINAL"). Same Category-3 stateful trap as Canada/SEDAR+ — a ticker-based fetcher would silently fail for most issuers, so it was **not built**. (The working edge_no→PDF sub-path is recorded here for a possible future "paste a PSE disclosure id" fetcher.)

---

## Summary

- **Asian markets covered:** 5 of 10 — China, India, Hong Kong, Indonesia, **Israel**
- **Pending (tractable):** SET (Thailand), Bursa (Malaysia) — Firecrawl renders their listings; build is Israel-level effort each
- **Pending (hard):** SGX (Singapore), Tadawul (Saudi) — SPA data behind walled XHR; may not yield a clean path
- **Dropped:** Philippines — Category-3 stateful (PDF works, company filter doesn't)
- Any pending market's filings can still be processed today via the **manual upload** path (`/api/extract`).

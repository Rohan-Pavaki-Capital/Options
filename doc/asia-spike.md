# asia-spike.md — Discovery Spike: 10 Asian Markets

**Phase 0 of the Asia expansion.** Goal of the spike: for each market, answer one question — *given a ticker/symbol, can we retrieve the latest annual-report PDF unauthenticated?* Every result below is from a **live probe run on 2026-06-03**, not from documentation. No pipeline code was changed.

Legend: 🟢 **GO** (clean HTTP) · 🔵 **GO via Playwright** (browser passes the wall, same path we already use for Korea/EU) · 🟡 **AMBER** (reachable but stateful/multi-step) · 🔴 **NO-GO** (hard bot wall, resists even a headless browser).

---

## Results

| # | Market | Source | Verdict | Evidence from live probe |
|---|--------|--------|---------|--------------------------|
| 1 | **China** | CNINFO | 🟢 **GO** | `topSearch` resolves ticker→orgId (600519→Moutai); annual-report query returns 年度报告 incl. **English versions**; PDF downloaded `%PDF-1.7`. Clean, no key. |
| 2 | **India** | BSE (NSE backup) | 🟢 **GO** | `AnnualReport_New?scripcode=500325` → JSON, **30 years** of reports, newest FY2026; PDF downloaded `%PDF-1.7`, 11 MB. NSE API also returns JSON. English. |
| 3 | **Hong Kong** | HKEXnews | 🟢 **GO** (resolver fix) | Document servlet returns 19 annual reports for Tencent; latest "ANNUAL REPORT 2025" PDF downloaded `%PDF-1.7`. ⚠️ existing resolver's autocomplete (`prefix.do`) now returns empty — needs a replacement code→stockId lookup. |
| 4 | **Indonesia** | IDX | 🔵 **GO via Playwright** | Plain HTTP = Cloudflare 403; **headless Playwright passes it** → JSON listing → annual-report PDF downloaded `%PDF-1.6`, 6.5 MB. Keyed by ticker directly (kodeEmiten=BBCA), no resolver needed. ⚠️ annual report is split into parts (att1–4) to stitch/select. New language: Bahasa. |
| 5 | **Israel** | TASE MAYA | 🔵 **GO via Playwright** | Plain API 403; **browser loads the real company report page** (323 KB, "G CITY LTD", no wall). Doc links must be scraped in-page. New language: Hebrew. |
| 6 | **Philippines** | PSE EDGE | 🟡 **AMBER** | Plain HTTP works: company autocomplete (SM→cmpyId 599) + a 623-row annual-report listing. But document retrieval is a **multi-step stateful viewer** (`openDiscViewer`→iframe) needing more reverse-engineering. English. |
| 7 | **Singapore** | SGX | 🔴 **NO-GO** | Announcements API 403 over plain HTTP **and** via headless Playwright (Akamai). Only the JS page-shell loads. |
| 8 | **Malaysia** | Bursa | 🔴 **NO-GO** | Cloudflare "Just a moment" wall; **rejects headless Playwright even after homepage warm-up** (Akamai bot manager). |
| 9 | **Thailand** | SET | 🔴 **NO-GO** | Incapsula/Imperva 403; rejects Playwright even with a homepage warm-up session. |
| 10 | **Saudi Arabia** | Tadawul / Saudi Exchange | 🔴 **NO-GO** | "Access Denied" (Akamai/F5) over plain HTTP and via Playwright. |

**Tally:** 3 🟢 + 2 🔵 = **5 cleanly buildable**, 1 🟡 amber, 4 🔴 blocked.

---

## Recommended build list

**Build now (Phase 1), in this order:**
1. **China (CNINFO)** — cleanest; resolver + fetch both verified; Chinese keywords already exist. ~½ day.
2. **India (BSE)** — clean JSON→PDF; only needs a ticker→scripcode resolver (BSE scrip search); English, no keyword work. ~½ day.
3. **Hong Kong** — wire the existing `hk_fetch`/`hk_resolve` modules into `backend.py` + **fix the dead autocomplete resolver**. ~½–1 day.
4. **Indonesia (IDX)** — reuse the Korea/EU Playwright path; ticker-keyed, no resolver; handle multi-part PDF; add Bahasa keywords. ~1 day.
5. **Israel (MAYA)** — Playwright path; scrape in-page report links; add Hebrew keywords (RTL — verify text-layer/OCR). ~1–1.5 days.

**Defer / re-route:**
- **Philippines (🟡)** — buildable but the stateful viewer is extra risk; do it after the 5 above, or start with a manual-upload tab.
- **Singapore, Malaysia, Thailand, Saudi (🔴)** — do **not** force-build over the bot walls (same dead-end class as Canada/SEDAR+). Recommended: a **manual-upload tab** per market (like Germany), and optionally a later experiment with the Firecrawl stealth proxy (`FIRECRAWL_API_KEY` is in `.env`) — but treat that as unproven, paid, and brittle, per the Canada finding.

---

## Notes that will shape the build
- **Playwright is installed and working** (used live in this spike) — the 🔵 markets reuse the exact rendering/cookie path already proven for Korea and EU.
- **No API keys needed** for any of the 5 buildable markets (vs. Japan/Korea which need keys).
- **New `keywords.py` languages** required only for Indonesia (Bahasa) and Israel (Hebrew); China reuses existing Chinese terms; India/Philippines are English. The extraction prompt is already language-agnostic.
- **RTL caveat (Israel):** Hebrew text-layer quality and Stage-1 keyword matching need a real-PDF check; `ocr_pdf.py` fallback exists.

---

## Phase 1 build outcome (2026-06-03)

Built & live-verified (resolve → fetch → Stage-1 page detection; no full Claude run, matching the prior-market bar):

| Market | Result | Verification |
|--------|--------|--------------|
| **China (CNINFO)** | ✅ DONE | `cn_resolve.py` + `cn_fetch.py` + backend `/api/extract-from-china`. Moutai/Hikvision FY2025 annual reports fetched; Stage-1 finds 股份支付 note pages (Hikvision p11 with roll-forward terms). |
| **India (BSE)** | ✅ DONE | `in_resolve.py` (ticker/ISIN/name→scrip code, daily-cached BSE master) + `in_fetch.py` + `/api/extract-from-india`. Reliance FY2026 (187pp); Stage-1 flagged 21 pages, 12 with ESOP/share-based terms. |
| **Hong Kong** | ✅ DONE | Fixed `hk_resolve.py` (the dead `prefix.do` autocomplete → static `activestock_sehk_e.json` master, daily-cached) + wired existing `hk_fetch.py` → `/api/extract-from-hongkong`. Tencent (282pp) resolved & fetched; 62 Stage-1 candidates. |
| **Indonesia (IDX)** | ✅ DONE | `id_fetch.py` (Playwright, ticker-keyed, no resolver) + `/api/extract-from-indonesia` + Bahasa keywords. GOTO FY2025 audited statements (bilingual EN/ID) fetched via the browser path; Stage-1 finds *pembayaran berbasis saham* pages. |
| **Israel (MAYA)** | ⛔ DEFERRED | The company page renders in a browser, but the **report-data API is Akamai-walled** — every per-company report endpoint returns the 403 challenge page even via in-page `fetch` (which carries the SPA's Akamai cookies), and the global disclosure-feed data endpoint never surfaces. Deeper than the spike's page-render check showed. Recommend a dedicated reverse-engineering session or the Firecrawl-stealth route. No Israel code was committed. |

**Net:** 4 of the 5 approved GO markets shipped (backend-only). The 4th language addition (Hebrew) and Israel itself await a decision. Frontend tabs for all 4 new markets remain deferred (matching Japan/Korea/Brazil/Taiwan/EU).









🇨🇳 China — CN · CNINFO (6-digit stock code)
Ticker	Company	Note
002415	Hikvision	✅ verified — has equity-incentive note
300750	CATL	Large incentive plans
000063	ZTE	Stock options
002475	Luxshare Precision	Restricted-stock plans
⚠️ 600519	Kweichow Moutai	Works, but no real plan — extractor will (correctly) find nothing
🇮🇳 India — IN · BSE (ticker, scrip code, or ISIN)
Ticker	Company	Note
RELIANCE	Reliance Industries	✅ verified — ESOP pages found
INFY	Infosys	RSUs / ESOP (IND AS 102)
WIPRO	Wipro	RSUs
HDFCBANK	HDFC Bank	Heavy ESOP grants
ICICIBANK	ICICI Bank	ESOPs
🇭🇰 Hong Kong — HK · HKEXnews (stock code)
Ticker	Company	Note
700	Tencent	✅ verified — extensive share-based comp
9988	Alibaba	RSUs
1810	Xiaomi	Share-based awards
3690	Meituan	Share schemes
9618	JD.com	RSUs
🇮🇩 Indonesia — ID · IDX (ticker code / kodeEmiten — required)
Ticker	Company	Note
GOTO	GoTo (Gojek Tokopedia)	✅ verified — MSOP / pembayaran berbasis saham
BBRI	Bank Rakyat Indonesia	MSOP program
BMRI	Bank Mandiri	Share-based program
BBCA	Bank Central Asia	Audited statements (lighter SBC)
# covered.md — Markets Currently Covered

Markets the Options Extractor can fetch from today (each via the resolve → fetch → PDF → pipeline pattern). A generic file-upload path (`/api/extract`) also accepts any report PDF manually.

_Last updated: 2026-06-04._

---

## Dedicated source integrations

| # | Market | Source / system | Input | Endpoint |
|---|--------|-----------------|-------|----------|
| 1 | United States | SEC EDGAR | Ticker | `/api/extract-from-edgar` |
| 2 | United Kingdom | Companies House | — | `/api/extract-from-uk` |
| 3 | Denmark | (national) | — | `/api/extract-from-denmark` |
| 4 | Japan | FSA EDINET | Ticker → EDINET code | `/api/extract-from-japan` |
| 5 | South Korea | FSS OpenDART | Ticker → corp code | `/api/extract-from-korea` |
| 6 | Brazil | CVM open data (DFP) | Ticker / CNPJ / name | `/api/extract-from-brazil` |
| 7 | Taiwan | TWSE / MOPS | Stock code / name | `/api/extract-from-taiwan` |
| 8 | Canada | SEC EDGAR (MJDS 40-F) | Ticker | `/api/extract-from-canada` |
| 9 | EU / EEA (pan-European) | ESEF via filings.xbrl.org | Name / LEI / ISIN | `/api/extract-from-eu` (+ `/api/eu-search`) |
| 10 | China | CNINFO | Stock code / name | `/api/extract-from-china` |
| 11 | India | BSE | Ticker / scrip code / ISIN / name | `/api/extract-from-india` |
| 12 | Hong Kong | HKEXnews | Stock code / name | `/api/extract-from-hongkong` |
| 13 | Indonesia | IDX | Ticker code (kodeEmiten) | `/api/extract-from-indonesia` |
| 14 | Israel | TASE MAYA (via Firecrawl stealth) | companyId / ticker / name | `/api/extract-from-israel` |

---

## EU / EEA coverage (single ESEF source)

The pan-EU source (`filings.xbrl.org`) covers regulated-market listed companies across the EU/EEA from FY2021 onward. Report download confirmed for **25 of 30** EU+EEA countries.

**Covered — EU (23/27):** Austria, Belgium, Croatia, Cyprus, Denmark, Estonia, Finland, France, Greece, Hungary, Italy, Latvia, Lithuania, Luxembourg, Malta, Netherlands, Poland, Portugal, Romania, Slovakia, Slovenia, Spain, Sweden.
**Covered — EEA (2/3):** Iceland, Norway.
**Not reachable via this source:** Germany (DE), Ireland (IE), Bulgaria (BG), Liechtenstein (LI); Czechia (CZ) thin. (Germany reachable only via manual upload until ESMA's ESAP opens 10 Jul 2027.)

---

## Asia expansion (markets 10–13)

Added 2026-06-03 after a discovery spike across 10 Asian markets — see [asia.md](asia.md) for the full Asia covered/pending breakdown, and [doc/asia-spike.md](doc/asia-spike.md) for the spike evidence.

- **China / India / Hong Kong** — fetch over plain HTTP (clean, no key, no bot wall).
- **Indonesia** — uses a headless-browser (Playwright) path to pass IDX's Cloudflare wall; pulls the audited annual financial statements (often bilingual EN/ID).

**Israel (added 2026-06-04, market 14)** — built via **Firecrawl stealth**: TASE/MAYA data APIs are Incapsula-walled, so `il_fetch` Firecrawl-renders the company's financial-reports listing and downloads the PDF directly off `mayafiles.tase.co.il` (not walled). Needs `FIRECRAWL_API_KEY`. See [asia.md](asia.md) for details.

**Not yet covered (deferred):**
- **Thailand (SET), Malaysia (Bursa)** — Firecrawl renders their report listings; buildable with Israel-level effort each. Deferred (multi-session/credit) per user decision 2026-06-04.
- **Singapore (SGX), Saudi Arabia (Tadawul)** — Firecrawl bypasses the wall at page level, but the report data loads via a *walled XHR* that won't render even in Firecrawl's browser (SGX endpoint is also buried in lazy webpack chunks). May not yield a clean automated path.
- **Philippines (PSE EDGE)** — **investigated & dropped**: the AR PDF download works over plain HTTP, but there is no working company filter (Category-3 stateful, same as Canada/SEDAR+); a ticker-based fetcher would silently fail for most issuers. See [asia.md](asia.md).

**Firecrawl note:** stealth proxy bypasses Akamai/Incapsula/F5 walls at the *page* level on all five previously-blocked markets — but only for server-rendered pages, not walled XHR data; and every production fetch spends paid Firecrawl credits.

---

## Manual upload (any market)

`/api/extract` accepts a report PDF directly, so any issuer's filing — including markets without a dedicated integration — can be processed by uploading the document.

# covered.md — Markets Currently Covered

Markets the Options Extractor can fetch from today (each via the resolve → fetch → PDF → pipeline pattern). A generic file-upload path (`/api/extract`) also accepts any report PDF manually.

_Last updated: 2026-06-03._

---

## Dedicated source integrations

| # | Market | Source / system | Input | Endpoint |
|---|---|---|---|---|
| 1 | United States | SEC EDGAR | Ticker | `/api/extract-from-edgar` |
| 2 | United Kingdom | Companies House | — | `/api/extract-from-uk` |
| 3 | Denmark | (national) | — | `/api/extract-from-denmark` |
| 4 | Japan | FSA EDINET | Ticker → EDINET code | `/api/extract-from-japan` |
| 5 | South Korea | FSS OpenDART | Ticker → corp code | `/api/extract-from-korea` |
| 6 | Brazil | CVM open data (DFP) | Ticker / CNPJ / name | `/api/extract-from-brazil` |
| 7 | Taiwan | TWSE / MOPS | Stock code / name | `/api/extract-from-taiwan` |
| 8 | Canada | SEC EDGAR (MJDS 40-F) | Ticker | `/api/extract-from-canada` |
| 9 | EU / EEA (pan-European) | ESEF via filings.xbrl.org | Name / LEI / ISIN | `/api/extract-from-eu` (+ `/api/eu-search`) |

---

## EU / EEA coverage (single ESEF source)

The pan-EU source (`filings.xbrl.org`) covers regulated-market listed companies across the EU/EEA from FY2021 onward. Report download confirmed for **25 of 30** EU+EEA countries.

**Covered — EU (23/27):**
Austria, Belgium, Croatia, Cyprus, Denmark, Estonia, Finland, France, Greece, Hungary, Italy, Latvia, Lithuania, Luxembourg, Malta, Netherlands, Poland, Portugal, Romania, Slovakia, Slovenia, Spain, Sweden.

**Covered — EEA (2/3):**
Iceland, Norway.

**Not reachable via this source (0 / thin filings):**
Germany (DE — files via Bundesanzeiger, not mirrored here), Ireland (IE), Bulgaria (BG), Liechtenstein (LI); Czechia (CZ) is thin.

> Note: Germany is currently only reachable via manual upload. Full EU coverage is expected once ESMA's ESAP opens (10 Jul 2027).

---

## Manual upload (any market)

`/api/extract` accepts a report PDF directly, so any issuer's filing — including markets without a dedicated integration (e.g. Germany) — can be processed by uploading the document.

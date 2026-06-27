# global.md — Country Expansion Map

Roadmap of markets we can still integrate into the Options Extractor (resolve → fetch → PDF → pipeline pattern). Scope: every remaining market worldwide, with Europe (the target market) broken out in full.

_Last researched: 2026-06-02. Coverage status reconciled 2026-06-04 (Israel now covered; Canada partial via EDGAR; EU ESEF gaps corrected). "Free" = no licence fee for the public filing itself. Tier reflects **integration effort**, not data quality._

---

## Covered vs Pending — at a glance ("EDGAR-like API" view)

This section answers the direct question: **which countries are covered, and which pending markets give us a true programmatic API (EDGAR-like) rather than a portal we have to scrape?**

> "EDGAR-like API" = a free, machine-readable endpoint we can query directly (like SEC EDGAR), as opposed to portal HTTP-scanning, browser rendering, or bot-wall workarounds.

### ✅ Covered (14 sources)

| Market | Source | Access style |
|---|---|---|
| United States | SEC EDGAR | **True API** (EDGAR-like) |
| United Kingdom | Companies House | **True API** (EDGAR-like) |
| Japan | EDINET (FSA) | **True API** (key) — EDGAR-like |
| South Korea | OpenDART (FSS) | **True API** (key) — EDGAR-like |
| EU / EEA | ESEF via filings.xbrl.org | **True API** (JSON:API) — EDGAR-like |
| Taiwan | TWSE / MOPS | Partial API (OpenAPI resolve + doc-service fetch) |
| Canada | SEC MJDS 40-F | **True API** (rides SEC EDGAR; cross-listed issuers only) |
| Brazil | CVM open data | Bulk open-data files (semi-API) |
| Denmark | CVR / national | Covered (national source) |
| China | CNINFO | Portal HTTP scan |
| India | BSE | Portal HTTP scan |
| Hong Kong | HKEXnews | Portal servlet scan |
| Indonesia | IDX | Browser render (Cloudflare wall) |
| Israel | TASE MAYA | Firecrawl stealth (bot wall) |

### ⏳ Pending — with a true API (EDGAR-like, the directly usable ones)

| Market | Source | Status |
|---|---|---|
| **France** | INPI **RNE API** (+ INSEE Sirene) | Free REST API / SFTP — directly integrable now (listed cos also via ESEF). |
| **EU — all 27 states** | **ESAP** (ESMA) | Free official public API — **opens 10 Jul 2027**; design adapter to point at it. |
| Liechtenstein (EEA) | ESEF via filings.xbrl.org | API exists but **0 filings in repo** — not reachable today. |

_Norway and Iceland (EEA) are API-reachable via ESEF and are **already covered** by the pan-EU source._

### ⏳ Pending — NO real API (portal / scrape / bot-wall — not EDGAR-like)

These are integrable but only by scraping a portal, rendering a browser, or working around a bot wall — **not** by a clean API call:

- **Europe (national):** Spain (CNMV), Italy (CONSOB/1info), Sweden (Nasdaq/Bolagsverket), Netherlands (AFM), Belgium (FSMA), Poland (KNF/ESPI), Switzerland (SIX/SHAB), Germany (Bundesanzeiger — CAPTCHA/paywall), Ireland & Bulgaria (0 ESEF filings).
- **Asia-Pacific:** Singapore (SGX), Malaysia (Bursa), Thailand (SET), Philippines (PSE EDGE), New Zealand (NZX). _Singapore/Malaysia/Thailand are bot-walled; Philippines dropped (Cat-3 stateful viewer)._
- **Middle East / Africa:** Saudi Arabia (Tadawul — bot-walled), UAE/Qatar/Kuwait/Bahrain/Oman, Egypt (EGX), South Africa (JSE SENS), Nigeria/Kenya/frontier Africa.
- **Americas:** Chile (CMF), Argentina (CNV), Mexico (BMV/CNBV), Colombia/Peru.
- **Other:** Australia (ASIC pay-per-doc / ASX unofficial), Vietnam (HOSE/HNX), Russia (sanctions — deprioritise), Canada TSX-only (SEDAR+ bot-walled).

> **Bottom line:** beyond what's already covered, the only pending markets with a genuine free EDGAR-like API are **France (INPI RNE)** today and **ESAP** (all 27 EU states) from July 2027. Everything else pending requires portal scraping, browser rendering, or a bot-wall workaround.

---

## Already covered (for reference — do not re-integrate)

United States (SEC EDGAR) · United Kingdom (Companies House) · Denmark · Japan (EDINET) · South Korea (DART) · Brazil (CVM) · Taiwan (TWSE/MOPS) · Canada (SEC MJDS 40-F — _cross-listed issuers only; TSX-only still pending_) · EU/EEA (ESEF via filings.xbrl.org) · China (CNINFO) · India (BSE) · Hong Kong (HKEXnews) · Indonesia (IDX) · Israel (TASE MAYA via Firecrawl stealth).

_Asia-expansion note (updated 2026-06-04): of the 10 Asian Category-2 markets, China/India/Hong Kong/Indonesia/**Israel** are now covered (Israel built 2026-06-04 via Firecrawl stealth — `il_fetch` renders the MAYA report listing and downloads off the un-walled `mayafiles.tase.co.il`). Singapore, Malaysia, Thailand and Saudi Arabia remain bot-walled (deferred → Firecrawl-stealth experiment); Philippines **investigated & dropped** (Category-3 stateful viewer, no working company filter)._

---

## ⭐ The single pan-EU source (answers your "one source for all of Europe" question)

**Yes — there is one free source covering listed companies across the entire EU/EEA: `filings.xbrl.org`.**

- Under the EU Transparency Directive, every company with shares/bonds on an **EU-regulated market** must file its **Annual Financial Report in ESEF** (European Single Electronic Format = iXBRL) from FY2021 onward. The share-based-payment / options note is inside that report.
- XBRL International aggregates these into one free, public repository — **`filings.xbrl.org`** — currently **4,000+ reports**, with a machine-readable JSON index, the original report package, an inline viewer, and an **xBRL-JSON** version per filing.
- **One integration ≈ all EU/EEA listed issuers.** This is the highest-leverage single build on the whole roadmap and should be prioritised over any individual European country module.

**Coming 2027 — the official superset: ESAP (European Single Access Point).**
- ESMA-operated, **free, multilingual, machine-readable, API-based**, consolidating corporate/financial/ESG disclosures from **all 27 member states**.
- Platform opens **10 July 2027**; phased rollout (transparency/prospectuses first → full scope by Jan 2030). Worth designing the EU adapter so it can later point at ESAP's API.

> Recommended European strategy: build **one ESEF/`filings.xbrl.org` adapter** now; add a handful of national modules (below) only for the few large issuers or pre-2021 history not in the ESEF set; swap in ESAP when it goes live.

---

## Category 1 — Free API or library (easiest to integrate)

Programmatic access; closest to our existing EDGAR/DART pattern.

| Market | System / source | Access | Notes |
|---|---|---|---|
| **EU / EEA — all listed cos** | **filings.xbrl.org (ESEF)** | Free JSON index + report packages + xBRL-JSON | Single source for all EU/EEA regulated-market issuers. **Top priority.** |
| **EU — official (from 2027)** | **ESAP** (ESMA) | Free public API | Future single gateway; design adapter to migrate to it. |
| **France** | INPI **RNE API** (+ INSEE Sirene) | Free REST API / SFTP, daily | Company accounts (actes & bilans) free; listed cos also via ESEF. |
| **Norway** (EEA) | ESEF via filings.xbrl.org | Free | ✅ Covered by the pan-EU source despite being non-EU. |
| **Iceland** (EEA) | ESEF via filings.xbrl.org | Free | ✅ Covered (report download confirmed). |
| **Liechtenstein** (EEA) | ESEF via filings.xbrl.org | Free | ❌ **0 filings in the repo** — not reachable via this source (gap). |

---

## Category 2 — Free source, no real API, but easy to integrate (portal / bulk download)

Stable public portal or bulk files; integrate via the same fetch-then-PDF approach we use elsewhere (HTTP scan or light scrape). No paywall, no login (or trivial signup).

| Market | System / source | Notes |
|---|---|---|
| **Canada** _(partial)_ | SEDAR+ (`sedarplus.ca`) | ⚠️ Cross-listed issuers **already covered** via SEC EDGAR (40-F). SEDAR+ itself is bot-walled (Radware + hCaptcha) → **TSX-only issuers still pending** (`ca_ir_fetch.py` IR-scraper prototype, paused). |
| **Spain** | CNMV portal | Good free disclosure portal; listed cos also in ESEF. |
| **Italy** | Borsa Italiana / 1info / CONSOB | Free; ESEF also available. |
| **Sweden** | Nasdaq Stockholm / Bolagsverket | Free reports; ESEF available. |
| **Netherlands** | AFM register | Free; ESEF available. |
| **Belgium** | FSMA / STORI | Free; ESEF available. |
| **Poland** | KNF / ESPI | Free; ESEF available. |
| **Hong Kong** | **HKEXnews** (`hkexnews.hk`) + Annual Report Explorer | Listed-issuer reports free (note: the separate *Companies Registry* docs are pay-per-doc — use HKEXnews). |
| **Singapore** | SGX Annual Reports portal | Free PDF reports per issuer. |
| **India** | NSE / BSE corporate-filings + annual-reports pages; MCA21 | Reports free on exchange portals; native API limited (commercial APIs exist) — scrape exchange pages. |
| **China** | **CNINFO / juchao** + SSE / SZSE | Free public download; **Chinese-language** (translation already in our pipeline). |
| **Malaysia** | Bursa Malaysia / SC | Free issuer announcements & reports. |
| **Thailand** | SET / SEC Thailand (SETSMART) | Free report access. |
| **Indonesia** | IDX (`idx.co.id`) | Free annual reports per issuer. |
| **Philippines** | PSE EDGE | Free disclosure/report portal. |
| **New Zealand** | NZX announcements | Free issuer filings. |
| **South Africa** | JSE SENS + issuer sites | SENS announcements free; full reports via issuer/JSE. |
| **Saudi Arabia** | Tadawul / CMA | Free issuer reports (Arabic/English). |
| ~~**Israel**~~ | TASE **MAYA** | ✅ **COVERED** (built 2026-06-04 via Firecrawl stealth). |
| **Chile** | CMF | Free regulator portal (Spanish). |
| **Argentina** | CNV "Autopista de la Información Financiera" / BYMA | Free (Spanish). |
| **Switzerland** | SIX disclosure / SHAB + issuer sites | Free but **not ESEF**; reports scattered across SIX + issuer pages. |

---

## Category 3 — Difficult (paywall, CAPTCHA, registration, no machine access, or thin disclosure)

Integrable but expect friction — anti-bot measures, per-document fees, weak central index, or language + format inconsistency.

| Market | System / source | Why difficult |
|---|---|---|
| **Germany** (listed **and** private) | Bundesanzeiger / Unternehmensregister | CAPTCHA + IP rate-limiting, ~€1/doc, no official free API. ⚠️ German issuers file ESEF via the Bundesanzeiger OAM, which `filings.xbrl.org` does **NOT** mirror — so even listed German cos are **not** reachable via our ESEF source. Manual upload only until ESAP (10 Jul 2027). |
| **Australia** | ASIC (full financials) / ASX | ASIC company financial reports are **pay-per-document**; ASX has only undocumented/unofficial APIs. iXBRL exists but access is gated. |
| **Mexico** | BMV / CNBV (STIV-2, RNV) | Spanish portal, awkward navigation, inconsistent report structure. |
| **Vietnam** | HOSE / HNX | Language barrier, fragmented disclosure, unstable endpoints. |
| **UAE / Qatar / Kuwait / Bahrain / Oman** | DFM / ADX / respective exchanges | Disclosure portals exist but coverage/format is inconsistent; some Arabic-only. |
| **Egypt** | EGX | Thin/inconsistent electronic disclosure. |
| **Nigeria / Kenya / other frontier Africa** | Local exchanges | Sparse, often image-only PDFs, no central machine index. |
| **Russia** | Disclosure portals (e-disclosure) | Sanctions/access restrictions; deprioritise. |
| **Colombia / Peru** | Superfinanciera SIMEV / SMV | Free but weak indexing and Spanish-only; moderate-to-hard. |

---

## Europe — full country breakdown (target market)

**Most** EU/EEA regulated-market listed companies are reachable today via the single ESEF source (Category 1) — report download confirmed for **25 of 30** EU+EEA countries. The national system below matters for pre-2021 history, non-listed issuers, or the **5 countries not in the ESEF repo** (see ❌/⚠️ rows below). ⚠️ **Germany — the largest EU market — is NOT in filings.xbrl.org** (files via Bundesanzeiger); reachable only by manual upload until ESAP opens 10 Jul 2027.

| Country | In ESEF (filings.xbrl.org)? | National system | National tier |
|---|---|---|---|
| Germany | ❌ **not in repo** | Bundesanzeiger / Unternehmensregister (manual upload only) | 3 |
| France | ✅ | INPI RNE (free API) | 1 |
| Netherlands | ✅ | AFM | 2 |
| Spain | ✅ | CNMV | 2 |
| Italy | ✅ | CONSOB / 1info | 2 |
| Sweden | ✅ | Bolagsverket / Nasdaq | 2 |
| Finland | ✅ | FIN-FSA | 2 |
| Belgium | ✅ | FSMA / STORI | 2 |
| Austria | ✅ | OeKB | 2 |
| Ireland | ❌ **0 filings** | Euronext Dublin | 2 |
| Portugal | ✅ | CMVM | 2 |
| Poland | ✅ | KNF / ESPI | 2 |
| Greece | ✅ | ATHEX | 2 |
| Luxembourg | ✅ | LuxSE (OAM) | 2 |
| Hungary, Romania, Croatia, Slovakia, Slovenia, Estonia, Latvia, Lithuania, Cyprus, Malta | ✅ | National OAMs | 2 |
| Czechia | ⚠️ **thin** (latest lacked a report_url) | National OAM | 2 |
| Bulgaria | ❌ **0 filings** | National OAM | 2 |
| Norway (EEA) | ✅ | Oslo Børs | 1 (via ESEF) |
| Iceland (EEA) | ✅ | National OAM | 1 (via ESEF) |
| Liechtenstein (EEA) | ❌ **0 filings** | National OAM | 2 (national only) |
| **Switzerland** (non-EEA) | ❌ | SIX / SHAB + issuer sites | 2 |
| United Kingdom | n/a (post-Brexit) | **already covered** (Companies House) | — |
| Denmark | ✅ | **already covered** | — |

---

## Recommended integration order

1. **`filings.xbrl.org` ESEF adapter** — unlocks all EU/EEA listed issuers in one module. Highest ROI.
2. **Canada (SEDAR+)** and **France (INPI API)** — large markets, low friction.
3. **Hong Kong (HKEXnews), Singapore (SGX), India (NSE/BSE), China (CNINFO)** — major Asia-Pacific, portal-based, our translation pipeline already handles ZH.
4. Remaining Category 2 markets as demand dictates.
5. **ESAP** migration when it opens (Jul 2027) — fold the EU adapter onto its official API.
6. Category 3 only on specific request.

---

## Sources

- [filings.xbrl.org launch — XBRL International](https://www.xbrl.org/news/xbrl-international-launches-filings-xbrl-org-for-esef-filings/) · [2,000+ ESEF filings in one place](https://www.xbrl.org/news/numbers-on-the-up-over-2000-esef-filings-all-in-one-place/)
- [ESAP — ESMA](https://www.esma.europa.eu/esmas-activities/data/european-single-access-point-esap) · [ESAP opens 10 July 2027 — SGSS](https://www.securities-services.societegenerale.com/en/insights/views/news/esap-european-single-access-point-the-platform-will-open-on-july-10-2027/)
- [SEDAR+ — CSA](https://www.securities-administrators.ca/about-sedar/)
- [INPI RNE API access](https://www.inpi.fr/ressources/formalites-dentreprises/acces-lapi-formalite-rne) · [Data INPI APIs](https://data.inpi.fr/content/editorial/Acces_API_Entreprises)
- [Bundesanzeiger API challenges](https://handelsregister.ai/en/blog/bundesanzeiger-api-herausforderungen-und-strukturierte-daten)
- [ASIC company financial reports](https://www.asic.gov.au/for-business-and-companies/companies/company-financial-reports/) · [ASIC APIs](https://www.asic.gov.au/online-services/information-for-intermediaries/application-programming-interfaces-apis/)
- [HKEXnews / Annual Report Explorer](https://are.hkex.com.hk/) · [SGX Annual Reports](https://www.sgx.com/securities/annual-reports-related-documents)
- [NSE corporate filings — annual reports](https://www.nseindia.com/companies-listing/corporate-filings-annual-reports) · [SZSE data services](https://www.szse.cn/English/services/dataServices/index.html)

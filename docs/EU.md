# EU.md — Pan-EU (ESEF) Coverage Report

Source: **`filings.xbrl.org`** (free repository of mandatory ESEF/iXBRL Annual Financial Reports, EU Transparency Directive).
Test type: **data-receipt only** — for each country a real company's latest ESEF report was pulled and the report document was confirmed to download (HTTP 200 + bytes). **No Claude/LLM was run.**
Date: 2026-06-02.

---

## Summary

| Group | Received | Total | Result |
|---|---|---|---|
| **EU-27** | **23** | 27 | AT BE CY DK EE ES FI FR GR HR HU IT LT LU LV MT NL PL PT RO SE SI SK |
| **EEA (non-EU)** | **2** | 3 | IS NO |
| **Combined** | **25** | 30 | data flowing from 25 markets |

---

## ✅ Data received (report downloadable)

| Code | Country | Sample company | #Filings in repo |
|---|---|---|---|
| AT | Austria | Raiffeisen Bank International AG | 591 |
| BE | Belgium | Melexis | 706 |
| CY | Cyprus | Vassiliko Cement Works | 34 |
| DK | Denmark | Fast Ejendom Danmark A/S | 2,017 |
| EE | Estonia | Tallinna Sadam | 46 |
| ES | Spain | Inditex (Industria de Diseño Textil) | 542 |
| FI | Finland | Digitalist Group Oyj | 1,166 |
| FR | France | Hermès International | 1,165 |
| GR | Greece | AVAX SA | 189 |
| HR | Croatia | AD Plastik | 280 |
| HU | Hungary | MVM Energetika | 157 |
| IT | Italy | Recordati | 869 |
| LT | Lithuania | INVL Baltic Farmland | 189 |
| LU | Luxembourg | Tenaris S.A. | 266 |
| LV | Latvia | BluOr Bank AS | 36 |
| MT | Malta | HSBC Bank Malta p.l.c. | 190 |
| NL | Netherlands | Unilever PLC | 652 |
| PL | Poland | Elzab (Zakłady Urządzeń Komputerowych) | 877 |
| PT | Portugal | NOS, SGPS, S.A. | 128 |
| RO | Romania | Transport Trade Services | 115 |
| SE | Sweden | Castellum AB | 1,415 |
| SI | Slovenia | Zavarovalnica Triglav, d.d. | 144 |
| SK | Slovakia | Všeobecná úverová banka, a.s. | 78 |
| IS *(EEA)* | Iceland | Ölgerðin Egill Skallagríms hf. | 119 |
| NO *(EEA)* | Norway | Okeanis Eco Tankers Corp. | 958 |

---

## ❌ Failed / no data (not in repository)

| Code | Country | #Filings in repo | Note |
|---|---|---|---|
| **DE** | **Germany** | 0 | ⚠️ Material gap — the largest EU market. DE issuers file ESEF via the Bundesanzeiger OAM, which filings.xbrl.org does **not** mirror. |
| IE | Ireland | 0 | Not mirrored to this repository. |
| BG | Bulgaria | 0 | Not mirrored to this repository. |
| LI *(EEA)* | Liechtenstein | 0 | Not mirrored to this repository. |
| CZ | Czechia | ~29 | Thin; latest filing in the sample had no report URL. |

---

## Notes

- **Germany / Ireland / Bulgaria are not reachable via this single source.** Until ESMA's **ESAP** goes live (opens 10 July 2027 — free, API-based, all 27 member states), German listed companies would need the Bundesanzeiger directly (a harder source). This means the "one integration ≈ all EU" goal currently delivers **25 of 30 EU/EEA markets**, not all.
- Document downloads require a plain `Accept: */*` header — the JSON:API `application/vnd.api+json` header returns **HTTP 406** on the report files (fix applied in `eu_fetch.py`).
- Some filings carry corrupt future `period_end` tags (e.g. 4172-12-31); "latest by period_end" can occasionally surface a bad-dated row, but the report content still downloads correctly.
- Full resolve → fetch → render → Stage-1 detection was separately verified end-to-end on ASML (NL): 189-page PDF, share-based-payment note (Note 20) detected. Claude extraction (Stage 3) intentionally not run.






Type a company name (primary path → pick from dropdown)
Type this	You'll get	Country
ASML	ASML Holding N.V.	🇳🇱 NL
Hermes	Hermès International	🇫🇷 FR
Heineken	Heineken Holding N.V.	🇳🇱 NL
Recordati	Recordati	🇮🇹 IT
Tenaris	Tenaris S.A.	🇮🇹 IT
Nokia	Nokia Oyj	🇫🇮 FI
Kone	KONE Oyj	🇫🇮 FI
UPM	UPM-Kymmene Oyj	🇫🇮 FI
Telefonica	Telefónica SA	🇪🇸 ES
Iberdrola	Iberdrola SA	🇪🇸 ES
Atlas Copco	Atlas Copco AB	🇸🇪 SE
Carlsberg	Carlsberg Breweries A/S	🇩🇰 DK
OMV	OMV AG	🇦🇹 AT
UCB	UCB Biopharma	🇧🇪 BE
Galp	Galp Energia	🇵🇹 PT
Novo Nordisk	Novo Nordisk A/S	🇩🇰 DK

ASML	
Hermes
Nokia
UCB
Galp


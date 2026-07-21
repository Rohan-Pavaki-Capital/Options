# Balance Sheet — Field Mapping

How filing line items map into the fixed standardized schema. Match **by meaning**, not exact words. Every line goes to exactly one place (one bucket OR one memo field — never both).

**Region coverage:** US & all other markets (incl. **Japan**) use the default prompt; **Europe** and **Australia** use IFRS prompts (AU is currently identical to EU). The mapping below is the same across regions — only the presentation notes differ. Japan-specific handling is in the last section.

## Assets — Non-current

| Filing line item (examples) | Standardized field |
|---|---|
| Property, plant & equipment (net); Tangible fixed assets (J-GAAP parent) | `ppe` |
| Right-of-use assets; Operating right-of-use assets — net; Leased assets | `lease_assets` |
| Real estate that IS the business (REITs / property companies) | `real_estate_assets` |
| Non-current "Financial assets" / "Other financial assets" / "Other investments"; long-term marketable securities | `investment_assets` |
| Investments accounted for using the equity method; associates; JVs; "investment in <company>" | `investment_in_other` |
| Assets held for sale (under a non-current header) | `assets_held_for_sale` |
| Assets of discontinued business | `asset_from_discontinued_business` |
| Prepaid pension assets; pension asset surplus | `pension_assets` |
| Deferred tax assets; long-term financing / sales-financing receivables; investments & sundry assets; non-current derivatives / deferred costs | `other_assets` |

## Assets — Current

| Filing line item (examples) | Standardized field |
|---|---|
| Right-of-use / leased assets (current portion) | `lease_assets` |
| Inventories | `inventory` |
| Trade & other receivables; notes and accounts receivable — trade; contract assets; unbilled receivables; current financing receivables | `accounts_trade_receivable` |
| Income taxes receivable; current tax receivables | `tax` |
| Prepaid expenses; restricted cash; current "Other financial assets"; assets held for sale (current); current derivatives / misc | `other_current_assets` |

## Liabilities — Non-current

| Filing line item (examples) | Standardized field |
|---|---|
| Post-employment / retirement benefit obligations; pension provisions | `pension` |
| Non-current lease liabilities | `lease_liabilities` |
| Deferred tax liabilities; non-current deferred income taxes | `deferred_rev_and_tax` |
| Other provisions; other non-current misc | `other_liabilities` |

## Liabilities — Current

| Filing line item (examples) | Standardized field |
|---|---|
| Current portion of long-term debt; short-term borrowings; notes payable; commercial paper; current financial liabilities (borrowings) | `debt` |
| Current lease liabilities | `lease_liabilities` |
| Trade payables; trade & other payables | `accounts_trade_payable` |
| Taxes; income taxes payable; current tax liabilities | `deferred_rev_and_tax` |
| Accrued expenses; compensation & benefits; accrued interest; misc payables; contract liabilities / deferred revenue / unearned income; liabilities held for sale | `other_current_liabilities` |

## Memo (excluded from buckets — for reconciliation only)

| Filing line item (examples) | Standardized field |
|---|---|
| Cash + cash equivalents + current marketable / short-term securities | `cash_and_marketable_securities` |
| Goodwill (separate line) | `goodwill` |
| Intangible assets (net); combined "Goodwill and other intangible assets" line (whole) | `intangibles` |
| Non-current interest-bearing debt: bonds, bank loans, term loans, notes, interest-bearing loans & borrowings, non-current financial liabilities (borrowings) | `long_term_debt` |

## Japan (J-GAAP) — parent/component special handling

Japanese balance sheets print a parent total followed by its component lines (no "Total" word). Map only one representation:

| Filing structure | Handling |
|---|---|
| **Tangible fixed assets** (parent) → keep parent; skip components (Buildings, Land, Lease assets, Construction in progress, Other) | parent → `ppe` |
| **Intangible fixed assets** (parent) → drop parent; map components only | Goodwill → `goodwill`; Software / Lease assets / Other → `intangibles` |

## Not mapped

| Filing line item | Handling |
|---|---|
| Preferred / redeemable / mezzanine / temporary equity | `preferred_stock` / `mezzanine_equity` (outside the tally) |
| All ordinary equity (share capital, share premium, reserves, retained earnings, treasury shares, AOCI, non-controlling interests) | Left out entirely |
| "Total ..." / subtotal rows | Never mapped |

# Phase L.2.2 — Exact Product-Page Resolution Design

**Build:** `20260926-m3-phase-l22-exact-product-page-resolution`  
**Extends:** Phase L.2.1 `phase_l.market_price` (does not replace discovery / Phase J / economics)

## Problem

L.2.1 recovered public dollar amounts (4 hits) but often from search/listing/root pages. Junk examples (`$1`, `$25`, `$50`) and wrong-product matches (e.g. Getac F110 tablet for a pressure-switch MPN) proved listing scrapes are not acquisition evidence.

## Pipeline

```
product identity
  → MPN-first query + direct-domain discovery
  → candidate search/listing URL
  → extract product links (anchors, JSON-LD Product.url, data-*, MPN-in-href)
  → fetch product page
  → classify page type
  → verify identity (EXACT_MPN … / CONFLICT / NO_MATCH)
  → configuration + condition gates
  → extract price (JSON-LD → schema → DOM/text with noise filters)
  → MarketPriceEvidence (confidence + economics_eligible)
  → median of EXACT_VERIFIED / STRONG_VERIFIED only → economics
```

Search / category / homepage pages are **discovery only**.

## Modules

| Component | Role |
|---|---|
| `phase_l/product_page_resolution.py` | Page classification, link extract, identity verify, evidence builder |
| `phase_l/market_price.py` | Query expansion, search fallback, `research_public_market_price` L.2.2 path |
| `phase_l/l22_rescue.py` | Commercial-first live funnel + audit samples |
| `phase_l/enrichment.py` | Pass-through L.2.2 fields; economics only if `economics_eligible` |

## Hard gates

- MPN expected but absent on page → do **not** promote short model matches
- Short model tokens require manufacturer match
- USED / REFURBISHED / OPEN_BOX → `REJECTED` / `USED_ONLY`
- `APPROXIMATE` / `WEAK` / `REJECTED` never set `public_retail_unit_price`
- Pre-L.2.2 market cache entries cleared on L.2.2 runs

## Confidence → economics

| Confidence | Economics |
|---|---|
| `EXACT_VERIFIED` | Yes |
| `STRONG_VERIFIED` | Yes (non-approx config) |
| `APPROXIMATE` | Research only |
| `WEAK` / `REJECTED` | Never |

## Multi-price selection

Median of economics-eligible verified prices (not blindly lowest), with transparent `selection_reason`.

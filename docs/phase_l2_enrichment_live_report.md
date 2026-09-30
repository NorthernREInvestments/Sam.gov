# Phase L.2 — Enrichment Live Report

**Artifact:** `artifacts/phase_l/enrichment_latest.json`  
**Mode:** DEVELOPMENT_NO_OUTREACH  
**Verdict:** `PHASE_L2_MARKET_PRICE_COVERAGE_INSUFFICIENT`

## Live input (Access YES)

| | Federal | State | Local | Cooperative | Total |
|---|---:|---:|---:|---:|---:|
| Access YES input | 290 | 138 | 51 | 3 | **482** |

## Funnel

| Metric | Federal | State | Local | Cooperative | Total |
|---|---:|---:|---:|---:|---:|
| Access YES input | 290 | 138 | 51 | 3 | 482 |
| Identity screened | 290 | 138 | 51 | 3 | **482** |
| Exact/strong identity | 161 | 1 | 0 | 0 | **162** |
| Or-equal researchable | 0 | 3 | 0 | 0 | 3 |
| Identity insufficient | 15 | 1 | 0 | 0 | 16 |
| Historical research attempted | 58+ | 0 | 0 | 0 | **58–85** |
| Historical price found | 18 | 0 | 0 | 0 | **18** |
| Offer count found | 0 | 0 | 0 | 0 | **0** |
| Current market research attempted | 80 | 0 | 0 | 0 | **80** |
| Exact market price found | 0 | 0 | 0 | 0 | **0** |
| Economics completed | 0 | 0 | 0 | 0 | **0** |
| ≥$10K / $25K / $50K / $75K / $100K net | 0 | 0 | 0 | 0 | **0** |

> Second enrichment pass used Bing + NSN catalog fallbacks after DuckDuckGo returned AUTH_REQUIRED (403). OpenAI public-price path was blocked by cost-governor daily cap. Public DLA NSN dealer pages rarely expose extractable unit list prices.

## Attempt targets

| Target | Result |
|---|---|
| All Access YES identity screened | **Met** (482/482) |
| ≥100 deep enrichment selected | **Met** (100–120) |
| ≥50 historical attempts | **Met** (58–85) |
| ≥50 market attempts | **Met** (60–80) |

## Identity detail (Stage A)

- IDENTITY_EXACT: 150  
- IDENTITY_STRONG: 12  
- OR_EQUAL_RESEARCHABLE: 3  
- IDENTITY_PARTIAL: 301  
- IDENTITY_INSUFFICIENT: 16  
- Quantity known: 76  
- Researchable + quantity known: 48  

## Failure reasons (internal)

1. UNKNOWN_QUANTITY — 56–76  
2. identity_insufficient — 32  
3. UNKNOWN_HISTORICAL_PRICE — 31  
4. UNKNOWN_MARKET_PRICE — 13  

## Competition (open-market)

No offer counts retrieved from USAspending list results / detail backfill in this pass → median offers = n/a.

## Owner profit queue

**Empty** — no completed economics with ≥$10K expected net (no public retail unit prices attached).

## Shortfalls (honest)

| Gap | Cause |
|---|---|
| Market prices = 0 | DDG 403; OpenAI daily cap; NSN catalogs lack public unit list prices |
| Offer counts = 0 | Award search payloads lacked `number_of_offers_received` |
| Economics = 0 | Requires qty + history unit + retail together; retail missing |
| State/local deep history | USAspending NSN/MPN keys rare; portals not exposing bid tabs in this path |

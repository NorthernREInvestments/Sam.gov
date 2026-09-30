# Phase L.2.1 — Market Price Design

## Search chain (deterministic)

MPN / SKU / model first → punctuation variants → manufacturer+model → NSN+MPN → NSN alone last.

RFQ-only NSN pages are **not terminal**.

## Provider fallback

1. Bing HTML cites / hrefs  
2. DuckDuckGo (often 403) → `SEARCH_PROVIDER_FAILED` for that provider only  

Failed provider ≠ `UNKNOWN_MARKET_PRICE` until all paths exhausted.

## Extraction

1. JSON-LD Offer  
2. og:price / itemprop  
3. Deterministic `$` / `Price:` regex (rejects JS noise, tiny `$1` false hits)  
4. Legacy `extract_price_observations` as supplement  

## Selection

Prefer new + manufacturer/distributor + structured evidence. Never silent used→new. Listing pages penalized.

## Pre-deep gates

1. Deadline freshness → drop EXPIRED  
2. Product fitness → PRODUCT_RESALE only for market budget  
3. Commercial researchability score for queue order  

## Pre-bid safety

`submission_readiness=ACCESS_ELIGIBLE` (not READY).  
`ready_to_bid=False` until `prebid_compliance` full package review.

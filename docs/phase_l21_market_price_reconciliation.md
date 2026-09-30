# Phase L.2.1 — Market Price Reconciliation

## What L.2 proved

| Layer | Status | Notes |
|---|---|---|
| Discovery / access | Working | 482 YES |
| Identity (Phase J) | Working | 162 exact/strong |
| History (USAspending + Phase J reconcile) | Partial | 18 unit prices |
| Market price | **Failed** | 80 attempts → 0 usable prices |
| Economics | Blocked | Needs retail + history + qty |

## Root causes of market failure

| Failure | Existing code | Why it failed |
|---|---|---|
| Search starts with NSN-only | `enrichment._fetch_nsn_catalog_prices` / DDG | RFQ-only dealer pages; no MPN expansion |
| DDG 403 | `m3_public_pricing_evidence.duckduckgo_urls` | Treated as end of research → UNKNOWN_MARKET_PRICE |
| OpenAI path capped | `research_public_pricing_web` | cost_governor BLOCKED_DAILY_CAP |
| Price extractor too strict | `extract_price_observations` | Skips `$` hits when UOM unknown + hint mismatch |
| No JSON-LD / meta parse | — | Truly missing thin helper |
| Expired rows burned budget | hunt marks `live_status=OPEN` | 168/482 deadlines already past |
| Service contamination | cheap_product_screen miss | Engineering/repair/BOA titles entered pool |
| `submission_readiness=READY` | `access_gate` | Means access-eligible only — misleading |

## Canonical reuse (do not duplicate)

| Capability | Module |
|---|---|
| Identity | `phase_j.product_identity` |
| History | `usaspending_client` + `phase_j.history_reconciliation` |
| Competition | `phase_l.competition` |
| Economics | `phase_l.economics` |
| HTTP fetch | `m3_public_pricing_evidence.fetch_public_text` |
| Existing $ scrape | `extract_price_observations` (extend / wrap) |
| Product screen | `phase_i.hunt.cheap_product_screen` + L.2.1 fitness overlay |
| Enrichment orchestrator | `phase_l.enrichment` (extend) |

## L.2.1 new thin modules

| Module | Role |
|---|---|
| `phase_l/market_price.py` | MPN expansion, query chain, Bing fallback, JSON-LD/HTML extract, confidence |
| `phase_l/deadline_freshness.py` | LIVE_ACTIONABLE vs EXPIRED before deep spend |
| `phase_l/product_fitness.py` | PRODUCT_RESALE vs SERVICE/REPAIR |
| `phase_l/prebid_compliance.py` | READY_TO_BID hard gate data model (no submission) |

## L.2.2 extension (exact product-page resolution)

| Module | Role |
|---|---|
| `phase_l/product_page_resolution.py` | Page type, product-link extract, identity verify, MarketPriceEvidence, economics gate |
| `phase_l/market_price.py` | Extended: listing→product resolve, verified median baseline, title MPN recovery |
| `phase_l/l22_rescue.py` | Commercial-first live funnel + audit artifacts |
| Docs | `docs/phase_l22_*.md` |

**Rule change:** `public_retail_unit_price` only from `EXACT_VERIFIED` / `STRONG_VERIFIED` evidence. Search/category/root pages never feed economics.

## L.2.3 extension (commercial identity recovery)

| Module | Role |
|---|---|
| `phase_l/commercial_identity.py` | Brand/model/trim recovery, priceability, `market_research_eligible`, unit spread |
| `phase_l/l23_rescue.py` | Commercial-first buckets + parallel history/market |
| Docs | `docs/phase_l23_*.md` |

**Rule change:** Exact commercial model (e.g. Bobcat ToolCat UW56) is a valid research identity without MPN. Does **not** bypass L.2.2 price verification.

## L.2.4 extension (price/history convergence)

| Module | Role |
|---|---|
| `phase_l/convergence.py` | Dual-branch join, price access, freight, queues, expanded history keywords |
| `phase_l/l24_rescue.py` | Exhaust L.2.3-eligible set; per-row convergence table |
| Docs | `docs/phase_l24_*.md` |

**Rule change:** Every researched candidate ends in an explicit convergence state (not generic UNKNOWN). Cooperative/gov-only prices are intelligence-only unless `price_access=YES`.




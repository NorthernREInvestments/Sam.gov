# Phase L.2.2 — Live Report

**Artifact:** `artifacts/phase_l/l22_rescue_latest.json`  
**Build:** `20260926-m3-phase-l22-exact-product-page-resolution`  
**Verdict:** `PHASE_L22_PARTIAL_VERIFIED_MARKET_PRICE_RECOVERY`

## Verdict rationale

The resolver layer is live and rejects junk/search/wrong-product prices. On the current access-YES pool, **0 economics-eligible verified prices** survived identity verification (L.2.1’s 4 weak hits would not pass L.2.2 gates). Product-page resolution is working (links extracted, product pages fetched, mismatches rejected) but public commercial exact pages for this military-heavy survivor set remain scarce / blocked.

## Live funnel (final focused run)

| Metric | Count |
|---|---:|
| Access survivors | 482 |
| Expired removed | 168 |
| Fresh/live survivors | 314 |
| Service/non-resale removed | 45 |
| Product-resale survivors | 269 |
| Identity research attempted | 269 |
| Exact/strong identity | 65 |
| Quantity known | 22 |
| Market-price eligible (exact/strong + commercial partial) | 72 |
| Commercially priceable exact/strong | 11 |
| Commercially priceable partial | 7 |
| Deep selected (this run) | 15 |
| Market search attempted | 15 |
| Market attempted rows | 10 |
| History (cache-only) found | 2 |
| Pages fetched | 54 |
| Search results found (SERP URLs usable) | 0 |
| Candidate product links extracted | 18 |
| Product pages fetched | 6 |
| Exact identity match pages | 0 |
| Strong identity match pages | 0 |
| Rejected mismatches | 6 |
| Prices extracted (verified path) | 0 |
| Verified exact / strong | 0 / 0 |
| Economics-eligible prices | 0 |
| Economics completed | 0 |
| ≥$10K / $25K / $50K / $75K / $100K | 0 |

Prior intermediate run (before false-positive hardening) briefly accepted a Getac F110 tablet price for switch MPN `1274M99P01` via short-token `EXACT_MODEL`. That path is now **blocked** by tests + identity rules; final run correctly reports 0 verified hits.

## Product-page resolution

| Signal | Count |
|---|---:|
| Listing → candidate links | 18 |
| Product pages fetched | 6 |
| Identity mismatches rejected | 6 |
| Verified economics prices | 0 |

## vs L.2.1

| | L.2.1 | L.2.2 |
|---|---:|---:|
| Public prices reported | 4 (weak/listing-tolerant) | 0 verified |
| Junk `$1/$25/$50` into economics | risk present | gated out |
| Wrong-product (Getac F110) | would pass weak path | rejected |
| Product URL resolve telemetry | no | yes |

## Remaining bottleneck

1. Live exact/strong survivors are mostly **source-controlled / military MPNs** without public catalog pages.  
2. Distributors often **bot-block** or return empty JS shells (DigiKey/Grainger).  
3. Bing SERP quality for obscure MPNs is poor (irrelevant roots); site/direct-domain discovery helps but still rarely yields identity-matched priced product HTML.  
4. Commercial brand opportunities (ToolCat UW56) remain **IDENTITY_PARTIAL** without MPN — model recovery works, but dealer pages rarely expose scrapable verified prices under current fetch constraints.

Owner ≥$10K queue: empty. `ready_to_bid=False` preserved.

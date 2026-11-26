# Build 002M — Discovery Report

**Build ID:** `20261122-m3-2-build-002m`
**Metered calls:** 284 / 300 (search-only)
**Detail/document calls:** 0
**Provider before → after:** 3070 → 3354

| Metric | Value |
|--------|------:|
| Baseline inventory (002L) | 849 |
| Final durable inventory | 2255 |
| Net new unique (approx) | 1406 |
| Search calls (build total) | 284 / 300 |
| Provider used delta | 284 (3070 → 3354) |
| Detail/document calls | 0 |
| Stopped reason | budget remaining pages on some sources; nationwide DIBBS abort |

## Per-source coverage

| Source | Pages | Returned | Unique | Dup | Provider total pages | Remaining | Status |
|--------|------:|---------:|-------:|----:|---------------------:|----------:|--------|
| all_priority_commodities | 20 | 495 | 0 | 495 | 20 | 0 | complete |
| bonfire_open | 3 | 75 | 68 | 7 | 49 | 46 | error_stop |
| bonfire | 2 | 45 | 8 | 37 | 2 | 0 | complete |
| calprocure_open | 15 | 358 | 253 | 105 | 15 | 0 | complete |
| calprocure | 2 | 31 | 0 | 31 | 2 | 0 | complete |
| instantmarkets_open | 15 | 375 | 327 | 48 | 101 | 86 | budget_exhausted_partial |
| instantmarkets | 6 | 141 | 0 | 141 | 6 | 0 | complete |
| nationwide_commodities | 174 | 4350 | 0 | 20 | 591 | 417 | aborted_excluded_source_flood |
| opengov_open | 15 | 375 | 283 | 92 | 60 | 45 | budget_exhausted_partial |
| opengov | 8 | 192 | 63 | 129 | 8 | 0 | complete |
| planetbids_open | 15 | 375 | 296 | 79 | 31 | 16 | budget_exhausted_partial |
| planetbids | 4 | 85 | 30 | 55 | 4 | 0 | complete |
| publicpurchase_open | 4 | 88 | 87 | 1 | 4 | 0 | complete |
| publicpurchase | 1 | 1 | 0 | 1 | 1 | 0 | complete |

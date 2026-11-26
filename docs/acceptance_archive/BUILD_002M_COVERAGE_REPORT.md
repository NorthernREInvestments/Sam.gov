# Build 002M — Coverage Report

**Do not claim nationwide completeness without source-level evidence.**

| Source | Completion | Remaining pages |
|--------|------------|----------------:|
| opengov | complete | 0 |
| bonfire | complete | 0 |
| planetbids | complete | 0 |
| calprocure | complete | 0 |
| publicpurchase | complete | 0 |
| instantmarkets | complete | 0 |
| all_priority_commodities | complete | 0 |
| nationwide_commodities | aborted_excluded_source_flood | 417 |
| opengov_open | budget_exhausted_partial | 45 |
| bonfire_open | error_stop | 46 |
| planetbids_open | budget_exhausted_partial | 16 |
| calprocure_open | complete | 0 |
| publicpurchase_open | complete | 0 |
| instantmarkets_open | budget_exhausted_partial | 86 |

## Benchmarks (historical, not production counts)

- ~3,899 listings across five major state/local portals (prior observation)
- ~2,500 InstantMarkets mixed-source (prior observation)

## This backfill

- Authorized search calls: ≤300 @ page_size=25
- Theoretical max rows: 7,500
- Actual unique new: 101
- Actual durable inventory: 2255
- SAM.gov / DLA: disabled

## Gaps

- **nationwide_commodities**: status=aborted_excluded_source_flood, remaining_pages=417, provider_total_pages=591
- **opengov_open**: status=budget_exhausted_partial, remaining_pages=45, provider_total_pages=60
- **bonfire_open**: status=error_stop, remaining_pages=46, provider_total_pages=49
- **planetbids_open**: status=budget_exhausted_partial, remaining_pages=16, provider_total_pages=31
- **instantmarkets_open**: status=budget_exhausted_partial, remaining_pages=86, provider_total_pages=101

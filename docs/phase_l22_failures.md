# Phase L.2.2 — Failures

Explicit drop reasons observed in live + unit coverage.

| Reason | Meaning | Live note |
|---|---|---|
| `NO_EXACT_IDENTITY` | No MPN/SKU/model+mfr commercial key (or NSN-only skipped) | Dominant among 269 resale survivors |
| `EXPIRED` | Deadline gate | 168 removed before market |
| `MIXED_PRODUCT_SERVICE` / `SERVICE` / `REPAIR_OVERHAUL` / `ENGINEERING_SUPPORT` | Fitness gate | Preserved from L.2.1 |
| `NO_SEARCH_RESULTS` | Providers returned no usable commercial URLs | Common for obscure MPNs |
| `FETCH_FAILED` | `fetch_public_text` not `ACCESS_OK` | Bot-block / 404 / empty |
| `SEARCH_PAGE_ONLY` | Listing/search/homepage — discovery only | Telemetry + unit tests |
| `PRODUCT_IDENTITY_MISMATCH` | Page fetched but identity failed | 6 product pages in final run |
| `NO_PRODUCT_URL` | No extractable product link from listing | Still frequent on JS shells |
| `NO_PRICE_ON_MATCHING_PAGE` | Identity OK but no clean price | Covered in resolver |
| `USED_ONLY` | Refurb/used/open-box | Economics blocked |
| `CONFIGURATION_MISMATCH` | Spec conflict (RAM/storage etc.) | Unit-tested |
| `PRICE_NOISE` | Shipping/finance/`$1` script noise | Unit-tested |
| `PRICE_TOO_WEAK` / `APPROXIMATE_NOT_ECONOMICS` | Below economics bar | Research-only |

## What is *not* a failure

- Search provider empty ≠ market architecture broken when direct-domain discovery still runs.
- 0 verified prices with 6 identity mismatches = gate working, not silent UNKNOWN collapse.

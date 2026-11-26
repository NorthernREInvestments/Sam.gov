# Build 002F — Audit (from Build 002E job)

**Job:** `0db24e0c-69df-44ad-bbc9-f9c73d6e8825`  
**Paused for replan:** yes (`002F_replan_progressive_qualification`)

## Queue snapshot (at pause)

| Bucket | Pending | Done/error | Notes |
|--------|--------:|-----------:|-------|
| Total | 4,555 | 3,081 | total job 7,636 |
| unknown_mixed | 3,739 | 2,522 | Dominant; many full-package attempts |
| product_supply | 259 | 477 | Legitimate full-package candidates |
| product_with_incidental_services | 2 | 50 | Eligible |
| product_rental_leasing | 6 | 23 | Eligible |
| product_with_significant_installation | 1 | 6 | Eligible |
| services | 349 | 3 | **Should not auto full-package** |
| construction | 81 | 0 | **Should not auto full-package** |
| professional_services | 57 | 0 | **Should not auto full-package** |
| maintenance_repair_services | 52 | 0 | **Should not auto full-package** |
| software_only | 9 | 0 | **Should not auto full-package** |
| forecast (status) | 1,234 pending | — | Not active bid packages |

Done package outcomes were mostly `VERIFICATION_REQUIRED` (2,572) — full acquire spent on thin/listing URLs without eligibility gating.

## Unnecessary work already performed

- Confirmed non-product completed in queue: low count in *done* classes (services≈3) because prioritization processed products/unknown first — but **pending still holds ~548 confirmed non-product + 1,234 forecasts** that would otherwise burn full-package time.
- Primary waste mode: **unknown_mixed + listing URLs** receiving full acquire → VERIFICATION_REQUIRED/FAILED without business eligibility.

## Root causes of slow runtime (002E)

1. Serial batch processing (~15–25 items/min observed).  
2. Full package path for every pending row (detail + all attachments).  
3. No progressive qualification before download.  
4. Repeated work on already-known classifications.  
5. Per-request HTTP clients; limited connection reuse.

## 002F corrections

1. Progressive qualification before automatic full-package acquisition.  
2. Replan pending: skip confirmed non-product / inactive; targeted investigate uncertain; full package only for eligible.  
3. Concurrent workers with per-source limits + timing instrumentation.  
4. Reuse existing packages/documents (content-hash / skip unchanged).  
5. Preserve unrestricted discovery; manual acquire override unchanged.

# BUILD 002Q — Permanent NextBid Repair & Full Inventory Plan

**Generated:** 2026-11-23T00:41:28.864887+00:00

## Status

- Phase 1 permanent repair: **COMPLETE (offline)**
- Phase 2 full acquisition: **PREPARED — NOT EXECUTED**
- Acquisition lock: **ENGAGED** (awaiting owner authorization)
- Metered NextBid delta this run: **0**
- OpenAI spend this run: **$0**

## Defects corrected

- Paid responses now persist to durable store BEFORE parse (nextbid_response_store + metered_request).
- Request identity + durable/cache lookup + in-flight dedupe before every metered call.
- Uncertain HTTP outcomes count as consumed; max_retries hard-capped; no uncontrolled retry.
- Provider usage discrepancy stop + reconcile baseline vs /usage.
- Legacy HTTP cache migrated into durable paid-response store.
- Search-cache unique IDs upserted into inventory; thicker listings never overwritten by thin.
- Full-text store consumed by production qualification pipeline (prior + reprocess).
- Acquisition remains fail-closed locked; scheduler cannot bypass killswitch.

## Offline safety-test results

- All passed: **True**
- `paid_response_survives_restart`: True
- `parse_failure_preserves_original`: True
- `extraction_rerun_offline`: True
- `duplicate_request_identity_stable`: True
- `scheduler_blocked_by_default`: True
- `concurrent_workers_respect_job_budget`: True
- `scheduler_cannot_bypass_killswitch`: True
- `failed_requests_no_uncontrolled_retry`: True
- `provider_discrepancy_stops_acquisition`: True
- `complete_not_overwritten_by_thin`: True
- `missing_info_not_auto_dq`: True
- `acquisition_relocked`: True

## Recovered inventory

| Metric | Value |
|---|---|
| Unique IDs in search cache | 6359 |
| Inventory before | 6585 |
| Inventory after | 6585 |
| Previously missing recovered (this run) | 0 |
| Cumulative missing recovered (directive) | 4330 |
| Inventory before directive | 2255 |
| Usable full_text after | 1863 |
| Verified quantities | 88 |
| Ready for bidding (B) | 0 |
| Durable paid-response files | 352 |

## Owner views (do not show unresolved as ready)

- **A_eligible_ready_supplier_pricing**: 88
- **B_eligible_ready_to_submit**: 0
- **C_eligible_registration_required**: 0
- **D_information_recovery_needed**: 6192
- **verified_quantities**: 88
- **confirmed_disqualifications**: 392
- **actionable_ready_list_note**: Ready-to-bid counts only include bid_eligibility-confirmed rows; unresolved/DQ are excluded from A/B.
- primary_dispositions: `{"verified_bid_opportunities": 0, "verified_product_information_needed": 2087, "potential_product_classification_needed": 4105, "easy_vendor_registration": 0, "confirmed_disqualification": 392, "closed_expired_non_solicitation": 1}`

## Actual actionable opportunity list

- Ready to submit (B): **0**
- Supplier-pricing / qty-verified candidates (not bid-ready): **92**
- Information recovery needed (D): **6192**
- Note: None are ready-to-submit (B=0). Qty-verified rows remain information-recovery / supplier-pricing candidates until procurement gates pass.

| nb_id | qty | agency | title |
|---|---|---|---|
| NB00348901 | 12 | Chicago Transit Authority | C26RI103317777R - REBID - Invitation For Bids (Non- Negotiable) - Furnish and De |
| NB00321554 | 1 | Monterey One Water | PURCHASE OF ONE (1) 6” HIGH PRESSURE DIESEL PUMP  WITH A TRAILER |
| NB00345848 | 2 | City Of Lubbock | Two (2) Animal Transport Vehicles |
| NB00370595 | 1 | santarosafl | Purchase of one Rebuilt Caterpillar 836G landfill compactor utilizing a trade-in |
| NB00375339 | 53 | stmaryscountymd | Unication G5 Dual Band P25 Voice Pagers 700-800 MHz/VHF (136-174 MHz) |
| NB00291906 | 24 | habd | QU27003 Fire Protection Services |
| NB00339475 | 2 | San Diego International Airport | Two (2) New/Unused 2026 or Newer Hybrid Full Size Light Duty four (4) door pick  |
| NB00365477 | 2 | City of Pasadena | Police Department Off Highway Vehicles (OHV) |
| NB00338603 | 160 | The Metropolitan Water District of Southern California | Furnish one hundred sixty (160) Chemical Flow Meters for the Metropolitan Water  |
| NB00278207 | 3 | Gabrielli Kenworth if New Jersey, LLC | Purchase of Three (3) 10-Ton and Two (2) 20-Ton Telescopic Cranes Trucks |
| NB00331651 | 1 | Dept of Parks & Recreation | C26635024 - Helicopter transport services for Panther Gap in Humboldt Redwoods S |
| NB00260314 | 1 | City of Bangor | Towable Air Start Unit |
| NB00261760 | 1 | GWINNETT COUNTY GOVERNMENT | BL081-24 Purchase of a Swift Water Rescue Boat |
| NB00326707 | 1 | Parks Recreation & Historic Preservation, NYS Office of | One (1) Compact Tracked Articulating Boom Lift with Transport Trailer for Sarato |
| NB00375434 | 7 | The Metropolitan Water District of Southern California | East Penn Manufacturing Company Station Batteries (7 units) RFB-LB-481905 |
| NB00372789 | 1 | clermontfl | Purchase and Delivery of Intelligent Auto-Flusher |
| NB00361285 | 2 | Bay County | 27-07 North Bay Booster Station and 5MG Storage Tank Designs |
| NB00348882 | 600 | City of Pasadena | TREES FOR PLANTING |
| NB00380234 | 18 | Department of Fish & Wildlife | Smoke Detector Removal and Replacement |
| NB00303615 | 4 | DeKalb County | MEDICAL SERVICES (ANNUAL CONTRACT WITH FOUR (4) OPTIONS TO RENEW) |
| NB00362719 | 1 | Kern High School District | ABLE Replacement of Modular Toilet Building Bid No. 7333 |
| NB00367491 | 15 | City of Huntington Beach | Zone 4 ADA Ramps CC-1879 |
| NB00328155 | 25 | Pennsylvania Department of Environmental Protection | Cleaning Out and Plugging Twenty-Five (25) Orphan Gas Wells Wetmore Township, Mc |
| NB00362607 | 2 | Dept of Food & Agriculture | IFB 26-0094 - Smith River BPS HVAC Split System (Del Norte County) - Public Work |
| NB00362602 | 8 | Metrolink (SCRRA) | F125 Wheel Sets & BTR Wheel Assemblies IFB No. PO1071-27 |


## Phase 2 — Exact estimated credit requirements (NOT spent)

| Item | Credits |
|---|---|
| Full listing enumeration (low–high) | 264 – 1000 |
| Targeted detail + docs (low–high) | 5760 – 21875 |
| Total campaign (low–high) | 6024 – 22875 |
| Remaining monthly reserve | 1000 |
| Spendable now after reserve | 1646 |

Progressive evaluation: listing-first within daily ≤100; detail only product/ambiguous candidates; retain unresolved IDs when credits insufficient. Full campaign high exceeds current spendable (1646) by 21229 credits — prioritize and carry forward.

## Provider usage before / after

- Before: `{"ok": true, "remaining": 2646, "used": 3354, "allowance": 6000, "raw_ok": true}`
- After: `{"ok": true, "remaining": 2646, "used": 3354, "allowance": 6000, "raw_ok": true}`

## Confirmation

- All acquisition remains **LOCKED**.
- No Railway deploy. No Build 003. No new sources.
- **STOP — AWAITING OWNER AUTHORIZATION** before any unlock or spend.

# Build 002J — Recovery Report

**Build ID:** `20261122-m3-2-build-002j`  
**Metered NextBid calls this build:** **0**  
**Provider used (unchanged by 002J):** 3054 → still 3054 after free probes only.

## Offline reprocessing (paid data already acquired)

| Metric | Count |
|--------|------:|
| Records examined | 600 |
| Stage C strict fits recovered | **3** |
| Stage B product opportunities | 307 |
| Stage A active | 276 |
| Prior free-source fits (separate) | 3 |
| Inventory rows upserted | 600 |

## Recovered Stage C evidence (NextBid-paid corpus)

| nb_id | Qty | Source | Title (abbrev) |
|-------|----:|--------|----------------|
| NB00375339 | 53 est. | opengov | Unication G5 Dual Band P25 Voice Pagers |
| NB00339475 | 2 | planetbids | Two (2) Hybrid Full Size Light Duty pickups |
| NB00326707 | 1 | instantmarkets | One (1) Compact Tracked Articulating Boom Lift |

Full evidence objects: `docs/BUILD_002J_ACCEPTANCE.json` → `strict_m3_fit_evidence`.

**Note:** Build 002I claimed 6 Stage C fits. Offline reprocess with current quantity/integrity gates recovers **3**. The other three failed current ordered-quantity / writeback integrity checks and are **not** counted here.

Prior free-source Georgia/Delaware packages remain in `prior_free_source_evidence` (not double-counted).

## Coverage

Status: **UNVERIFIED**  
917 discovered ≠ ~3,899 portal benchmark. Pagination was capped; commodities tradeline was not fully paged. Closing gaps requires owner-authorized metered budget (proposed max **100** under hard daily ceiling).

## Outstanding document issues

- Portal HTML shells (PlanetBids/OpenGov/Bonfire) often need vendor session.
- Ordered quantity still unpublished for most Stage B goods in accessible text.
- Free `/account/activity` route unavailable (`no_such_route`) — cannot fully reconstruct the 710-call gap from provider activity API.

## Protections now live

- Fail-closed emergency stop (survives restart/deploy).
- Central controller: atomic reserve, daily ≤100, job ≤50, monthly reserve ≥1000.
- Scheduler/API run blocked without owner authorization file.
- Tests: `tests/test_nextbid_controller_002j.py` — **9 passed**, 0 metered calls.

## Proposed plan

**NOT EXECUTED.** See acceptance `proposed_acquisition_plan`. Max projected under current owner rule: **100 metered calls** for any authorized slice.

Await owner acceptance. **Do not resume paid acquisition. Do not begin Build 003.**

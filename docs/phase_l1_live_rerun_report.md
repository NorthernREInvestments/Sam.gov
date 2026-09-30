# Phase L.1 — Live Hunt Re-run Report

**Generated from:** `artifacts/phase_l/hunt_latest.json`  
**Phase tag:** L.1  
**Constraint:** DEVELOPMENT_NO_OUTREACH — no auto-registration

## Verdict

Access gate repair succeeded. Open-market / easy-registration opportunities now classify `our_bid_access = YES`. Economics is **allowed and attempted** on every YES row (`economics_researched = 483`). Profit tiers remain 0 because live discovery did not attach historical award or public retail unit prices in this pass (`HISTORY_RESEARCH_PENDING`).

## Funnel (post L.1)

| Metric | Federal | State | Local | Cooperative | Total |
|---|---:|---:|---:|---:|---:|
| Live opportunities | 500 | 1143 | 579 | 10 | 2232 |
| Tangible candidates | 369 | 140 | 51 | 3 | 563 |
| Access YES | 290 | 139 | 51 | 3 | **483** |
| CONDITIONAL/UNKNOWN | 0 | 0 | 0 | 0 | **0** |
| NO | 79 | 1 | 0 | 0 | 80 |
| Easy registration only | 0 | 139 | 51 | 0 | 190 |
| Real eligibility blockers | 79 | 1 | 0 | 0 | 80 |
| Recurring buyers needing registration | 0 | 1 | 2 | 0 | **10** |
| Economics researched | 290 | 139 | 51 | 3 | **483** |
| History found | 0 | 0 | 0 | 0 | 0 |
| Offer count found | 0 | 0 | 0 | 0 | 0 |
| Retail/acquisition price found | 0 | 0 | 0 | 0 | 0 |
| ≥$10K gross spread | 0 | 0 | 0 | 0 | 0 |
| ≥$10K expected net | 0 | 0 | 0 | 0 | 0 |
| ≥$25K / ≥$50K / ≥$75K / ≥$100K expected net | 0 | 0 | 0 | 0 | 0 |

## Comparison to Phase L (pre-repair)

| | Phase L | Phase L.1 |
|---|---:|---:|
| Access YES | 0 | 483 |
| CONDITIONAL/UNKNOWN | 480 | 0 |
| Access NO | 80 | 80 |
| Economics researched | 0 | 483 |

## Primary true blockers (NO)

1. `sole_source` — 61  
2. `source_approval_required` — 17  
3. `vehicle_not_held:BOAST_BOA` — 2  

## Registration intelligence

- Registration actions produced: 21 (per-source)
- Recurring buyers (`REGISTER_NOW_RECURRING_BUYER`): 10
- Owner action queue entries: 31 (`ACTION_REGISTER_PORTAL`, no auto-register)
- Tracker: `data/buyer_portal_registration_tracker.json`

## Discovery health

- Federal public SAM: 500 rows, `DISCOVERY_NORMAL`
- Live runner: 40 attempted / 16 successful / 1732 unique records
- Targets (≥1000 total, 400/300/300 mix): **met**

## Next bottleneck (not in L.1 scope)

History + retail research on the 483 YES pool so profit tiers can populate. Access is no longer the blocker.

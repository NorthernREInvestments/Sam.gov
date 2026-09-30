# Phase L.2.9 — Live Report

**Build:** `20260927-m3-phase-l29-maximum-source-coverage`  
**Verdict:** `PHASE_L29_PARTIAL_MAXIMUM_SOURCE_EXPANSION`  
**STOP:** No Phase M. No bids. No supplier contact. No financing.

## Opportunity coverage

| Stage | Count |
|-------|------:|
| Raw access=YES | 482 |
| Stage 1 | 284 |
| Stage 2 | 81 |
| Stage 3 | **81** |
| Stage 3 processed | **81 / 81** |
| Deep research queue | 23 |

## Source expansion (live)

| Source family | Attempts | Exact hits | Price leads | Verified | History hits | Blocks |
|---------------|--------:|----------:|------------:|---------:|-------------:|-------:|
| USAspending | 81 | 0 | 0 | 0 | **14** | 0 |
| cooperative contracts | 81 | 0 | 0 | 0 | 0 | 0 |
| distributors | 60 | 0 | 9 | 0 | 0 | 13 |
| state term contracts | 54 | 0 | 0 | 0 | 0 | 0 |
| government board records | 42 | **10** | 0 | 0 | **10** | 8 |
| open-data datasets | 6 | 0 | 0 | 0 | 0 | 0 |

New families implemented in code (inventory): OMNIA, Sourcewell, NASPO, GSA, SEWP, DLA/DIBBS, state portals (WA DES priority + 13 states), board records, Socrata open-data, OEM/dealer/distributor pools, static PDF/XLSX.

## Historical intelligence

- History found: **15** (was 14)
- Board-record history hits: **10**
- History+price joins: 0

## Current price intelligence

- Price leads: **10**
- Strong leads: **10**
- Corroborated: 0
- Verified / usable: **0 / 0**
- Price-evidence coverage: **7.4%** of Stage 3

## Economics

- Both found: 0
- Apparent positive: 0
- Verified positive: 0
- ≥$10K / $25K / $50K: 0

## Recurring-buy engine

- Products tracked: **15**
- Buyers tracked: 1
- Known product economics persisted: 0
- Reverse-hunt live matches: **19**

## Bot resilience

- Blocked events: 13
- Alternate attempts: 27
- `PRICE_NOT_RECOVERED_AUTOMATICALLY`: 3
- Manual verification queue: **6** (no fixed cap)
- Circuit-breaker skipped: bobcat, digikey, fastenal, ford, grainger, machinerytrader, mscdirect, sourcewell, zoro, socrata

## Failure taxonomy

| Class | Count |
|-------|------:|
| NO_PRICE_INFORMATION | 72 |
| STRONG_PRICE_LEAD_REQUIRES_VERIFICATION | 6 |
| PRICE_NOT_RECOVERED_AUTOMATICALLY | 3 |

## Remaining bottleneck

Source **universe** is wider (state adapters, board discovery, open-data, parallel branches, reverse hunt), but live HTML storefronts remain captcha/soft-blocked and most Stage 3 rows are mil-spec NSNs without public commercial price rows. Board records produced identity/history hits without yet converting to acquisition verify. Next leverage: deeper static coop/state PDF harvest and authorized-dealer inventory pages that serve non-JS documents.

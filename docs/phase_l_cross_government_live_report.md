# Phase L — Cross-Government Live Report

**Generated from:** `artifacts/phase_l/hunt_latest.json`  
**Mode:** `DEVELOPMENT_NO_OUTREACH`  
**SAM personal API:** `SAM_API_AUTH_FAILED` (not required — public SGS search used)

---

## Discovery funnel

| Metric | Federal | State | Local | Cooperative | Total |
|---|---:|---:|---:|---:|---:|
| Raw live opportunities | 500 | 1143 | 579 | 0 | **2222** |
| Tangible products | 369 | 140 | 51 | 0 | **560** |
| Access checked | 369 | 140 | 51 | 0 | **560** |
| `our_bid_access = YES` | 0 | 0 | 0 | 0 | **0** |
| Inaccessible (`NO`) | 79 | 1 | 0 | 0 | **80** |
| Unknown/Conditional | 290 | 139 | 51 | 0 | **480** |
| Exact/or-equal identity resolved | 150 | 3 | 0 | 0 | **153** |
| Historical pricing found | 0 | 0 | 0 | 0 | **0** |
| Offer count found | 0 | 0 | 0 | 0 | **0** |
| Retail price found | 0 | 0 | 0 | 0 | **0** |
| ≥$10K gross spread | 0 | 0 | 0 | 0 | **0** |
| ≥$10K expected net | 0 | 0 | 0 | 0 | **0** |
| ≥$25K / ≥$50K / ≥$75K / ≥$100K | 0 | 0 | 0 | 0 | **0** |

### Source mix vs targets

| Level | Target | Actual | Status |
|-------|-------:|-------:|--------|
| Total | 1000 | **2222** | Met |
| Federal | 400 | **500** | Met (public SAM SGS) |
| State | 300 | **1143** | Met |
| Local + Cooperative | 300 | **579** | Met via Local/Network |
| Cooperative alone | — | **0** | Shortfall — coop portals returned no usable open solicitations this run |

### Live runner health

- Sources attempted: 36  
- Successful: 15  
- Failed: 20  
- Auth blocked: 1  
- Bot blocked: 2  
- Federal public search: `DISCOVERY_NORMAL` (500 unique)

---

## Accessibility

| Result | Count |
|--------|------:|
| YES | 0 |
| NO | 80 |
| CONDITIONAL / UNKNOWN | 480 |

**Primary blockers**

1. `open_competition_but_company_registration_unconfirmed` (469) — empty company profile / SAM status UNKNOWN  
2. `sole_source` (61)  
3. `source_approval_required` (17)  
4. `small_business_status_unconfirmed` (8)  
5. `vehicle_not_held:BOAST_BOA` (2)

Fail-closed is correct: with no confirmed SAM/SB/vehicle holdings, M3 must not invent `YES`.

---

## Competition distribution

No historical offer counts were attached at listing-screen depth this run (history/retail deferred until access YES).

| Bucket | Open-comparable | Pre-filtered |
|--------|----------------:|-------------:|
| 1 / 2 / 3 / 4–5 / 6–10 / 11–20 / 20+ | 0 | 0 |

Infrastructure for separated open vs pre-filtered distributions is in place (`phase_l/competition.py`); populate when access-YES deals receive history enrichment.

---

## Top 20 — `our_bid_access = YES`

**None.** No deal cleared the hard accessibility gate under the current empty company eligibility profile.

## Research queue (CONDITIONAL / UNKNOWN open-ish) — not actionable

These are the best **research** candidates for owner confirmation of SAM registration / SB status. They are **not** READY_FOR_OWNER_REVIEW.

| # | Level | Title (truncated) | Access | Type |
|--:|-------|------------------|--------|------|
| 1 | FEDERAL | Circuit Card Assembly | CONDITIONAL | OPEN_MARKET |
| 2 | FEDERAL | NSN: 6620013216819 PN:5076300… | CONDITIONAL | OPEN_MARKET |
| 3 | FEDERAL | 58--Government intends to procure… | CONDITIONAL | OPEN_MARKET |
| 4 | FEDERAL | Indicator, Temperatu | CONDITIONAL | OPEN_MARKET |
| 5 | FEDERAL | NSN:1680-00-902-2196 ACTUATOR… | CONDITIONAL | OPEN_MARKET |
| 6 | FEDERAL | Load Balancer | UNKNOWN | TOTAL_SMALL_BUSINESS |
| 7+ | FEDERAL | Additional NSN/product listings in `artifacts/phase_l/research_queue_latest.json` | CONDITIONAL/UNKNOWN | OPEN_MARKET / SB |

Next owner action to unlock YES: confirm SAM active registration + SBA size status in `data/company_eligibility_profile.json`, then re-run access gate (no new broad architecture required).

---

## Economics

No retail/history spend on inaccessible deals (by design).  
Expected-net tiers all **0** this run.

---

## Verdict precursor

See completion classification in the final summary: discovery/mix succeeded; accessibility YES and economic PASSes did not — blocked by company profile unknowns, not by missing search plumbing.

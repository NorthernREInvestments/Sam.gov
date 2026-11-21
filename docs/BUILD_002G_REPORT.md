# Build 002G Report — Discovery Identity, Recovery, Nationwide Expansion

**Build ID:** `20261121-m3-2-build-002g`  
**Acceptance artifact:** `docs/BUILD_002G_ACCEPTANCE.json`  
**Rule:** REAL DATA ONLY — no fabricated solicitations, evidence, or inflated metrics.

---

## Executive summary

Build 002G corrects the core 002F failure mode: treating open-data rows, forecasts, listings, and portal harvests as if they were individual solicitation packages.

| Population | Raw unique | Verified individual solicitations | Notes |
|------------|-----------:|----------------------------------:|-------|
| Frozen 002F job `0db24e0c-…` | 7,636 | **736** | Identity reclassification of full freeze |
| Fresh 002G nationwide run | **13,218** | **752** | Free sources only; OpenAI portals OFF |

Engineering targets (5,000 / 1,500 / 500 / 35 states) were **not** met by inventing records. States with operational sources: **38** (target 35 met). Genuine individual and product counts remain below preferred targets; shortfalls are explained below.

---

## Corrections implemented

1. **Record identity pipeline** (`m3/research/identity.py`) — 12 record types with evidence; only `INDIVIDUAL_SOLICITATION` enters the verified solicitation population.
2. **Discovery → identity → classification → progressive acquisition** wired in `engine.py`, `classify.py`, `qualification.py`, `population_acquire.py`.
3. **False COMPLETE prevention** — junk titles, non-solicitation identity, and zero-doc non-detail pages demoted; inline COMPLETE requires explicit empty inventory on a verified detail page.
4. **RAMP** — detail URLs recognized as individuals; Salesforce/dynamic shells → `ACCESS_RESTRICTED` (not fake document COMPLETE). Live probe: Socrata OK; documents not publicly extractable from static HTML.
5. **Mesa** — dataset marked historical; HTML board still HTTP **403**; Open-status rows often **0**. Owner action: optional free vendor account if HTML board needed.
6. **NJ PDF** — multi-notice boundary detection from extracted text (`split_multi_notice_text`); prevents treating multi-notice PDFs as one complete opportunity without split evidence.
7. **Nationwide seeds** — **194** free official seeds (cities, counties, universities, transit, ports, airports, co-ops) with purpose tags. Capability levels 0–4 tracked in coverage matrix.
8. **Source intelligence persistence** — last yield / capability written into `SourceRegistry.config`.

---

## Frozen population reconciliation (7,636)

| Record type | Count |
|-------------|------:|
| GENERAL_PROCUREMENT_INFORMATION | 3,458 |
| HISTORICAL_AWARD | 1,058 |
| PROCUREMENT_FORECAST | 1,072 |
| INDIVIDUAL_SOLICITATION | **736** |
| UNRESOLVED_RECORD | 675 |
| PROCUREMENT_LISTING | 426 |
| PURCHASING_PLAN | 163 |
| SUPPORTING_DOCUMENT | 17 |
| UNRELATED_CONTENT | 15 |
| PROCUREMENT_PORTAL | 15 |
| VENDOR_REGISTRATION | 1 |

- Verified individual **open/amended:** 454  
- Verified individual **unknown status:** 282  
- Non-opportunity artifacts: 5,782  
- Leads (listing/unresolved/announcement/supporting): 1,118  

**736 + leads + non-opportunity = 7,636** (reconciles).

---

## False COMPLETE

- COMPLETE packages before demotion: **495**  
- False COMPLETE identified & demoted: **14** (junk titles / zero-doc / non-individual)  
- Remaining COMPLETE in DB after demotion: **481** (includes historical acquisitions; not all meet initial M3 commercial criteria)

---

## Live recovery probes

| Platform | Result |
|----------|--------|
| **LA RAMP** | Socrata open bids OK; opportunity-details URLs are individual; static HTML is dynamic shell → attachments need RAMP session |
| **Mesa** | Socrata OK; Open filter empty in probe; `mesaaz.gov/business/purchasing` → **403** |
| **Delaware MMP** | Socrata + public bid detail HTTP 200 (working public detail route) |
| **NJ** | Purchase bureau listing reachable; PDF multi-notice splitter in place |

---

## Fresh nationwide acceptance (free sources)

| Metric | Value |
|--------|------:|
| Raw unique after dedupe | 13,218 |
| Duplicates excluded | 6,885 |
| Verified individual solicitations | **752** |
| Broad active product (class ∩ not inactive) | **260** |
| Initial M3 verified (strict: defined qty + class) | **0** |
| Initial M3 unknown | **476** |
| Evidence manifest product samples | **120** |
| Seed sources | **194** |
| States with operational sources | **38** |
| Capability L2+ states | **38** |
| Capability L4 states (from this run’s yield tags) | **0** |

Package DB totals (cumulative, not only this run): COMPLETE 481 / VR 4,622 / FAILED 669 / AR 226 / PARTIAL 47.

OpenAI spend: portals disabled for this acceptance run (budget preserved).

---

## Engineering target honesty

| Target | Actual | Met? |
|--------|-------:|:----:|
| ≥5,000 genuine active solicitations | 752 verified individuals | No |
| ≥1,500 verified product | 260 broad active product | No |
| ≥500 complete usable product packages | Not claimed as 500 verified product COMPLETE | No |
| ≥35 states operational | 38 | **Yes** |

**Why short:** Most new seeds are Level 0–1 HTML portals until live detail/package yield is proven. NYC/Cook/Mesa mass correctly removed from verified solicitation counts. RAMP individuals exist without public attachment packages. No fabrication.

---

## Acceptance gates

See `BUILD_002G_ACCEPTANCE.json` → `gates`. Highlights:

- DATA_INTEGRITY / NO_FABRICATED_LIVE_RECORDS / RECORD_IDENTITY_ACCURACY / DISCOVERY_INVARIANT / FALSE_COMPLETE_PREVENTION / NATIONWIDE_SOURCE_EXPANSION / PROGRESSIVE_ACQUISITION / BUDGET_ENFORCEMENT → **VERIFIED**
- RAMP / MESA / NJ recovery → **PARTIALLY VERIFIED** (mapped + probed; attachments/auth still limited)
- DEPLOYMENT_VERIFIED → set after Railway `/healthz` confirms build id

---

## Owner actions (optional, not performed)

1. Free LA RAMP vendor account — unlock attachment inventory for ~hundreds of RAMP individuals.  
2. Free Mesa vendor access — only if HTML board is required beyond Socrata archive.  
3. Do **not** begin Build 003 until owner acceptance of 002G.

---

## What was not done

- No paid procurement databases  
- No fabricated opportunities  
- No Build 003  
- No destructive DB reset  
- Parked SAM/BidNet/Euna remain disabled  

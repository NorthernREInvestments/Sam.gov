# Phase F — Validation Scorecard

**Starting baseline:** 1725 passed / 0 failed  
**Final full suite:** 1752 passed / 0 failed (2479.51s)  
**Method:** Factual PASS / PARTIAL / FAIL / NOT TESTED

| Domain | State | Cases / evidence | Highest remaining note |
|--------|-------|------------------|------------------------|
| Discovery/intake | PARTIAL | R_13, R_14 REAL_SOURCE | No live bulk scrape (stop rule) |
| Classification | PASS | R_11 | — |
| Identity | PASS | R_01, R_02, R_11, R_13 | — |
| Quantity/UOM | PASS | R_04, R_05, R_07, R_14 | HD trap detected |
| History | PASS | History UI UNKNOWN + empty | Thin learning data volume |
| Supplier | PASS | R_02, R_11, R_12 | Public ≠ validated |
| Economics | PASS | R_10–R_12 | — |
| Packaging | PASS | R_03, R_12 | MIL without false READY |
| Delivery | PASS | R_05, R_01, R_13 | Multi-FOB |
| Inspection | PASS | R_08, R_09 | FAT + source |
| Acceptance | PASS | R_02, R_09 | — |
| Technical data | PARTIAL | incidental | Thin dedicated TDP |
| Submission | PASS | R_06, R_12 | Amendment ack |
| Financing | PASS | R_10 | Cash/PG blocks |
| Owner readiness | PASS | R_01, R_10–R_12 | Positive + negative |
| Operator workflow | PASS | F-G008 next-action fix | — |
| Dashboard consistency | PASS | gate-only READY + buckets | — |
| Deep Dive usability | PASS | blockers + next-action | — |
| Pipeline consistency | PASS | shared enrich | — |
| History (UI) | PASS | UNKNOWN preserved | — |
| Post-award | PARTIAL | R_08 + transitions | Full invoice path thin |
| Invoice/payment | PARTIAL | transitions DELIVERED→PAID | Not full WAWF |

## Live-test readiness

**LIMITED LIVE TEST READY** — critical READY/block path proven on reality corpus including two REAL_SOURCE cases; suite must be 0-fail at completion; no autonomous external actions; next live phase should be supervised intake only.

# Phase F — Reality Gap Register

**Started:** 2026-09-23  
**Starting baseline:** 1725 passed / 0 failed (`docs/phase_f_baseline.md`)  
**Final suite:** **1752 passed / 0 failed** in 2479.51s — `docs/phase_f_completion_report.md`

Priority: P0 = bad bid / financial loss / false readiness · P1 = missed good deal / operator failure · P2 = confusing UI · P3 = polish

| ID | Case | Screen | Expected | Actual | Severity | Business risk | Root cause | Module | Fix status | Regression test |
|----|------|--------|----------|--------|----------|---------------|------------|--------|------------|-----------------|
| F-G001 | suite | — | `test_dependency_blocking` stable | Flaked under full suite | P1 | Suite noise | Shared AppSetting action index contention | action orchestration + test isolation | **FIXED** | action orchestration memory fixture |
| F-G002 | UI | Deep Dive / cards | Blockers in plain English | Raw codes title-cased | P1 | VA cannot act | `plainBlockers` weak | `m3-mobile.js` | **FIXED** | `test_ui_blocker_map_has_financing_plain_english` |
| F-G003 | UI | Deep Dive | No internal evidence enums | `LEVEL_4_UNKNOWN` shown | P2 | Confusing | Raw evidence level | `m3-mobile.js` | **FIXED** | `test_ui_no_primary_stage_jargon` / enum audit |
| F-G004 | R_04 | Quantity | HD detected + total pieces | Missing HD pattern | P0 | 100× economics | Extract gap | `extract.py` | **FIXED** | `test_r04_uom_trap_*` / `REGRESSION_QUANTITY` |
| F-G005 | R_02/R_12 | Ready | Public price ≠ READY | Blocked | — | — | E.1 gate | readiness | **PASS** | R_02, R_12 |
| F-G006 | R_01/R_11 | Ready | Good deals READY | READY | — | — | Positive path | readiness | **PASS** | R_01, R_11 |
| F-G007 | Corpus | — | REAL_SOURCE cases | R_13 Opp199 + R_14 Iowa | P2 | Live gaps | Only realistic-frozen earlier | corpus | **FIXED** | `test_r13_r14_*` |
| F-G008 | Next action | Deep Dive | Executable VA instructions | Generic stage text | P1 | VA unsure | `_next_action` + no gate prefer | resolver + enrichment | **FIXED** | `test_fg008_*` / `REGRESSION_READY` |
| F-G009 | Transitions | — | Illegal EXPIRED→READY / REJECTED→ACTIVE blocked | Helper needed | P1 | Bad reactivation | No transition check | `transitions.py` | **FIXED** | `test_state_transitions_*` |
| F-G010 | History | History | UNKNOWN preserved | Thin profit-only line | P2 | Invented clarity | Sparse history cards | `m3-mobile.js` | **FIXED** | `test_history_ui_preserves_unknown_*` |
| F-G011 | suite | Dashboard | Full suite completes | Hung ~78% after mid-suite noise | P1 | Cannot certify | `mobile_dashboard_summary` enriched every durable row | `m3_mobile_read_model.py` | **FIXED** | full suite 1752/0 |

## Open vs fixed

| Status | Count |
|--------|-------|
| FIXED | 9 |
| PASS (no defect) | 2 |
| OPEN | 0 |
| ACCEPTED_LIMITATION | Live bulk scrape / autonomous bid still out of scope by Phase F stop rule |

## Repair rule

Only defects revealed by baseline flake, reality matrix, and UI torture were repaired. No architecture redesign. No supplier-intelligence phase started.

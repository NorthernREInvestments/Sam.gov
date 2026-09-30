# Phase G — Live Source Gap Register

**Run:** 2026-09-24  
**Rule:** No open P0 allowed for Phase G completion.

| ID | Sev | Source | Opportunity | Observed | Expected | Impact | Smallest safe fix | Test | Status |
|----|-----|--------|-------------|----------|----------|--------|-------------------|------|--------|
| G-G001 | P2 | phase_g/live_batch | enriched product set | Primary action labeled VERIFY_UOM while gate next-action said Economics | Action priority should follow severity/actionability order | Operator confusion (safe) | Reorder `_operator_action` / `_blocker_bucket` | `test_phase_g_provenance.py` + rescore | **FIXED** |
| G-G002 | P1 | SAM listing-only path | All 100 enriched DLA/SAM products | `history_class=NONE` for all; no USASpending/history attach in Phase G batch | Historical price attempt on viable product candidates | Incomplete review (not false ready) | Accept for Phase G; wire history retrieval in a later supervised phase — do not fabricate | documented | **ACCEPTED_LIMITATION** |
| G-G003 | P1 | Document/TDP | Enriched listings | Attachment/TDP blockers rarely explicit; submission/delivery UNKNOWN instead | Clear REVIEW_TDP when package required but missing | Incomplete review | Measure via submission/delivery unknowns; deep document recovery not run in this batch (cost/stop) | documented | **ACCEPTED_LIMITATION** |
| G-G004 | P2 | Cheap screen | ~650 UNKNOWN/ambiguous survives | Held rather than reject | Optional tighter product filter | Operator noise | Do not weaken safety; optional later | documented | **ACCEPTED_LIMITATION** |
| G-G005 | P1 | DIBBS direct | — | Not exercised (bot/403) | Direct DIBBS RFQ pull | Coverage gap | Continue SAM DLA cross-publish only; no bot bypass | preflight | **ACCEPTED_LIMITATION** |
| G-G006 | P2 | SAM API | Bootstrap | Stopped at `SAM_API_BUDGET` after 8 calls / 800 opps | Enough volume for Phase G | Truncation only | Volume already sufficient (800) | — | **CLOSED** |
| G-G007 | P0 | Owner gate | READY set | 0 READY; blockers present on enriched set | No false READY on listing-only | — | None required | ready audit | **PASS** |

## Counts

| Severity | Open | Fixed | Accepted | Pass |
|----------|-----:|------:|---------:|-----:|
| P0 | 0 | 0 | 0 | 1 |
| P1 | 0 | 0 | 3 | 0 |
| P2 | 0 | 1 | 2 | 0 |

**Open P0:** 0

# Phase L.11 — Legacy Cleanup

**Build:** `20260928-m3-phase-l11-exact-award-history-recovery`

## Reconciled

| Item | Action |
|------|--------|
| Duplicate buyer-history search | Canonicalized in `exact_history_recovery.BUYER_FIRST_ORDER` (supersedes ad-hoc L.8/L.10 loops for Gov D) |
| Platform history vs discovery | Kept separate: `platform_history.py` vs live fetchers |
| Unbounded live_runner hang | Replaced commercial_feed path with `resilient_hunt` per-source process watchdog + checkpoint |
| Duplicate timeout configs | Hunt uses `DEFAULT_SOURCE_TIMEOUT_S` / `RequestBudget.timeout_seconds`; circuit breaker via source health |
| Fixed Stage 3 caps | Still asserted false (`STAGE3_NO_ROW_CAP`) |
| Loose Gov promotion | Unchanged — exact match rules only (`RULE_GOV_A_*` / `RULE_GOV_B_*`) |

## Retained for replay

`l6`–`l10_rescue` historical runners — not alternate live paths.

## Live entrypoints

- Workflow: `CanonicalOpportunityWorkflow`
- History: `run_exact_history_recovery`
- Hunt: `discover_live_sources` → resilient for `commercial_feed`
- Runner: `phase_l.l11_rescue.run_phase_l11_award_history`

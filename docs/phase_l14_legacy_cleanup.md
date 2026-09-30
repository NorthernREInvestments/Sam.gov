# Phase L.14 — Legacy Cleanup

**Build:** `20260928-m3-phase-l14-nonbidnet-source-expansion`

## Audit before new sources

Reconciled via `phase_l.legacy_cleanup`:

- stale/nonfunctional platform adapters → capability matrix + parked taxonomy
- duplicate discovery / award-history branches → canonical live fetchers + `platform_history_adapters`
- obsolete pagination / unbounded fetch → resilient hunt (timeout, retry, size, circuit breaker, checkpoint)
- fixed count caps → `STAGE3_NO_ROW_CAP`, `DEEP_RESEARCH_NO_FIXED_COUNT`
- BidNet-as-current-priority → obsolete `LEGACY_BIDNET_AUTH_HISTORY_AS_CURRENT_PRIORITY`
- commercial_feed BidNet-only → obsolete `LEGACY_COMMERCIAL_FEED_BIDNET_ONLY`

## Canonical entrypoints (live)

| Role | Entrypoint |
|------|------------|
| Live runner | `phase_l.l14_rescue.run_phase_l14_nonbidnet` |
| Hunt | `phase_l.hunt` profile `non_bidnet` |
| History | `phase_l.platform_history_adapters.run_platform_history` |
| Workflow | `CanonicalOpportunityWorkflow` |

## Obsolete rules (must stay inactive on live path)

Includes L.11–L.13 obsoletes plus:

- `LEGACY_BIDNET_AUTH_HISTORY_AS_CURRENT_PRIORITY`
- `LEGACY_COMMERCIAL_FEED_BIDNET_ONLY`

`obsolete_rules_active_on_live_path` must remain `false`.

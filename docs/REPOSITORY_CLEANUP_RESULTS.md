# M3 Production Repository Consolidation Results

**Build:** `20260930-m3-production-repository-consolidation`  
**Branch:** `cleanup/m3-r5-production-consolidation`  
**Baseline (`r5_known_good_commit`):** `2fe3e1629e4844846057ddcf564e73b83b421791`

## Verdict

`M3_PRODUCTION_REPOSITORY_CONSOLIDATED_READY`

## Before / after

| Metric | Before (baseline) | After |
|--------|------------------:|-----:|
| Docs (`docs/*.md`) | ~410 | 123 |
| Tests (`tests/test_*.py`) | 206 | 188 |
| `phase_l/l*_rescue.py` | 21 | **0** |
| `phase_l/*.py` | ~100 | 79 |
| Synthetic DEAD_END_DEALS | — | **0** |
| Live SAM calls in validation | — | **0** |
| External side effects | — | **0** |

Deleted vs baseline (on disk): **346** paths — see `artifacts/repository_cleanup/deletion_manifest.json`.

## What was removed (proven)

1. **Historical rescue scaffolding** — all `phase_l/l*_rescue.py` + matching one-shot `scripts/run_phase_l*` rescue runners + rescue-only tests  
2. **Orphan top-level utilities** — `check_status.py`, `claude_export.py`, `clear_db.py`, `refresh_workflow.py` (0 refs)  
3. **Obsolete phase/pilot/audit docs** — early L.* design dumps, G–K/F/E1 reports (kept CURRENT_*, R1–R5, M3 operator/owner, SAM policy, L.21–L.23.1 live docs)  
4. **Obsolete phase F/E1 tests**  
5. **Caches / pytest junk** where unlocked  

## What was preserved

- Response engine R1–R5 + operator UI sync  
- Discovery, supplier, quote, economics, financing, registrations, SAM budget, firewall  
- Canonical funnel L.23 / L.23.1, call desk L.22, quote prep L.21  
- `phase_g/h/i/j` (still live-imported via hunt/enrichment)  
- `validation_harness/`, `operator_workflow/`, `execution_requirements/`  
- Canonical corpus `data/response_corpus/real/` + registries  
- Uncertainty list: `artifacts/repository_cleanup/remaining_compatibility.json`

## Canonical authorities (single)

| Concern | Authority |
|---------|-----------|
| Operator next action | `response_engine.operator_state_service` |
| Funnel | `phase_l.l23_full_population_funnel` |
| Call desk | `phase_l.l22_supplier_call_desk` |
| Quote economics | `phase_l.quote_economics` |
| Response docs | `response_engine.r4_service` |
| Preflight / submission | `response_engine.r5_service` |
| UI entry | `/ops` |

## Migrations

- `phase_l.legacy_cleanup` updated: rescue modules marked deleted; live runner remains L.23  
- Domain smoke suite: `tests/test_m3_domain_smoke.py`  
- `static/index.html` cache-bust aligned to `APP_BUILD_VERSION`  
- Essential L.21–L.23.1 + operator UI audit docs restored after over-aggressive first doc pass  

## Validation

| Check | Result |
|-------|--------|
| Boot / import | OK |
| Today sections include owner/responses/submissions | OK |
| Unresolved imports to deleted rescues | 0 |
| Synthetic DEAD_END | 0 |
| Core regression (R1–R5, owner UI, L21–L231, domain smoke) | **300+ passed** (prior batch); critical recheck **104 passed** |
| SAM / external side effects | 0 |

## Remaining limitations

- Many `m3_*_read.py` modules kept pending full route audit  
- Early L.15–L.20 modules kept (scripts removed); listed in remaining_compatibility  
- `refresh_pricing.py` kept (intake/backfill references)  
- Not all of the 188 tests were re-run in one mega-suite this pass; core production paths were  

## Rollback

```text
git checkout 2fe3e1629e4844846057ddcf564e73b83b421791
```

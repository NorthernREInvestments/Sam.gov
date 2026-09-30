# Phase F — Clean Starting Baseline

**Date:** 2026-09-23  
**Gate:** Step 0 — must be 0 failed before Phase F substantive work.

## Flaky test diagnosis

| Item | Detail |
|------|--------|
| Test | `tests/test_m3_action_orchestration.py::test_dependency_blocking` |
| Prior suite result | 1724 passed / 1 failed (E.1 completion) |
| Isolation behavior | Passed repeatedly when run alone |
| Root cause | Shared `AppSetting`/SQLite persistence for action indexes. Under full-suite load, silent `_save_index` failures / lost read-modify-write updates left prerequisites not `COMPLETED` (or missing), so dependents stayed `BLOCKED`. |
| Narrow repair | (1) Autouse in-memory monkeypatch of `_load_index` / `_save_index` in the action-orchestration test module so tests do not share durable DB state. (2) Retry once on `_save_index` and re-merge upsert on failed save in `m3_action_orchestration_read.py`. No action-orchestration redesign. |
| Confirmation | `test_dependency_blocking` **10/10** passed (~0.08s each). Full `test_m3_action_orchestration.py` **10 passed**. |

## Full-suite baseline (required before Phase F)

| Metric | Value |
|--------|-------|
| Command | `python -m pytest -q --tb=line` |
| Passed | **1725** |
| Failed | **0** |
| Duration | 2646.29s (~44 min) |
| Status | **CLEAN — Phase F may proceed** |

## Rule

Do not begin Phase F reality-case / UI torture work until Failed = 0.  
**Satisfied 2026-09-23.**

# Build 002E Report — Full-Scale Validation & Acquisition Reliability

**Build ID:** `20261121-m3-2-build-002e`  
**Baseline:** Deployed Build 002D (not owner-accepted as fully verified)  
**Status:** In progress — metrics filled from live harness in `BUILD_002E_ACCEPTANCE.json`

## Why 002D stopped at 150 packages

Harness env caps (`M3_RECOVERY_MAX_LEVEL3=100` + `EXTRA=50`), not a product ceiling. See `BUILD_002E_AUDIT.md`.

## Corrections implemented

1. **Full population freeze** — every unique discovery record written to `acceptance_populations/{run_id}.jsonl` and enqueued as `package_job_items` in Postgres.
2. **Resumable acquisition job** — `PackageAcquisitionJob` + batch processor + background worker + startup resume.
3. **No acceptance sample cap** — job.total equals frozen unique count.
4. **Document cap** raised to 200 with overflow tracked as PARTIAL (not silent abandon).
5. **Completeness honesty** — listing/unknown/zero-doc cannot falsely COMPLETE.
6. **API** — `/api/m3/package-jobs/{id}`, `/resume`, `/batch`.
7. **Continue script** — `scripts/continue_package_job.py` for bounded slices.

## Acceptance standard (honest)

Full-population *accounting* is required. Full-population *completion* in one terminal session is not claimed if outstanding work remains — outstanding count, job id, and resume path are reported.

## Gates

See `docs/BUILD_002E_ACCEPTANCE.json` → `gates`.

## Owner acceptance

Await owner acceptance. Do not begin Build 003.

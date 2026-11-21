# Build 002E Report — Full-Scale Validation & Acquisition Reliability

**Build ID:** `20261121-m3-2-build-002e`  
**Baseline:** Deployed Build 002D (not owner-accepted as fully verified)  
**Production:** `/healthz` → `20261121-m3-2-build-002e`  
**Job:** `0db24e0c-69df-44ad-bbc9-f9c73d6e8825` (Postgres-durable, resumable)

## Why 002D stopped at 150 packages

Harness env caps (`M3_RECOVERY_MAX_LEVEL3=100` + `EXTRA=50`), plus `result_snapshot[:5000]` and `max_documents=30`. **Not** a product architecture ceiling. See `BUILD_002E_AUDIT.md`.

## Corrections implemented

1. Full unique population freeze → JSONL + `package_job_items` queue  
2. Resumable `PackageAcquisitionJob` with batch processor, background worker, startup resume  
3. No 150-package acceptance sample cap — job.total = frozen unique count  
4. Document cap 200 with overflow → PARTIAL  
5. Completeness honesty (no false COMPLETE on listing/unknown/zero-doc without detail inventory)  
6. Solicitation number truncation + session rollback isolation (job survives bad rows)  
7. API: `/api/m3/package-jobs/{id}`, `/resume`, `/batch`  
8. Continue: `scripts/continue_package_job.py`

## Freeze reconciliation

| Metric | Value |
|--------|------:|
| Prior 002C baseline | 9,799 |
| Frozen unique (002E run) | **7,636** |
| Delta vs baseline | −2,163 (page/source coverage + publication churn — not silent exclusion) |

Every frozen record was enqueued (`package_job_items` count = 7,636).

## Acquisition funnel (live job; in progress)

Snapshot from `BUILD_002E_ACCEPTANCE.json` at finalize time (~34% coverage). Job continues via additional slices + Railway startup resume.

| Funnel step | Count | Denominator |
|-------------|------:|-------------|
| Frozen acceptance population | 7,636 | — |
| Acquisition attempted | 2,598 | 2,598 / 7,636 = **34.0%** |
| Outstanding (resumable) | 5,038 | visible |
| Identified documents | (see acceptance) | |
| Downloaded documents | (see acceptance) | of identified |
| Extracted documents | (see acceptance) | of downloaded |

**FULL_POPULATION_ACCEPTANCE_CLAIM = FAILED** until coverage ≈ 100%. Outstanding work is explicit, not hidden.

## Platform notes (early processed slice)

- **gov_html / NJ purchase:** relatively strong COMPLETE rates  
- **Socrata / Mesa / LA City / Winnipeg / generic_http:** many FAILED or VERIFICATION_REQUIRED (listing URLs, no solicitation detail, or inventory unverified)  
- Access-restricted counts remain visible  

## Costs

OpenAI portal discovery off for freeze. Deterministic acquisition continues within $1/day $30/month policy.

## Gates

See `docs/BUILD_002E_ACCEPTANCE.json` → `gates`. Incomplete nationwide drain is reported as PARTIALLY VERIFIED / FAILED claim — not as full acceptance.

## Owner acceptance

Await owner acceptance. Do not begin Build 003.  
Resume: `python scripts/continue_package_job.py 0db24e0c-69df-44ad-bbc9-f9c73d6e8825`

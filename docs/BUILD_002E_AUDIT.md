# Build 002E — Audit of Build 002D Acceptance Limitations

## Why testing stopped at 150 packages

| Cause | Evidence | Type |
|-------|----------|------|
| **Harness env caps** | Shell set `M3_RECOVERY_MAX_LEVEL3=100` and `M3_002D_EXTRA_PACKAGES=50` | **Hardcoded acceptance limit** (agent-imposed, not product architecture) |
| **Script defaults** | `acceptance_build002d.py` `setdefault(M3_RECOVERY_MAX_LEVEL3, "400")` and `EXTRA_PACKAGES` default 150 | Artificial sample ceiling |
| **result_snapshot truncate** | Engine stores `filtered[:5000]` only | Implementation limitation for freeze/replay |
| **max_documents=30** | `acquire_solicitation_package(..., max_documents=30)` | Per-solicitation cap (safeguard without resume of remainder) |
| **Terminal-bound run** | Single ~15 min `python scripts/acceptance_build002d.py` | No persistent background continuation |

Not a Railway timeout and not an inherent inability to process ~9.8k records — processing was intentionally bounded.

## 002D harness outcomes (honest)

| Metric | Value | Notes |
|--------|------:|-------|
| Packages acquired | 150 | Cap = 100 L3 + 50 extra |
| Marked COMPLETE | 93 | **Many false COMPLETE** (0 docs / listing / unknown URL) before post-harness harden |
| Identified documents | 59 | |
| Downloaded | 54 | 91.5% of identified |
| Extracted | 51 | |
| FAILED | 54 | Often Winnipeg / generic_http |
| Discovery population that run | 8,204 | Below 9,799 baseline (pages/sources differ) |
| OpenAI portal | $0 | |

## Authoritative URL / platform reality

- **Working:** Mesa Socrata (48/48 downloads), some `.gov` direct PDFs
- **Weak:** ProcureTap / AI-discovered listing URLs, generic boards, Winnipeg dataset URLs without solicitation detail
- **Junk URLs:** `fonts.gstatic.com` etc. — rejected in post-002D hardening (must remain)

## Durability

- Content-hash blobs under `M3_DATA_ROOT/package_blobs` + DB rows — durable across process restarts **when that volume is the production volume**
- 002D acceptance wrote packages to **local** `govtracker/data`, not necessarily Railway `/data` — production durability must be re-verified on Railway

## Estimated full-population cost/time (order of magnitude)

Assumptions: ~8–10k unique records; ~2–8s average resolve/fetch when URL exists; many fail fast on bad URLs.

| Path | Estimate |
|------|----------|
| Wall time (serial ~4s avg) | ~9–11 hours |
| Wall time (batch workers, 4–8 concurrent, checkpoints) | ~2–4 hours |
| OpenAI | $0 if portal/L4 off; budget still $1/day $30/month |
| Storage | Dominated by PDFs; Mesa-scale ~0.5–1 MB/doc; nationwide depends on attachment density |

## 002E corrections required

1. Remove acceptance sample caps; process full frozen population via **resumable job + batch workers** (not one unbounded shell).
2. Persist **full unique population** to disk (not `result_snapshot[:5000]`).
3. Raise document-per-package limit; track overflow as PARTIAL with resume.
4. Independent inventory verification honesty (no false COMPLETE).
5. Source/platform recovery for worst performers.
6. Live Railway verification of jobs + document access.

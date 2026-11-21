# Build 002F Report — Acquisition Optimization & Progressive Qualification

**Build ID:** `20261121-m3-2-build-002f`  
**Production:** `/healthz` → `20261121-m3-2-build-002f`  
**Prior job:** `0db24e0c-69df-44ad-bbc9-f9c73d6e8825`

## Problems addressed

1. Nationwide full-package acquisition too slow (~20 items/min in 002E).  
2. Resources wasted on confirmed non-product / inactive solicitations.

## Architecture (preserved discovery invariant)

- Discovery remains unrestricted (no adapter-level product filters).  
- Progressive qualification gates **automatic full-package** only.  
- Manual / saved overrides still force full acquisition.

| Disposition | Action |
|-------------|--------|
| Confirmed services/construction/software | Skip full package |
| Forecast/closed/cancelled | Skip full package |
| unknown_mixed | Targeted (detail + ≤1 doc); escalate if product |
| Product classes | Full package |
| Saved / manual | Full package |

## Queue replan (measured)

| Metric | Count |
|--------|------:|
| Pending examined | 4,351 |
| Skipped confirmed non-product | 548 |
| Skipped inactive/forecast | 1,058 |
| Kept for targeted investigation | 2,745 |
| Eligible full (remaining pending) | 0 (products already processed in 002E) |

## Before/after performance (measured)

| Metric | 002E baseline | 002F observed |
|--------|---------------|---------------|
| Items/min | ~20 | **75.72** |
| Speedup | — | **3.79×** |
| Wall (this slice) | — | 2,174 s |
| Items this run | — | 2,744 |
| Acquire avg / p95 / max | — | 1.49s / 5.17s / 16.6s |
| HTTP text fetch avg | — | 0.94s |
| URL resolution avg | — | 0.07s |

Bottlenecks: network detail fetches (targeted path), not OCR (not exercised at scale).

## Funnel snapshot

| Disposition | Count |
|-------------|------:|
| Done | 3,090 |
| Error | 1,370 |
| Skipped full package | 1,606 |
| Pending (targeted remaining) | 1,570 |
| Coverage incl. skips | **79.4%** |
| Targeted investigations (slice) | 1,000 |
| Unnecessary downloads avoided (replan) | **1,606** |

Complete packages remain honest (no false COMPLETE inflation).

## Remaining work

Pending ~1,570 uncertain records continue via targeted investigation (resumable job). Not claimed as 100% drained in this acceptance slice.

## Gates

See `docs/BUILD_002F_ACCEPTANCE.json`.  

Await owner acceptance. Do not begin Build 003.

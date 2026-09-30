# Phase L.10 — Quality Results

**Verdict:** `PHASE_L10_EXACT_WORKFLOW_WORKING`

## Quote quality (all Stage 3, no cap)

| Metric | L.9 | L.10 |
|--------|-----|------|
| Validated | 1 | 1 |
| Secondary | 1 | 11 |
| Recon-only | 214 | 201 |
| Gov A | 14 | 14 |
| Gov C | 0 | 13 |
| Gov D (benchmark corpus) | 214 | 201 remaining / 13→C |
| Supplier A (Stage 3 graded) | 3* | 4 |
| Supplier B | 20* | 32 |

\*L.9 graded quote-dependent positives only; L.10 grades all Stage 3.

## Evidence improvement

- Model-specific award bands + recoverable commercial identity: **Gov D → Gov C** (13 rows).
- Secondary quote targets **1 → 11**; recon-only **214 → 201**.
- Generic `generic_vehicle` remains Gov D / RECON_ONLY (no inflation).
- Validated held at 1 (requires Gov A/B).

## Bottleneck

Exact buyer awards / bid tabs still scarce (auth-walled platforms). Validated gate needs Gov A/B.

## Tests

319 Phase L tests passed (`pytest tests -k phase_l`).

## STOP

No Phase M, outreach, quotes, bids, purchase, or financing.

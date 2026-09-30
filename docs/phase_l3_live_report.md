# Phase L.3 — Live Report

**Build:** `20260927-m3-phase-l3-commercial-acquisition-rebalance`  
**Verdict:** `PHASE_L3_PARTIAL_COMMERCIAL_REBALANCE`

## Fresh discovery

| Metric | Count |
|--------|------:|
| Raw / accessible | 483 |
| Tangible (est.) | 483 |
| Stage 1 | 282 |
| Stage 2 | 81 |
| Stage 3 | 81 |
| Stage 3 processed | 81/81 |
| Deep queue | 10 |

Fresh hunt ran first (L.3 primary pass); Stage 3 then reprocessed with tightened fleet/equipment lane classification.

## Acquisition lanes (Stage 3)

| Lane | Count |
|------|------:|
| Quote-required commercial | 9 |
| Unknown | 16 |
| Mil-spec specialty | 56 |
| **Commercial share** | **11.1%** |
| Specialty share | 69.1% |

All-accessible commercial lanes: 30 / 483 (6.2%).

## Economics

| Metric | Count |
|--------|------:|
| History found | 14 |
| Strong / price leads | 14 |
| Verified public price | 0 |
| Quote-required commercial | 9 |
| Supplier candidates | 31 |
| Max buy price calculated | 14 |
| Apparent / verified positive | 0 / 0 |

## L.2.9 comparison

| | L.2.9 | L.3 |
|--|------:|----:|
| Stage 3 processed | 81/81 | 81/81 |
| Lane tagging | none | yes |
| Quote-required | n/a | 9 |
| Commercial Stage 3 share | ~0 (untagged; mil-spec dominated) | 11.1% |
| Strong leads | 10 | 14 |
| Verified prices | 0 | 0 |
| Supplier candidates (quote path) | n/a | 31 |

## Remaining bottleneck

**Discovery mix is still defense/NSN-heavy.** Lane logic correctly routes fleet/equipment to quote-required and mil-spec to specialty, but commercial open/distributor rows remain scarce in the live feed. State/local commercial volume must grow further before Stage 3 commercial share crosses the Working threshold (~45%).

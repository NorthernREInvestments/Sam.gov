# Phase L.2.3 — Live Report

**Artifact:** `artifacts/phase_l/l23_rescue_latest.json`  
**Verdict:** `PHASE_L23_COMMERCIAL_IDENTITY_RECOVERY_WORKING`

## Candidate-pool expansion

| Metric | L.2.2 | L.2.3 |
|---|---:|---:|
| Product-resale live | 269 | 269 |
| Commercially researchable / market-research eligible | **11** (exact/strong commercial score) | **23** |
| Exact commercial model/trim/equipment | ~0 usable | **3** |
| Strong commercial identity | — | **6** |
| Exact MPN/SKU researchable | (mixed) | **14** |
| High priceability | — | **20** |
| Deep selected | 15 (military-skewed) | **23** (all eligible; commercial-first) |

Population does not support ≥40 distinct commercial researchables; **all 23 available were used**.

## Bobcat ToolCat UW56

| Field | Value |
|---|---|
| Phase J | `IDENTITY_PARTIAL` |
| L.2.3 state | `EXACT_EQUIPMENT_MODEL` |
| Manufacturer | Bobcat |
| Model | ToolCat UW56 |
| Priceability | HIGH |
| Market research eligible | **YES** |
| Deep order | #3 (bucket A) |

## Ford F-150 Police Responder

| Field | Value |
|---|---|
| Phase J | `OR_EQUAL_RESEARCHABLE` |
| L.2.3 state | `EXACT_VEHICLE_TRIM` |
| Manufacturer | Ford |
| Model | F-150 Police Responder |
| Priceability | HIGH |
| Market research eligible | **YES** |
| Deep order | #1 (bucket A) |
| Verified price (L.2.2 gate) | **$56,150** (`STRONG_VERIFIED`, carstory.com Police Responder page) |

Also recovered: Ford Expedition SSV (exact vehicle trim).

## Funnel (live)

| Metric | Count |
|---|---:|
| Live product resale | 269 |
| Commercial identities attempted | 269 |
| Exact commercial model | 3 |
| Exact MPN | 14 |
| Strong commercial | 6 |
| High / medium priceability | 20 / 3 |
| Market research eligible | 23 |
| Market attempted | 23 |
| Product pages fetched | 17 |
| Identity verified pages | 1 (strong) |
| Price verified | 1 |
| History found | 2 |
| Both history + price | 0 |
| Positive unit spread | 0 |
| Economics completed | 0 |
| ≥$10K | 0 |

## Buckets selected

- A exact commercial model: 3  
- B exact MPN/SKU: 14  
- C strong/recovery: 6  

## Remaining bottleneck

Commercial identity recovery works for brand/model opportunities present in the pool. Most of the 269 resale rows are still military NSN-only without public catalogs. Verified retail remains scarce after L.2.2 gates; history+retail rarely co-occur. Next leverage is more commercial opportunities in discovery and better dealer-page fetch/extraction for Bobcat/Ford OEM networks — **not** loosening price verification.

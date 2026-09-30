# Phase L.6 — Government Value Hierarchy

## States

| State | Meaning |
|-------|---------|
| `GOV_VALUE_EXACT` | Exact prior award / NTE / contract price for same item |
| `GOV_VALUE_STRONG` | Budget, ceiling, buyer×product memory, strong family match |
| `GOV_VALUE_RANGE` | Explicit low–high (incl. category award bands) |
| `GOV_VALUE_COMPARABLE` | Comparable / generic commercial family recon |
| `GOV_VALUE_UNKNOWN` | No usable government-side value |

## Tiers

- **Tier A** — exact award, NTE, current contract/schedule/board price
- **Tier B** — same family/buyer recent purchase, budget/ceiling
- **Tier C** — category benchmarks / comparable models (**not** final expected award)

Tier C sources are labeled `GOVERNMENT_CATEGORY_BENCHMARK_RECON:*` with `final_award_value=false`.

## Captured fields

source · unit/total value · low/mid/high · quantity · confidence · identity match · freshness (via memory timestamps)

## Buyer value memory

Persisted at `data/phase_l6_buyer_value_memory.json` keyed by **buyer × product**, never agency-wide median (prevents DoD contamination).

## Expected revenue

`ExpectedRevenueLow` / `Mid` / `High` with evidence `quality` = gov-value state.

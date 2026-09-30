# Phase L.2.4 — Price/History Convergence Design

**Build:** `20260927-m3-phase-l24-price-history-convergence`

## Objective

Force **Branch A (historical government price)** and **Branch B (current usable acquisition price)** to run independently on the same L.2.3-eligible candidates, then JOIN into an explicit convergence state.

## Modules

| Module | Role |
|---|---|
| `phase_l/convergence.py` | Expanded history keywords, price-access gate, freight screen, join, queues |
| `phase_l/enrichment.py` | History lookup uses ≥3 keyword approaches (NSN/MPN/model) |
| `phase_l/market_price.py` | Coop/OEM domain hints retained; L.2.2 verification unchanged |
| `phase_l/l24_rescue.py` | Exhaust all eligible candidates; emit per-row table + queues |

## Convergence states

`BOTH_FOUND_POSITIVE_SPREAD` · `BOTH_FOUND_NEGATIVE_SPREAD` · `HISTORY_ONLY` · `CURRENT_PRICE_ONLY` · `NEITHER_FOUND` · `ACCESS_BLOCKED` · `IDENTITY_CONFLICT` · `CONFIGURATION_CONFLICT`

## Price access

Cooperative / government-only / dealer-only prices stored as intelligence with `ECONOMICS_ELIGIBLE=False` unless publicly buyable.

## Freight

Heavy equipment / AK / HI / OCONUS → `FREIGHT_REQUIRED_BEFORE_FINAL_PASS` (raw spread OK; final net blocked).

## Queues

`PROFITABLE_ANY_AMOUNT` · `PROFITABLE_GE_10K` · `PROMISING_UNIT_ECONOMICS` · `NEGATIVE_ECONOMICS` · `RESEARCH_INCOMPLETE`

# Phase L.2.9 — Recurring Buy Report

## Engine

- `RecurringProductWatch` via `aggregate_recurring_purchases`
- `BuyerWatch` / cadence via `aggregate_buyer_watchlist`
- `KnownProductEconomics` persisted to `data/phase_l29_known_product_economics.json`
- **Reverse hunt:** known product → match live solicitations by MPN/model/NSN/buyer tokens

## Live results

| Metric | Count |
|--------|------:|
| Products tracked | 15 |
| Buyers tracked | 1 |
| Known product economics | 0 |
| Reverse-hunt live matches | **19** |

Artifacts: `artifacts/phase_l/l29_summary.json` → `recurring_buy`.

# R2 — Legacy Economics Cutover

## Canonical path (operator Bid Prep)

`response_engine.r2_service.run_r2_analysis`

## Reused

| Asset | Role |
|-------|------|
| `micro_purchase_lab_economics` Decimal utils | Money kernel |
| `phase_l.quote_economics` L6 max-buy algebra | Ported to Decimal in `pricing.calculate_max_buy_decimal` |
| L.22 quotes | Consumed via `supplier_evidence` |
| R1 requirements / product_mode | Technical spine |

## Deprecated as Bid Prep authority

- `phase_l.acquisition_lanes.calculate_maximum_buy_price` (10% path) — do not use as authority
- `bid_compliance_engine` for readiness
- `m3_deal_economics` as CLIN bid authority

## Retained non-authority

- µLab UI / historical-equivalent = pricing intelligence
- `transaction_economics` / `draft_bid_assembly` = later assembly inputs

**Competing operator readiness paths: 0** (R1 readiness + R2 economics under response_engine).

# Phase L — Economics Model

## Screening baseline

**Current public retail price** is the conservative, publicly verifiable acquisition baseline.

Tracked:

- `public_retail_unit_price`
- `public_retail_total`
- `public_retail_source`
- `public_retail_checked_at`

Volume/reseller discounts are **upside scenarios only**. They must not turn a failing retail baseline into a PASS.

## Default financing

Decision case: **5% of financed acquisition amount** (retail total).

Alternate scenarios computed: 0% / 3% / 5%.

Financing must not rescue negative unit economics (`gross_retail_spread ≤ 0`).

## Formulas

```
expected_revenue     = expected_bid_unit × qty   (default bid unit = historical unit)
public_retail_total  = retail_unit × qty
gross_retail_spread  = expected_revenue − public_retail_total
estimated_direct     = freight + packaging + compliance + contingency
estimated_financing  = retail_total × financing_rate
expected_net_profit  = expected_revenue − retail_total − direct − financing
```

## Profit tiers

| Expected net | Tier |
|-------------:|------|
| < $10,000 | FAIL (not priority) |
| $10k–$24,999 | PASS |
| $25k–$49,999 | STRONG |
| $50k–$74,999 | EXCELLENT |
| $75k–$99,999 | EXCEPTIONAL |
| ≥ $100,000 | MONSTER |

Floor: `min_actual_profit_usd()` (≥ $10,000).

## Gate order

Access `YES` → then history/retail/economics. Inaccessible deals skip expensive pricing.

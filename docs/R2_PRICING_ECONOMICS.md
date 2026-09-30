# R2 — Pricing & Economics

## Money

`response_engine/money.py` re-exports µLab Decimal (`D`, `money`, `pct`, `ratio`). **No float money.**

## Max-buy (INTERNAL ONLY)

Decimal port of L6:

`(revenue − freight − fees − risk − profit) / (1 + financing_rate)`

Default financing rate: **5%**.

## Profit

`expected_profit = revenue − total_execution_cost`  
`margin = profit / revenue`  
`markup = profit / cost`

States: `VERIFIED_PROFIT` | `PROVISIONAL_PROFIT` | `SCENARIO_PROFIT` | `UNKNOWN_PROFIT`

## Scenarios

`BidPriceScenario` — not final bid. Target profit / target margin / owner-entered.

## Modules

`pricing.py` · `acquisition_cost.py` · `economics` via `r2_service.py`

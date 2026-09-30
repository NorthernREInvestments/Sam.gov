# Phase L.6 — Quote Priority

## `QuoteEconomicsScore` (0–100)

Factors: gov-value quality · max-buy headroom · supplier count · commercial commonality · freight difficulty · deadline runway · recurring-buy signal

## Ranking (all quote-required; no cap)

1. Strongest government-side value  
2. Largest max-buy headroom  
3. Multiple supplier candidates  
4. Commercially common product  
5. Longer deadline  
6. Repeat buyer/product  
7. Easier freight  
8. Lower configuration ambiguity  

## Deadline runway

| Days | Feasibility |
|-----:|-------------|
| 14+ | High |
| 7–13 | Viable |
| 5–6 | Urgent |
| 3–4 | Constrained |
| &lt;3 | Generally not current execution |

Short/expired rows retained for recurring intelligence.

## Queues

- `SUPPLIER_QUOTE_PRIORITY` — all ranked quote-economics rows  
- `ECONOMICS_MANUAL_REVIEW` — config/UOM/freight/headroom edge cases (uncapped)  
- Deep research — every viable reconnaissance row (per-row stop-loss only)

## Quote packet (no send)

solicitation · buyer · product · MFR/model · equivalents · qty · condition · destination · delivery date · FOB · warranty · specs · internal quote deadline · **target acquisition price**

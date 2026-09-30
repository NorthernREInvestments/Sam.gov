# Phase L.6 — Max-Buy Engine

## Formula (conceptual)

`expected revenue − desired profit − financing − freight − fees − variable costs − risk reserve`

Financing applied once via `(revenue − profit − freight − fees − risk) / (1 + rate)`.

## Thresholds

- `BREAK_EVEN_MAX_BUY`
- `MAX_BUY_FOR_5K_PROFIT` / `_10K_` / `_25K_`
- `MAX_BUY_FOR_15_PERCENT_MARGIN` / `_20_` / `_25_`

Impossible thresholds return `null` with reason.

## Defaults

| Cost | Default |
|------|---------|
| Financing | 5% of financed acquisition (configurable) |
| Freight | Case-by-case reserve (`FREIGHT_ESTIMATE` / `FREIGHT_LOW_CONFIDENCE_RESERVE` / `FREIGHT_QUOTE_REQUIRED`) |
| Risk reserve | Optional (e.g. 2% when freight uncertain) |

## Quote artifacts

- `SUPPLIER_QUOTE_TARGET` — primary owner ceiling
- Bands: `EXCELLENT` / `ACCEPTABLE` / `MARGINAL` / `FAIL` (derived from economics, not hardcoded $)
- Simulator: quotes at 60/75/90/100/110% of max-buy → spread, financing, net, margin, go/no-go
- Range economics: conservative (low) used for ranking confidence; mid/upside also reported

## UOM / config

`UOM_UNRESOLVED` blocks false unit↔total economics. Config mismatch (bundled history vs bare product) downgrades evidence quality.

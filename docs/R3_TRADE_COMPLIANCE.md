# R3 — Trade Compliance

**Module:** `response_engine.trade_compliance`

## Regimes

`BUY_AMERICAN` | `TRADE_AGREEMENTS` | `FTA` | `NONE` | `UNKNOWN`

Detected from solicitation/clause text (e.g. 52.225-1, 52.225-5). Solicitation controls over generic library.

## Country of origin

- Line-level analysis (multi-CLIN may differ)
- Brand headquarters / U.S. distributor / warehouse ≠ COO
- Classifications: U.S.-made | designated-country | non-designated-country | unknown
- Unknown origin **never** auto-passes
- Evidence quality required for `PASS_VERIFIED` (OEM declaration, certificate of origin, etc.)

## All-or-none

If procurement is all-or-none and any required line FAILs trade → whole response blocked.

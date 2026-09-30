# Phase L.7 — Regression

## Caps

`STAGE3_NO_ROW_CAP` · `DEEP_RESEARCH_NO_FIXED_COUNT` · `MANUAL_QUEUE_NO_FIXED_CAP` · **no fixed-32 positive ceiling**

## Legacy

Obsolete Stage 1/2 exact gates and `NO_PRICE_INFORMATION`-as-dead-end inactive on live path. Cap constants unified via `acquisition_lanes` re-export from `source_roles`.

## Quote isolation

Supplier packets assert absence of max-buy / profit / government value fields.

## Final bid gate

`EXACT_VERIFIED` unchanged. READY_FOR_QUOTE_OUTREACH ≠ READY_TO_BID.

## Outreach

`send_authorized=false` · `outreach_authorized=false` · operating mode `DEVELOPMENT_NO_OUTREACH`

## Tests

`tests/test_phase_l7_quote_readiness.py` covers legacy cleanup, readiness gates, blockers, packets, evaluator, expiration, multi-quote, caps, owner approval, original source.

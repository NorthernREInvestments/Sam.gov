# Phase L.7 — Supplier Packet Design

## Supplier-facing packet

Asks for best commercial pricing, lead time, freight estimate.

Includes: solicitation # · buyer · product · MFR/model/MPN · qty/UOM · equivalents · specs · condition · warranty · destination · delivery date · FOB · packaging · TAA/BAA · internal quote deadline note.

`send_authorized=false` · `outreach_authorized=false`

## Never exposed to suppliers

government historical price · max-buy · desired profit · margin ceilings · expected revenue · risk reserve · financing assumption

Enforced by `INTERNAL_FIELDS_NEVER_SUPPLIER` + packet assertions.

## Internal quote control

Separate record: gov value · revenue range · break-even / $5K / $10K / $25K / margin max-buys · supplier target · freight reserve · financing · risk.

## Quote response evaluator (for later ingestion)

Landed cost · gross · net · margin · expiration · lead time → `QUOTE_EXCELLENT` / `ACCEPTABLE` / `MARGINAL` / `FAIL`

Expired quotes cannot be final acquisition evidence.

## Multi-quote comparison

Ranks by landed cost + compliance + authorization + lead time + terms — cheapest is not automatic winner.

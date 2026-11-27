# Historical Pricing Validation Rules

## Pricing types (not interchangeable)

`awarded_unit_price`, `awarded_extended_price`, `unsuccessful_competitor_bid`, `contract_authorized_catalog_price`, `purchase_order_price`, `government_expenditure`, `total_contract_award_amount`, `estimated_contract_value`, `contract_ceiling`, `government_estimate`, `intent_to_award`, `unknown_pricing_type`.

## Hard rules

1. Do not divide contract totals across products to invent unit prices.
2. Do not assume lowest bidder won.
3. Do not treat intent-to-award as executed award.
4. Do not treat estimated qty as guaranteed purchase.
5. Suppress `unit_price` when `pricing_type` is total/ceiling/estimate (`normalize_award_record`).
6. `MARKET_CONTEXT_ONLY` / `UNUSABLE` historical rows do not feed revenue benchmarks in `build_commercial_evidence`.
7. Missing historical data → display **UNKNOWN**, never `$0`.
8. Historical research is optional — not a bid-ready gate by itself.
9. No permanent historical award warehouse; retain only opportunity-scoped research run snapshots / audit.
10. OpenAI must not invent evidence; cite `source_url`.

## Relevance

| Relevance | Use in economics |
| --- | --- |
| `VERIFIED_COMPARABLE` | Eligible historical unit benchmark |
| `QUALIFIED_COMPARABLE` | Eligible with caveats |
| `MARKET_CONTEXT_ONLY` | Display only; no revenue math |
| `UNUSABLE` | Ignore for pricing |

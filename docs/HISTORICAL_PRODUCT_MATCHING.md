# Historical Product Matching

Code: `m3/research/historical_match.py`

## Match classes

| Class | Meaning |
| --- | --- |
| `EXACT_NSN_MATCH` | Same National Stock Number |
| `EXACT_MANUFACTURER_PART_NUMBER_MATCH` | Same manufacturer part number |
| `EXACT_VERIFIED_PRODUCT_IDENTIFIER_MATCH` | Other verified identical identifier |
| `VERIFIED_BRAND_OR_EQUAL_MATCH` | Solicitation allows brand-or-equal and historical item satisfies |
| `VERIFIED_SPECIFICATION_MATCH` | Specs match with documented comparison |
| `GENERAL_MARKET_CONTEXT_ONLY` | Similar category/name only |
| `NO_RELIABLE_MATCH` | Unrelated or conflicting identifiers |

## Match reason (required)

Plain-language **why** (1–3 sentences). Shown on the opportunity page as “Why it matches.”  
Used later in bidding so humans/software can trust or discard the price.

Examples:

- Exact: “Same MPN ABC-123 on the 2024 award tab; configuration matches the solicitation.”
- Reject: “Historical MPN ABC-123A adds a revision suffix; not an exact match.”

## Deterministic overrides

When current and historical both have NSN or MPN, `classify_identifier_match` overrides model guesses for exact vs none.

## Packaging

Never assume case/pack counts. `packaging_conversion_supported` requires an explicit pack quantity before case→each conversion.

# R4 — Spreadsheet Population

**Engine:** `response_engine.spreadsheet_fill`

## Rules

- Use R2 `BuyerPricingFieldMap` cells only — never guess mappings
- Preserve formulas, sheet order, hidden sheets, merges
- Do not overwrite buyer formula cells
- XLSM: preserve bytes, never execute macros → `MACRO_TEMPLATE_MANUAL_COMPLETION_REQUIRED`
- Original SHA-256 must remain unchanged
- Unknown prices stay blank (not 0 / N/A)

## Totals

Validate `unit × qty = extended` with buyer rounding; flag mismatches.

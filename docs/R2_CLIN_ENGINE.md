# R2 — CLIN Engine

Build: `20260929-m3-r2-clin-pricing-product-compliance`

## Model

`ResponseLineItem` on each `ResponseProject` (`line_items`).

Extracted from:

- parser tables (`tables.line_items`)
- pricing workbook sheets
- CLIN/qty regex in solicitation text
- quantity requirement fallback

## Provenance

Each line stores document / sheet / row / cell when available (`BuyerPricingFieldMap`).

## Award basis

`LINE_ITEM` | `LOT` | `GROUP` | `AGGREGATE` | `ALL_OR_NONE` | `MULTIPLE_AWARD` | `UNKNOWN`

## Module

`response_engine/clins.py` · orchestrated by `response_engine/r2_service.py`

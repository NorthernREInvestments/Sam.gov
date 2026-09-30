# R2 — Product Technical Compliance

## OfferedProduct

`response_engine/product_offer.py` — multiple options per line; explicit selection.

## Exact part

Mismatch → `TECHNICAL_FAIL`. Similarity never creates EXACT. Superseding only with evidence id.

## Brand-or-equal

Each salient characteristic is a `TechnicalComplianceItem`.  
Generic “meets requirements” → FAIL.

## Evidence

`PASS_VERIFIED` requires evidence (`ProductEvidence`). Otherwise `REVIEW_REQUIRED` / `UNKNOWN`.

## Module

`response_engine/technical_compliance.py`

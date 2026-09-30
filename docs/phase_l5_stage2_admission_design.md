# Phase L.5 — Stage 2 Admission Design

**Build:** `20260927-m3-phase-l5-commercial-retention-repair`

## Principle

**Permissive uncertainty early, strict certainty late.**

Missing MPN / quantity / history / public price ≠ failure at Stage 1/2.

## Stage 2 admits when any of:

| Anchor | Meaning |
|--------|---------|
| Exact | MPN, NSN, manufacturer+model, SKU |
| `brand_or_equal` | Named brand/model or equal language |
| `brand_clue` | Recognizable commercial manufacturer |
| `descriptive_spec` / `SPEC_DRIVEN_COMMERCIAL_PRODUCT` | Commercial category + descriptive hardware language |
| `partial_commercial` | PARTIAL commercial identity state |
| `document_signal` | Spec/pricing/bid-form attachment metadata |
| `category_tangible` | Tangible commercial category + sufficient title |

## Still hard-reject

Services, construction/food, access=NO, sole-source/unavailable eligibility, expired deadlines for live bid.

## Not required for Stage 2

Exact MPN, quantity, history, public retail price, freight, financing.

# Phase L.7 — Quote Readiness Design

**Status:** `READY_FOR_QUOTE_OUTREACH` ≠ `READY_TO_BID`

## Gate requirements

- Live/open solicitation (authoritative source verified)
- Product defined enough to request pricing
- Quantity known **or** clear unit-basis quote allowed
- Configuration/spec adequate
- Destination / freight classifiable (freight not invented)
- Usable government-side economics + max-buy target
- ≥1 credible supplier
- No hard eligibility / source-approval blocker
- Deadline runway sufficient for quote turnaround (≥5 days)

## Explicit blockers

`QUOTE_BLOCKED_PRODUCT_IDENTITY` · `CONFIGURATION` · `QUANTITY` · `UOM` · `DESTINATION` · `DEADLINE` · `ELIGIBILITY` · `SOURCE_APPROVAL` · `SOLICITATION_DOCUMENTS` · `NO_SUPPLIER` · `GOV_VALUE` · `OTHER`

## Owner flow (L.7 stops before send)

`READY_FOR_QUOTE_OUTREACH` → `OWNER_APPROVAL_REQUIRED` → (future) `APPROVED_FOR_QUOTE_OUTREACH` → send

## Separation

- **Supplier packet** — commercial pricing request only (no max-buy / profit / gov history)
- **Internal quote control** — all economics ceilings, reserves, financing assumptions

## Also prepared

Quote response evaluator · multi-quote comparison · quote expiration · supplier authorization states · `QuoteOutreachPriorityScore` · recurring boost · product/supplier memory stubs

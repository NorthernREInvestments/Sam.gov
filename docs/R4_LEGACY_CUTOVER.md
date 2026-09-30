# R4 — Legacy Cutover

## Canonical path

`response_engine.r4_service.run_r4_generation`

## Reused

R1 deliverables, R2 `BuyerPricingFieldMap` / scenarios / lines, R3 maps & attestations, `firewall.py`, openpyxl, python-docx.

## Migrated

`draft_bid_assembly.populate_field` verified-only rules; subset of `proposal_export` narrative patterns (deterministic).

## Deprecated as Bid Prep authority

`proposal_service` narrative UI; M3 `draft_bid_package` as readiness.

## Compatibility-only

`proposal_service` / `proposal_export` (`legacy=1`), `draft_bid_assembly`, `bid_package`, `bid_pricing_engine`, `submission_package` checklist.

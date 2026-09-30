# R3 — Nonmanufacturer Rule Engine

**Module:** `response_engine.nmr.evaluate_nmr`  
**Decision object:** `NMRDecision` (never boolean-only)

## States

- `NMR_NOT_APPLICABLE` — unrestricted / pure service
- `NMR_APPLIES_COMPLIANT` — conditions verified with evidence
- `NMR_APPLIES_WAIVER` — class/individual waiver with SBA source
- `NMR_APPLIES_NONCOMPLIANT` — nonmanufacturer, no waiver, missing facts
- `NMR_REVIEW_REQUIRED` — set-aside supply; facts incomplete
- `NMR_UNKNOWN` — set-aside/manufacturer status unknown

## Rules

- Solicitation set-aside + NAICS control
- Waiver never inferred from prior contracts
- Multi-item groups tracked; not one result blindly applied to every CLIN without groups
- IT VAR only with explicit evidence — product being IT is not enough
- Governing sources preserved: 13 CFR 121.406, FAR 19.505 / 19.102

## Legacy

`nmr_from_deep_deal_compat` bridges `deep_deal_compliance.evaluate_nonmanufacturer_rule` into R3 states. Operator-facing Bid Prep uses R3 only.

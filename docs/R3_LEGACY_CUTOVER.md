# R3 — Legacy Cutover

## Canonical path

**`response_engine.r3_service.run_r3_analysis`** is the sole operator-facing company/compliance result for Bid Prep.

## Reused

- `data/company_eligibility_profile.json` — canonical UNKNOWN-default spine
- `company_eligibility.set_aside_eligibility` / `held_certifications`
- `eligibility_gate.load_company_eligibility_profile`
- `phase_l.registration_tracker` — REGISTER_BEFORE_BID UI
- `deep_deal_compliance.evaluate_nonmanufacturer_rule` — compatibility bridge only

## Migrated into R3

NMR states, trade/COO, Section 889, owner attestations, cyber/DPAS detection, profile versioning, compliance matrix, response maps (R4 destinations only — no form fill).

## Compatibility-only (not Bid Prep eligibility)

- `deep_deal_compliance.evaluate_bid_compliance`
- `company_profile.py` underwriting DB

## Deprecated as competing operator results

Any parallel Bid Prep eligibility panel outside R3 must delegate or remain non-operator.

## SAM

R3 tests and corpus validation: **0 live SAM API calls**. Cached/owner snapshots only.

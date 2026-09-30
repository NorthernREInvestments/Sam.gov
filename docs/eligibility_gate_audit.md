# Eligibility Gate Audit

**Date:** 2026-09-25  
**Scope:** Phase H cohort (25 LIVE_SOURCE), Phase G live fixtures, sample near-READY / READY historical packets.  
**Method:** Deterministic `evaluate_eligibility_gate` on solicitation title + noticedesc/description text already captured in Phase H research packets; company profile = empty `vehicles_held` (no invented holdings).

## Opportunities reviewed

| Bucket | Count | Notes |
|--------|------:|-------|
| Phase H deep-research cohort | 25 | Re-scored with eligibility gate on frozen research packets + live BOAST re-pull |
| Phase G live harness fixtures | 10 | Including new `H_BOAST_eligibility_regression.json` |
| Validation corpus sample (IDIQ/approved-source fixtures) | 3 | CASE_008, CASE_020, R_07 — pattern coverage, not live READY |

## Missed eligibility requirements (pre-fix)

| Opportunity | Discoverable prereq | Pre-fix actionability | Post-fix |
|-------------|---------------------|------------------------|------------|
| BOAST RFOP — Power Distribution Panel NSN `6110-01-082-8958` | Active Army BOAST BOA holder by close | `READY_FOR_QUOTE_OUTREACH` (P0 false actionability) | `ELIGIBILITY_ACTION_REQUIRED` / `NOT_CURRENTLY_ELIGIBLE` |

No other Phase H cohort row had a detected vehicle/BOA/IDIQ/JCP/approved-source holder restriction in available notice text. Boilerplate “clearance” language appears in SAM notices but did not match JCP/export access detectors (no false JCP blocks).

## False actionability cases

| Case | Defect | Severity | Fixed? |
|------|--------|----------|--------|
| BOAST RFOP NSN 6110-01-082-8958 | Economics + identity produced quote-ready before vehicle eligibility | **P0** | Yes |

## Near misses

| Case | Observation |
|------|-------------|
| F110 BLADE / AMC 3V titles | Possible approved-source / AMC pathway — not confirmed as holder-only in notice text available; left `ELIGIBILITY_NOT_APPLICABLE` (blind spot: AMC codes need attachment-depth later) |
| MX908 brand product | Restricted commercial brand risk handled by identity/source paths, not vehicle gate |
| Sources Sought T-38 | Not awardable RFQ — already not quote-ready |
| Construction SACC IDIQ (Phase G fixture G_06) | Service/construction reject path — not product quote-ready |

## Fixes applied

1. `eligibility_gate.py` — vehicle/access detectors + `EligibilityGateResult` + company profile compare  
2. `data/company_eligibility_profile.json` — empty holdings (UNKNOWN/not held)  
3. Phase H readiness — eligibility blocks before quote/bid states  
4. Operator packet + mobile read-model — relevant eligibility chips only  
5. Permanent regression fixture `H_BOAST_eligibility_regression.json`  
6. Targeted tests in `tests/test_eligibility_gate.py` (15)

## Phase H cohort eligibility summary (after)

| Eligibility status | Count |
|--------------------|------:|
| `ELIGIBILITY_NOT_APPLICABLE` | 24 |
| `NOT_CURRENTLY_ELIGIBLE` | 1 (BOAST) |
| `ELIGIBLE_CONFIRMED` / `CONDITIONAL` / `UNKNOWN` | 0 |

| Readiness (after) | Count |
|-------------------|------:|
| `RESEARCHED_NOT_READY` | 24 |
| `ELIGIBILITY_ACTION_REQUIRED` | 1 |
| `READY_FOR_QUOTE_OUTREACH` | **0** |
| `READY_FOR_BID_DECISION` | **0** |

## Blind spots (documented, not overbuilt)

- Vehicle language only in PDF attachments not ingested into Phase H text blob  
- AMC / QPL / source-approval coded without explicit “holders only” prose  
- State/local cooperative & portal registration not yet pattern-complete  
- Processing-time for on-ramps intentionally UNKNOWN (no invented SLAs)

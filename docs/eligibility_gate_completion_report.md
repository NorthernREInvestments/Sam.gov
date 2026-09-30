# Eligibility Gate — Completion Report

**Date:** 2026-09-25  
**Status:** COMPLETE — hard STOP (no enrollment, outreach, JCP, bids, financing, or Phase I)

## Original BOAST defect

M3 recommended supplier quote outreach on a live Army **BOAST RFOP** (NSN `6110-01-082-8958`) while the company had **no confirmed BOAST BOA**. Economics overrode an undiscovered contractual eligibility prerequisite → **P0 false actionability**.

## Root cause

Phase H readiness ordered identity + history + economics ahead of vehicle/holder eligibility. No dedicated eligibility/vehicle/access gate compared solicitation restrictions to an operator company profile. Empty/unknown holdings were treated as non-blocking.

## Exact fix

1. **`eligibility_gate.py`** — detect vehicle/access restrictions from solicitation text; evaluate against `CompanyEligibilityProfile`; emit `EligibilityGateResult` with provenance; `blocks_quote_outreach` fail-closed unless `ELIGIBLE_CONFIRMED` or `ELIGIBILITY_NOT_APPLICABLE`.  
2. **`data/company_eligibility_profile.json`** — single source of truth; default empty vehicles; UNKNOWN remains UNKNOWN.  
3. **`phase_h/deep_research.py`** — evaluate gate before quote/bid readiness; `ELIGIBLE_CONDITIONAL` / missing / unknown → `ELIGIBILITY_ACTION_REQUIRED` with eligibility-first next action.  
4. **UI** — operator packet + mobile detail show only relevant eligibility chips (Confirmed / Conditional / Blocked / Unknown).  
5. **Regression fixture + tests** — BOAST P0 permanent; 15 targeted tests.

## Cases audited

- 25 Phase H cohort opportunities (re-score + live BOAST)  
- Phase G live fixtures + BOAST regression fixture  
- Sample corpus IDIQ / approved-source fixtures  

See `docs/eligibility_gate_audit.md`.

## Additional missed prerequisites found

None confirmed as additional P0 false-actionability in the Phase H cohort text available. Documented blind spots: attachment-only vehicle language, AMC/QPL coded paths without explicit holder prose, state/local portal registrations.

## Before / after readiness counts (Phase H 25)

| State | Before | After |
|-------|-------:|------:|
| `READY_FOR_QUOTE_OUTREACH` | 1 | **0** |
| `READY_FOR_BID_DECISION` | 0 | 0 |
| `ELIGIBILITY_ACTION_REQUIRED` | 0 | **1** |
| `RESEARCHED_NOT_READY` | 24 | 24 |

Quote-ready falling from 1 → 0 is **correct** (reality over scorecard).

## Targeted tests

```
python -m pytest tests/test_eligibility_gate.py -q
# 15 passed
```

Plus live BOAST regression script: `scripts/run_boast_eligibility_regression.py` → `ELIGIBILITY_ACTION_REQUIRED`.

## Full suite

```
python -m pytest -q --tb=line
# 1775 passed in 2277.88s (0:37:57)
# EXIT=0
```

Log: `artifacts/eligibility_gate_full_suite.txt`

## Remaining blind spots

- PDF/attachment-only restrictions not yet in text blob  
- No invented on-ramp processing times (conditional stays non-quote-ready)  
- No giant federal vehicle database (by design)  
- Operator profile UI editing deferred (profile file is editable)

## Hard STOP

No BOAST enrollment, JCP paperwork, drawing requests, supplier/CO contact, bids, financing, or Phase I started.

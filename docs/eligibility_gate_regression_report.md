# Eligibility Gate — Regression Report

**Date:** 2026-09-25  
**Build:** `20260925-m3-eligibility-gate-1`

## Original defect

Phase H marked **BOAST RFOP — Power Distribution Panel** (NSN `6110-01-082-8958`) as `READY_FOR_QUOTE_OUTREACH` because product identity, government history, and max supplier cost looked good — while the solicitation is limited to **active BOAST Basic Ordering Agreement holders**, and the company profile does not confirm BOAST status.

## Expected after fix

| Check | Expected | Observed |
|-------|----------|----------|
| `READY_FOR_QUOTE_OUTREACH` | False | False |
| Eligibility overall | `NOT_CURRENTLY_ELIGIBLE` or `ELIGIBLE_CONDITIONAL` | `NOT_CURRENTLY_ELIGIBLE` |
| Primary next action | Eligibility-first (not supplier quote) | Confirm/obtain BOAST BOA before supplier pricing |
| Operator plain label | Blocked — BOAST BOA required | Present |

## Evidence sources used

- Live SAM noticedesc pull (authorize_live)  
- Title: `BOAST RFOP - Panel, Power Distribution - NSN: 6110-01-082-8958`  
- Company profile: `vehicles_held: []` (`data/company_eligibility_profile.json`)  
- No inference from NAICS / industry / wishful prior awards

## Runs

### Offline cohort re-score (25 frozen Phase H packets)

| Metric | Before | After |
|--------|-------:|------:|
| Quote-ready | 1 | 0 |
| Bid-ready | 0 | 0 |
| Eligibility action | 0 | 1 |
| BOAST readiness | `READY_FOR_QUOTE_OUTREACH` | `ELIGIBILITY_ACTION_REQUIRED` |

### Live BOAST-only deep path

Artifact: `artifacts/phase_h/boast_live_regression.json`

```
readiness = ELIGIBILITY_ACTION_REQUIRED
elig     = NOT_CURRENTLY_ELIGIBLE / Army BOAST BOA / MISSING
next     = Confirm or obtain Army BOAST BOA eligibility before requesting supplier pricing...
```

### Confirmed-held counterfactual (unit test)

When profile `vehicles_held` includes `BOAST_BOA`, gate is actionable and readiness may return quote-ready if economics still pass — proves gate does not permanently blacklist the NSN.

## Fixture

`validation_harness/cases/phase_g_live/H_BOAST_eligibility_regression.json` — permanent P0 regression case.

## Targeted tests

`tests/test_eligibility_gate.py` — 15 passed (BOAST missing/held, IDIQ, open sol, JCP, approved source, set-aside, unknown, attachment vs title, multi-blocker outrank, profile update).

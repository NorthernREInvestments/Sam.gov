# Eligibility / Vehicle / Access Gate — Design

**Date:** 2026-09-25  
**Trigger:** Phase H P0 false-actionability — BOAST RFOP marked `READY_FOR_QUOTE_OUTREACH` while company BOAST BOA status was unconfirmed.

## Core rule

Eligibility outranks economics. No opportunity may reach:

- `READY_FOR_QUOTE_OUTREACH`
- `READY_FOR_OWNER_APPROVAL`
- `READY_FOR_BID_DECISION`

unless material discoverable eligibility/access prerequisites are evaluated.

## Reuse

| Existing | Role |
|----------|------|
| `company_eligibility.py` | Set-aside held/unsupported certs |
| `m3_eligibility_evidence_read.py` | COO/NMR/set-aside evidence binding |
| New `eligibility_gate/` | Vehicle/BOA/IDIQ/JCP/access detection + company profile compare |

No giant vehicle catalog. Detection is solicitation-text + documents + operator profile.

## Overall status enum

| Status | Meaning | Quote-ready? |
|--------|---------|--------------|
| `ELIGIBLE_CONFIRMED` | Profile confirms all material prereqs | Allowed (if other gates pass) |
| `ELIGIBLE_CONDITIONAL` | Path exists but not confirmed / timing uncertain | **No** → `ELIGIBILITY_ACTION_REQUIRED` |
| `NOT_CURRENTLY_ELIGIBLE` | Missing holder/member status | **No** |
| `ELIGIBILITY_UNKNOWN` | Requirement detected, company status UNKNOWN | **No** |
| `ELIGIBILITY_NOT_APPLICABLE` | No special vehicle/access prereq found | Allowed |

## Evaluation order (Phase H readiness)

1. opportunity viability / classification  
2. deadline viability  
3. **eligibility / vehicle / access** ← hard gate (outranks economics)  
4. document sufficiency  
5. product identity  
6. history / economic basis  
7. quote economics  
8. funding  
9. actionability  

Economics cannot produce `READY_FOR_QUOTE_OUTREACH` / bid / owner-approval while the eligibility gate blocks.

## Company profile

Minimal `CompanyEligibilityProfile` (file-backed / env-extendable):

- SAM / CAGE / UEI (optional)
- vehicles_held: list (e.g. `BOAST_BOA`) — default empty → UNKNOWN/not held
- jcp_status, clearances, approved_sources — default UNKNOWN

Do not invent holdings. UNKNOWN stays UNKNOWN.

## BOAST regression

Title/description containing BOAST + RFOP / BOA holder language → vehicle required.  
If `BOAST_BOA` not in company vehicles_held → not quote-ready; next action eligibility-first.

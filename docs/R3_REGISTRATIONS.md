# R3 — Registrations

**Module:** `response_engine.registrations_r3`  
Integrates with existing Registrations UI / `phase_l.registration_tracker`.

## Modes

| Situation | Operator mode |
|-----------|----------------|
| Easy state/local vendor registration | `REGISTER_BEFORE_BID` (not immediate reject) |
| CAGE missing for DIBBS/DLA | `DIBBS_CAGE_REQUIRED` hard block |
| Active SAM where required | PASS when verified ACTIVE |
| JCP for export-controlled data | required / approved / pending / unknown |

## CAGE states

`ACTIVE` | `PENDING` | `VALIDATION` | `ADDRESS_REVIEW` | `REJECTED` | `UNKNOWN`

Do not assume. Profile default is UNKNOWN.

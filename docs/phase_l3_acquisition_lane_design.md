# Phase L.3 — Acquisition Lane Design

**Build:** `20260927-m3-phase-l3-commercial-acquisition-rebalance`

## Lanes (priority order)

1. `COMMERCIAL_OPEN_CHANNEL`
2. `COMMERCIAL_DISTRIBUTOR_CHANNEL`
3. `QUOTE_REQUIRED_COMMERCIAL`
4. `MILSPEC_OPEN_CHANNEL`
5. `UNKNOWN_ACQUISITION_CHANNEL`
6. `MILSPEC_SPECIALTY` → `SPECIALTY_ACQUISITION_RESEARCH` (not rejected)
7. `SOURCE_APPROVAL_REQUIRED`
8. `SOLE_SOURCE_RESTRICTED`

Hard rule: difficult channel ≠ eligibility rejection.

## Research intensity

| Lane | Spend |
|------|-------|
| Commercial open/distributor | Full public-price + history |
| Quote-required | History + max buy + suppliers + limited price pass |
| Mil-spec specialty | Cheap history → specialty pipeline |
| Approval / sole | Blocked-profit track only |

## Reverse economics

`MAXIMUM_BUY_PRICE` from government unit − profit targets − freight/fees − financing.

# Build 002K — API Governance Audit

**Build ID:** `20261122-m3-2-build-002k`  
**Paid acquisition final state:** LOCKED  
**Safeguard tests:** PASSED

## Permanent hard limits (enforced in code)

| Limit | Value |
|-------|------:|
| Daily system-wide | 100 |
| Per job | 50 |
| Authorized test session | 100 |
| Monthly reserve floor | 1000 |

Effective available = lowest remaining applicable limit.

## Live test consumption

| Metric | Result |
|--------|--------|
| Provider before | used=3054 remaining=2946 |
| Provider after | used=3070 remaining=2930 |
| Provider delta | 16 |
| Local test_used | 16 |
| Within 100 | True |
| Live executed | True |

## Conditions

```json
{
  "safeguard_tests_passed": true,
  "usage_verified": true,
  "estimated_total_max_le_100": true,
  "monthly_reserve_ok": true
}
```

## Blocker (if any)

```json
null
```

Controller ledger: `data/nextbid_ledger/`. Cache: `data/nextbid_cache/`.

# SAM API Budget Policy

Hard daily limit: **10** live Opportunities API calls.

Config (single source, checked in order):

1. `SAM_DAILY_CALL_BUDGET`
2. `SAM_API_CALL_LIMIT`
3. `SAM_DAILY_API_BUDGET` (default 10)

## Rules

- Calendar day uses `SCHEDULER_TIMEZONE` (default America/Denver)
- Reserve **1** call by default (`SAM_DAILY_RESERVE_CALLS`)
- Cache hits cost **0**
- No automatic retries
- Tests must not call live SAM
- Canonical client: `discovery.sam_budgeted_client`

## Current

SAM: 3/10 calls used | 7 remaining

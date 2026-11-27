# Acquisition Schedule

Timezone: `SCHEDULER_TIMEZONE` (default `America/Denver`).

| Channel | When | What | Spend |
| --- | --- | --- | --- |
| **Free portals** | **08:00** and **15:00** | Full free-board pull + change-watch fingerprint | $0 pull credits |
| **Paid (NextBid)** | **08:00** once/day | Budgeted discovery/detail within API call caps | NextBid credits (when unlocked) |

## Env

```
SCHEDULER_ENABLED=false          # must be true for daemons to start
FREE_REFRESH_TIMES=08:00,15:00
PAID_REFRESH_TIME=08:00
SCHEDULER_TIMEZONE=America/Denver
```

Paid caps (existing): `nextbid_daily_call_limit`, `nextbid_job_call_limit`, plus emergency stop / `NEXTBID_ENABLED`.

## Code

- Policy: `m3/research/schedule_policy.py`
- Daemons: `m3/research/nextbid_schedule.py`
- Manual free pull: `python -m m3.research.jobs free-source-daily`
- Manual paid: only when NextBid unlocked

## Safety

Auto-run stays **off** until `SCHEDULER_ENABLED=true`.  
NextBid stays blocked while emergency stop / `NEXTBID_ENABLED=false`.

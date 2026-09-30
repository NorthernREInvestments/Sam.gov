# Phase L.14 — BidNet Authenticated History Parked

**Build:** `20260928-m3-phase-l14-nonbidnet-source-expansion`

## State

`BIDNET_AUTH_HISTORY_PARKED`

| Field | Value |
|-------|-------|
| blocked history | 82 (from L.13 still_blocked) |
| unlock | free vendor registration |
| priority | deferred_by_owner |
| discovery | may continue where already working |
| engineering | **no current effort** |

## Forbidden this phase

- BidNet registration / login
- anti-bot evasion / CAPTCHA
- BidNet history scraping
- award-page workarounds

## Artifacts

- `data/phase_l14_bidnet_auth_history_parked.json`
- `artifacts/phase_l/l14_bidnet_parked.json`

## Reopen

Only when owner explicitly unparks or registers. Prevents accidental reopen in later phases.

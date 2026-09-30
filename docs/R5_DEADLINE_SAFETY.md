# R5 Deadline Safety

**Build:** `20260929-m3-r5-preflight-submission-ui-sync`

## Canonical deadline

Date + time + timezone + source. Never compare naive datetimes.

## Internal target

Optional `internal_submission_target` earlier than legal deadline. Does **not** replace the legal deadline.

## Late submission

After controlling deadline → preflight `FAIL` → `SUBMISSION DEADLINE PASSED`.  
Do not encourage submit unless solicitation explicitly permits.

## UI urgency

Escalate presentation for >24h / <24h / <4h / <1h using existing app styles. Never auto-submit because deadline is near.

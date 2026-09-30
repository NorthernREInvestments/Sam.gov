# R5 Submission Adapters

**Build:** `20260929-m3-r5-preflight-submission-ui-sync`  
**Engine:** `response_engine.submission_adapters`

## Types

`PORTAL` · `DIBBS` · `PIEE` · `EMAIL` · `PHYSICAL` · `OTHER`

## Behavior

- Guided checklist in plain language.
- `DRY_RUN` default — tests never live-submit.
- Final submit always requires owner/human confirmation.
- No MFA / CAPTCHA / OTP bypass.
- No uncontrolled browser automation in R5.

## Portal price check

Entered portal price must match approved offer or hard-stop: `SUBMISSION_DATA_CONFLICT`.

## API

- `GET /api/response-projects/{id}/submission-plan`
- `POST /api/response-projects/{id}/submission-events` (`dry_run=true` default; `live_submit` forbidden)
- `POST /api/response-projects/{id}/validate-portal-price`

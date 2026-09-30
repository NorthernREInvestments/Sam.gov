# R5 Final Preflight

**Build:** `20260929-m3-r5-preflight-submission-ui-sync`  
**Engine:** `response_engine.r5_preflight` / `response_engine.r5_service.run_r5_preflight`

## Purpose

Deterministic mandatory checks immediately before owner approval / guided submission.

Not “AI thinks the package looks good.”

Example summary: `59/62 mandatory checks passed · 0 hard failures · 2 warnings`.

## Categories

Solicitation · Response package · Pricing · Technical · Company/compliance · Signatures · Submission / freeze / firewall

## States

`PASS` · `WARNING` · `FAIL` · `OWNER_ACTION_REQUIRED` · `MANUAL_REVIEW_REQUIRED`

Approval eligible only when: **0 FAIL** and **0 OWNER_ACTION_REQUIRED**.

## API

- `POST /api/response-projects/{id}/preflight`
- `GET /api/response-projects/{id}/preflight`

## Safety

0 live SAM calls. 0 external side effects.

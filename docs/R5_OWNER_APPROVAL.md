# R5 Owner Approval

**Build:** `20260929-m3-r5-preflight-submission-ui-sync`  
**Engine:** `response_engine.r5_approval`

## Model

`OwnerSubmissionApproval` is **package-version specific**.

Statuses: `PENDING` · `APPROVED` · `REJECTED` · `CHANGES_REQUESTED` · `EXPIRED_DUE_TO_CHANGE`

## Rules

- Explicit owner action only — no hidden auto-approval.
- Material changes (price, product, amendment, compliance, buyer-facing files) invalidate approval.
- Freeze (`FrozenSubmissionPackage`) records exact files/hashes/price — freeze ≠ submit.

## UI

Bid Prep → **Review & submit** → `APPROVE FOR SUBMISSION` after clean preflight.

Owner queue: Today → **OWNER ACTIONS**.

## API

`POST /api/response-projects/{id}/owner-approval` with `decision=APPROVED|REJECTED|CHANGES_REQUESTED`

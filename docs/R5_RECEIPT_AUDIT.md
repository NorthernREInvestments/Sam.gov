# R5 Receipt & Audit

**Build:** `20260929-m3-r5-preflight-submission-ui-sync`  
**Engine:** `response_engine.submission_audit`

## Distinction

Upload complete ≠ submit clicked ≠ buyer accepted ≠ confirmation generated.

Preferred success: `SUBMITTED_CONFIRMED` with receipt evidence.  
Fixture/test: `DRY_RUN_SUBMITTED_CONFIRMED` labeled **TEST SUBMISSION — NOT SENT**.

Without confirmation: `SUBMITTED_UNCONFIRMED` → UI `VERIFY SUBMISSION NOW`.

## Models

- `SubmissionEvent`
- `SubmissionReceipt` (hashed artifacts)
- `SubmissionAuditPackage` (immutable snapshot)

## API

- `POST /api/response-projects/{id}/receipt`
- `GET /api/response-projects/{id}/submission-audit`

Revisions create new submission versions — originals are not mutated.

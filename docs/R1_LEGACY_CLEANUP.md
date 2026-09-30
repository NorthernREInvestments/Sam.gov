# R1 Legacy Cleanup / Reconciliation

## Found

| Stack | Disposition |
|-------|-------------|
| `bid_compliance_*`, `bid_requirement_extraction`, `compliance_matrix`, `governing_documents` | **RETAINED** — still serves `/api/opportunities/.../compliance`; R1 is the new canonical response-project foundation; migrate consumers gradually |
| `draft_bid_assembly`, `bid_package`, `bid_readiness_engine` | **RETAINED** as later R2+ dependency |
| `phase_l.prebid_compliance` | **RETAINED** funnel gate until R1 owns READY_TO_BID |
| `proposal_service` / `static/proposal.js` | **SUPERSEDED** for operator path; not deleted |
| `submission_package` SF1449 helpers | **RETAINED**; R1 FORM/SIGNATURE categories cover schema |
| Owner Bid Prep checklist stub | **MIGRATED** surface to R1 operator summary |

## Removed

None deleted in R1 (avoid breaking live APIs). Parallel engines not stacked into Bid Prep — Bid Prep now reads `response_engine` first.

## Remaining debt

- Wire opportunity package retrieval → auto document ingest
- Dual readiness ladders (`bid_compliance` vs R1) until cutover
- SF-33 specific form population still future

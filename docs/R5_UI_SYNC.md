# R5 UI Sync

**Build:** `20260929-m3-r5-preflight-submission-ui-sync`  
**Presenter:** `response_engine.operator_state_service`

## Hard gate

Backend blockers must appear in the operator UI with:

- plain-language status
- one next action
- role (operator vs owner)
- destination

`DEAD_END_DEALS = 0` for validated corpus.

## Normal UI language

Hide R1–R5 codes. Example: `Need country-of-origin proof` not `R3 TRADE_COMPLIANCE_REVIEW`.

## Surfaces

| Surface | Role |
|---------|------|
| Home | Owner-level attention cards including Owner Actions / Responses / Submissions |
| Today | Action queues (calls → owner → responses → submissions → awaiting) |
| Bid Prep | Deal workflow including Review & submit |
| Advanced | Internal codes / hashes / provenance |

## Stages (plain)

Opportunity → Supplier → Product → Compliance → Response → Approval → Submission → Result

# R4 — Response Document Generation

**Build:** `20260929-m3-r4-buyer-forms-response-generation`  
**Canonical:** `response_engine.r4_service.run_r4_generation`  
**Max state:** `READY_FOR_R5_PREFLIGHT` (never `READY_TO_SUBMIT`)

## Purpose

Turn verified R1/R2/R3 facts into the buyer’s actual response package:

ResponsePlan → field maps → form/spreadsheet/quote/matrix generation → hashed package → R5 handoff.

## Absolute rules

- Deterministic compiler — not an AI proposal writer
- Fail-closed on UNKNOWN (no guessed N/A / zero / prior answer)
- No auto-signature / no auto-attestation
- No submission (portal/email/physical)
- Buyer originals immutable; working/generated copies versioned
- Internal economics / supplier quotes stay out of buyer-facing output

## Modules

| Module | Role |
|--------|------|
| `response_plan` | Deliverable inventory from R1 |
| `field_map` | ResponseFieldMap from R2/R3 |
| `spreadsheet_fill` | openpyxl population; formula preserve; XLSM manual |
| `document_generators` | Quote, matrix, narrative, certs, DOCX, PDF-safe |
| `package_store` | Versioned dirs, hashes, ZIP allowlist, SubmissionHandoff |
| `r4_service` | Orchestration + Bid Prep card |

## Operator flow

1. Bid Prep → resolve R1–R3 blockers  
2. **BUILD RESPONSE PACKAGE**  
3. Review drafts labeled `DRAFT — NOT SUBMITTED`  
4. Owner signatures / attestations remain pending  
5. Hand off to R5 preflight  

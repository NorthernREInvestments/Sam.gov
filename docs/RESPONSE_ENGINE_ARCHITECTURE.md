# Response Engine Architecture (R1–R5)

**Build R1:** `20260929-m3-r1-solicitation-response-compiler-foundation`  
**Build R1.1:** `20260929-m3-r11-production-intake-legacy-cutover`  
**Build R1.2:** `20260929-m3-r12-production-corpus-ocr-amendment-hardening`  
**Build R1.3:** `20260929-m3-r13-real-corpus-final-validation`  
**Build R2:** `20260929-m3-r2-clin-pricing-product-compliance`  
**Build R3:** `20260929-m3-r3-company-compliance-reps-certs-trade`  
**Build R4:** `20260929-m3-r4-buyer-forms-response-generation`  
**Build R5:** `20260929-m3-r5-preflight-submission-ui-sync`  
**Package:** `response_engine/`

## Purpose

M3 is a **solicitation response compiler**, not a generic proposal writer.

```
SOLICITATION PACKAGE → R1 requirements
→ R2 CLIN / product / technical / economics
→ R3 company eligibility / NMR / trade / 889 / attestations
→ R4 ResponsePlan → forms / spreadsheets / quote / matrix / package
→ R5 preflight → owner approval → freeze → guided adapters → receipt / audit
```

Operator UI exposes plain next actions only — never R1–R5 labels in normal screens.

## Permanent invariants

1. **Never silently guess** material requirements → `UNKNOWN` / blocked
2. **Fail closed** on material UNKNOWN/FAIL
3. **Discovery ≠ Authoritative ≠ Submission** systems stay separate
4. **Clean-room firewall:** internal evidence ≠ government submission content
5. Supplier quotes never auto-attach to government responses
6. **One canonical operator presenter:** `operator_state_service`
7. **Never claim ready-to-submit** without R5 preflight + owner approval + receipt path
8. **One canonical pricing path:** `r2_service`
9. **One canonical company/compliance path:** `r3_service`
10. **One canonical response generation path:** `r4_service`
11. **One canonical submission workflow:** `r5_service` (DRY_RUN default; never auto-sign / auto-submit)
12. **0 live SAM API** in R1–R5 automated tests; **0 external side effects** in R5 tests

## Modules

| Module | Role |
|--------|------|
| `r5_service` / `r5_preflight` / `r5_approval` / `r5_signatures` | Final preflight, owner approval, signatures |
| `submission_adapters` / `submission_audit` | Guided adapters + receipt/audit |
| `operator_state_service` | Plain-language next action / stages for UI |
| `r4_service` | Canonical response package generation |
| `response_plan` / `field_map` | Plan + ResponseFieldMap |
| `spreadsheet_fill` / `document_generators` / `package_store` | Outputs + hashes + handoff |
| `r3_service` | Company / compliance |
| `r2_service` | CLIN / product / economics |
| (R1 modules) | Documents / requirements / amendments |
| `service.py` | Orchestration + Bid Prep UI enrichment |

## Lifecycle states

R4 caps packages at draft / `READY_FOR_R5_PREFLIGHT`.  
R5 may reach `SUBMITTED_CONFIRMED` only with user-driven submission + receipt (fixtures use `DRY_RUN_SUBMITTED_CONFIRMED`).

## Extensibility

Future work should favor live opportunity execution, real quotes, CAGE unlock, and outcomes — not additional architecture layers for their own sake.

# R3 — Company Compliance / Representations / Certifications

**Build:** `20260929-m3-r3-company-compliance-reps-certs-trade`  
**Canonical service:** `response_engine.r3_service.run_r3_analysis`  
**Verdict target:** `PHASE_R3_COMPANY_COMPLIANCE_READY`

## Purpose

R3 answers whether the **company** is eligible to offer, which reps/certs apply, what must be true at submission vs pre-award, what SAM covers, what needs owner attestation, and whether NMR / trade / DoD access block the response.

R3 does **not** auto-certify. R3 does **not** fill SF1449 / submit bids (R4+).

## Accuracy rule

Every answer traces to solicitation, amendment, incorporated clause, verified SAM/company data, SBA/gov source, supplier/OEM evidence, or explicit owner confirmation. Else: `UNKNOWN`. Material unknown: `COMPLIANCE_BLOCKED`.

## States

- Applicability: `APPLIES` | `DOES_NOT_APPLY` | `POSSIBLY_APPLIES` | `UNKNOWN`
- Answers: `VERIFIED_*` | `OWNER_CONFIRMED_*` | `REVIEW_REQUIRED` | `UNKNOWN` | `NOT_APPLICABLE`
- Matrix: `PASS_VERIFIED` | `FAIL` | `OWNER_CONFIRMATION_REQUIRED` | `REVIEW_REQUIRED` | `UNKNOWN` | `NOT_APPLICABLE`
- Max readiness: `READY_FOR_RESPONSE_BUILD` — never `READY_TO_SUBMIT`

## Modules

| Module | Role |
|--------|------|
| `company_profile_r3` | Versioned CompanyComplianceProfile from eligibility JSON |
| `nmr` | Nonmanufacturer Rule engine |
| `trade_compliance` | Buy American / TAA / line COO |
| `section889` | Annual SAM vs solicitation-specific vs owner |
| `registrations_r3` | SAM/CAGE/DIBBS/JCP/state REGISTER_BEFORE_BID |
| `cyber_dod` | CMMC/DFARS/DPAS only when clauses present |
| `owner_attestations` | Explicit confirm; audit log; no auto-answer |
| `r3_service` | Orchestration + Bid Prep card |

## Integration

- Consumes R1 requirements + R2 products/lines
- Wired into `compile_project` and Bid Prep card
- APIs under `/api/response-projects/{id}/r3*`
- Settings: `/api/company-compliance-profile`
- 0 live SAM API calls

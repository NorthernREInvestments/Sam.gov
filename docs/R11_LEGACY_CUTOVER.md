# R1.1 Legacy Cutover

## Canonical source of truth

`ResponseProject` + `SolicitationDocument` + `SolicitationRequirement` + `ComplianceMatrix` (R1 response engine).

## Operator-facing readiness

**R1 wins.** `legacy_bridge.wrap_legacy_bid_readiness` / `wrap_legacy_compliance_matrix` wrap legacy API responses so legacy `READY` cannot override R1 `BLOCKED` / `REVIEW_REQUIRED` / incomplete packages.

Invariants for R1.1:

- `ready_to_submit: false`
- `bid_ready: false`

## Disposition

| Component | Disposition |
|-----------|-------------|
| `bid_compliance_engine` | Deprecated compatibility — still runs for old shapes; not Bid Prep authority |
| `bid_requirement_extraction` | Deprecated for Bid Prep — R1 `requirements.py` canonical |
| Governing-document helpers | Adapted — R1 `document_graph` owns ResponseProject graph |
| Legacy compliance matrix API | Compatibility wrapper → R1 matrix when project exists |
| `proposal_service` / narrative UI | Superseded — hidden from normal operator flow |
| Old bid package / draft assembly | Retained for future R4/R5; not readiness |
| Owner Bid Prep UI | Migrated — shows R1 package + compilation panels |

## Dual PASS/FAIL forbidden

No independently calculated operator READY/BLOCKED from legacy once a ResponseProject exists.

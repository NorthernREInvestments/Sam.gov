# Phase D — Execution + Compliance Capability Audit

**Date:** 2026-09-22  
**Rule:** Reuse existing extractors; do not create a parallel workflow. Projection/orchestration only.

---

## Summary matrix

| Capability | Status | Disposition |
|------------|--------|-------------|
| Solicitation / document extraction | EXISTS | REUSE / EXTEND orchestration |
| Clause extraction (DLA packaging/inspection) | PARTIAL | REUSE `m3_dla_clause_extraction` |
| FAR/DFARS general catalog | PARTIAL | EXTEND later; DLA-focused only today |
| Compliance modules | EXISTS (parallel stacks) | REUSE `bid_compliance_*` as primary |
| Packaging / MIL-STD | EXISTS | REUSE |
| Delivery / shipping | PARTIAL | EXTEND via normalized model |
| FOB | PARTIAL | EXTEND |
| Inspection / acceptance | PARTIAL | EXTEND + post-award checklist |
| Invoicing / WAWF / PIEE | PARTIAL | REUSE invoice CRM; extract WAWF flags; no e-submit |
| Supplier validation | EXISTS | EXTEND → confirmation checklist |
| Bid prep / submission | EXISTS | REUSE `bid_submission_intelligence` + `submission_package` |
| Deadline handling | EXISTS | REUSE `deadline_runtime` |
| Amendments | EXISTS (split pre/post) | EXTEND conflict/re-eval |
| Quantity / UOM / CLIN | EXISTS | REUSE `micro_purchase_lab_integrity` |
| COO / Buy American / TAA | EXISTS (signal-level) | REUSE; no legal conclusions |
| operator_workflow BID_PREPARATION gates | EXISTS | EXTEND with execution blockers |
| procurement_package / deal-room APIs | EXISTS | EXTEND attach execution profile |
| Unified ExecutionRequirement object | MISSING | **CREATE** (this phase) |
| Post-award ordered task list from requirements | PARTIAL (`m3_execution_os_read` stages) | EXTEND / wire |
| Invoice checklist from solicitation | MISSING | **CREATE** |
| Supplier quote packet (content only) | PARTIAL | EXTEND |

---

## Detail

### 1. Solicitation / document extraction — EXISTS
- `m3_document_intelligence.py`, `deep_deal_documents.py`, `solicitation_package_retrieval.py`, `table_extractors.py`, `attachment_pipeline.py`
- Multiple stacks; Phase D orchestrates text → requirements, does not replace extractors.

### 2. Clause extraction — PARTIAL
- Strong: `m3_dla_clause_extraction.py` (MIL-STD-2073/129, FOB, inspection, acceptance, FAT).
- Weak: FAR catalog essentially `52.219-14` in `attachment_pipeline.py`.
- Phase D: classify DLA/extracted clauses into EXECUTION/FINANCIAL/ELIGIBILITY/INFORMATIONAL/UNKNOWN with plain-English “what to do.”

### 3. Compliance — EXISTS (overlap risk)
- Primary: `bid_compliance_engine.py`, `bid_requirement_extraction.py`, `compliance_matrix.py`, `product_bid_compliance.py`
- Parallel: `deep_deal_compliance.py`
- **CONTRADICTORY risk:** dual evaluators + dual checklists vs mobile checklist. Phase D consumes bid_requirement + DLA clauses into one ExecutionRequirement list; does not delete either stack.

### 4–7. Packaging / Delivery / FOB / Inspection — EXISTS–PARTIAL
- Reuse DLA clause patterns + `bid_submission_intelligence.extract_delivery_logistics` + µLab `assess_execution_and_funding`.

### 8. Billing / WAWF / PIEE — PARTIAL
- Invoice CRM: `performance_service.py`, `ContractInvoice`
- WAWF: password reminder only (`performance_settings.py`)
- PIEE: PDF fetch + submit-via-PIEE hints
- Phase D: extract invoice/WAWF/payment **requirements** into checklist; no external filing.

### 9–11. Supplier / Bid prep / Deadlines — EXISTS
- Reuse commercial verification, submission intelligence, deadline_runtime.

### 12. Amendments — EXISTS
- Pre-award: `governing_documents.apply_amendment_supersession`, `bid_compliance_invalidation`
- Post-award: `amendment_monitor`
- Phase D: conflict detection + re-eval affected categories; latest amendment wins when determinable.

### 13–14. CLIN / COO — EXISTS
- µLab CLIN model; origin classification is signal-level (“not legal advice”).

### 15–16. Operator gates / Deal room — EXISTS
- Evidence gates: economics / supplier / funding only today.
- Phase D: add execution-critical blockers (mandatory packaging unknown, incomplete submission, supplier confirmation missing) without rewriting scoring.

### Contradiction watchlist (documented, not “fixed by rewriting scoring”)
1. `COMPLIANCE_BLOCKED` can map to `BID_PREPARATION` in `operator_workflow/mapping.py` while bid_readiness blocks assembly.
2. Dual compliance evaluators (`bid_compliance_engine` vs `deep_deal_compliance`).
3. Dual checklists (submission_package vs bid_readiness vs Phase C mobile checklist).
4. DLA “clauses” ≠ FAR 52.xxx catalog.
5. µLab COMPLETE maps to SUPPLIER_VALIDATION (intentional).

Phase D addresses (1)/(3) by projecting a single execution readiness surface for owner approval; does not delete upstream systems.

---

## Phase D build plan (implemented next)
1. `execution_requirements/` — normalized model + statuses + categories  
2. Aggregate extractors → ExecutionRequirement[] with provenance  
3. Conflict / amendment logic  
4. Supplier confirmation + quote packet (content only)  
5. Post-award / invoice / payment checklists + cash-cycle handoff  
6. Owner approval gate + attach to deal-room / operator projection  
7. Targeted tests → one full suite at end  

# Accuracy Regression Log

Permanent log of real accuracy defects, causes, fixes, and tests. Append new failures here.

| ID | Defect | Cause | Correction | Regression test | Live status |
| --- | --- | --- | --- | --- | --- |
| AR-001 | `source_documents` ignored | Parsers looked for `documents`/`files`/`items`/`results` only | `document_inventory.extract_document_entries` + `document_recovery_run._extract_doc_entries` + `local_evidence` inventory metadata | `test_source_documents_parsed_into_inventory` | Offline proven |
| AR-002 | Completeness by text length | Long description → `complete_available_package` | `package_completeness()` evidence-based labels | `test_package_completeness_ignores_long_text` | Offline proven |
| AR-003 | False supplier-quote / bid ready with UNKNOWN qty (NB00261711 pattern) | Promotion ignored missing mandatory quantities | `ai_output_verify` readiness blockers + partial flag | `test_unknown_quantity_blocks_full_supplier_quote_ready` | Offline proven; live AI PENDING |
| AR-004 | Expired signed download URL reused | Cached URL treated as permanent | `signed_url_expired` + recovery refetch path | `test_signed_url_expired` | Offline proven; live download PENDING |
| AR-005 | Wrong document selected (Specimen Contract vs product PDF) | First/high-score without demoting specimen | `select_best_source_document` wired into recovery | unit check + inventory tests | Offline selection proven; live PENDING |
| AR-006 | Knew docs existed but never listed (33 of 37) | Pipeline stopped after `get_opportunity` | SOP + inventory pending queues + recovery decision rules | cohort offline reprocess counts `awaiting_document_retrieval` | Process fixed offline; paid list PENDING auth |
| AR-007 | Package–document DB association weak (1 row / 43 packages) | Incomplete association writes | Inventory files + original bytes association path | offline reprocess inventories | Partial; DB association hardening ongoing |
| AR-008 | QVL categories treated as firm line items (NB00080682) | Over-eager product extraction | Verification + readiness (no firm qty → not ready) | readiness gates + case notes | Fixture expansion recommended |
| AR-009 | Installation obligation missed (outboard motors p.47) | Incomplete doc coverage / truncation risk | Coverage gates; large-doc section processing rules in SOP | SOP/coverage tests; fixture if PDF present | PENDING fixture completeness |
| AR-010 | Local metered commits (11) vs provider Δ (10) | Timeout/uncertain reservations counted locally | Keep conservative reservation; reconcile to provider used | controller ledger behavior | Historical; do not weaken limits |

## Regression cases (do not hardcode production answers)

1. NB00321554 diesel pump — duplicates / expired deadline  
2. NB00320329 forklifts — multi-line + dual delivery  
3. Jefferson 26-079/MR outboards — hidden install  
4. NB00261711 lubricants — UNKNOWN qty blocks full ready  
5. NB00375309 propane — reuse evidence; avoid specimen; expired URL  
6. NB00080682 QVL — not firm orders  

Tests: `tests/test_document_inventory_corrective.py`, `tests/test_solicitation_sop.py`.

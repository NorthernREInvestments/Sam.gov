# Phase L.10 — Legacy Cleanup

## Removed / reconciled

- Loose category-benchmark promotion to validated (obsolete rule `LEGACY_LOOSE_CATEGORY_BENCHMARK_AS_VALIDATED`)
- Generic supplier auto-promotion (`LEGACY_GENERIC_SUPPLIER_AUTO_PROMOTE`)
- Duplicate history branches / seed-as-exact (`LEGACY_DUPLICATE_HISTORY_BRANCH`)
- READY-without-evidence-grades (`LEGACY_READY_WITHOUT_EVIDENCE_GRADES`)
- Fixed Stage 3 / deep-research / queue caps (still asserted false)

## Single live path

`CanonicalOpportunityWorkflow` + `run_phase_l10_exact_workflow`  
`l6`–`l9_rescue` retained for regression/replay only.

## Graphs / memory

- `data/phase_l10_product_history_graph.json`
- `data/phase_l10_supplier_graph.json`
- `data/phase_l10_buyer_retrieval_memory.json`

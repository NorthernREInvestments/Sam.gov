# Master Corrective Build — Owner Report

**Finished:** 2026-11-26 (UTC)  
**Paid API this build:** NextBid 0 · SAM 0 · OpenAI $0.00  
**Emergency stop:** ENGAGED · nextbid_enabled=false · openai_enabled=false · bidnet_enabled=false

## Why M3 previously failed — and what is corrected

M3 stopped after `get_opportunity` even when `document_count > 0`, ignored NextBid `source_documents` in inventory/evidence paths, treated long local text as a “complete package,” allowed AI/promotion paths to mark supplier-quote and bid ready with UNKNOWN mandatory quantities, reused expired signed URLs / selected specimen contracts, and never ran BidNet deep-read on this cohort. Corrections: canonical `document_inventory` + evidence wiring, evidence-based package labels, deterministic readiness gates (partial vs full), signed-URL expiry + document selection, offline reprocess of all 82, permanent docs/tests. **Live original-byte retrieval and live AI remain PENDING.**

## 1. Root causes corrected

| Problem | Component | Correction | Test | Live |
| --- | --- | --- | --- | --- |
| Ignored `source_documents` | `document_inventory`, `local_evidence`, `document_recovery_run` | Parse source/nextbid_documents; inventory ≠ corpus text | `test_source_documents_parsed_into_inventory` | Offline YES |
| Premature stop / no list | SOP + queues + recovery | Pending-doc queues; recovery uses inventory | SOP + offline reprocess | Paid list PENDING |
| Completeness by text length | `package_completeness` | Evidence labels only | `test_package_completeness_ignores_long_text` | Offline YES |
| False quote/bid ready | `ai_output_verify` | Qty/docs/deadline blockers; AI cannot bypass | `test_unknown_quantity_blocks_…` | Offline YES |
| Expired signed URLs | `signed_url_expired` + recovery | No reuse when expired | `test_signed_url_expired` | Live download PENDING |
| Wrong doc (specimen) | `select_best_source_document` | Demote specimen; prefer product names | selection unit + recovery wire | Live PENDING |
| Weak associations | inventory by nb_id | Rebuild from stored manifests | offline reprocess | Partial (no bytes yet) |
| BidNet assumed | — | Documented disabled/unverified | docs | PENDING |

## 2. 82-record reconciliation

| Metric | Count |
| --- | --- |
| Original cohort | 82 |
| Active | 69 |
| Rejected | 6 |
| Archived | 7 |
| Unaccounted | **0** |
| Complete verified relevant packages | **0** |
| Awaiting document retrieval | 30 |
| Incomplete listing/body | 26 |
| Incomplete/blocked | 12 |
| Partial supplier-pricing | 1 (NB00261711) |
| Supplier-pricing ready | **0** |
| Bid-ready | **0** |

Artifacts: `data/acceptance/corrective_offline_reprocess_82/{PER_SOLICITATION.json,SUMMARY.json,RECONCILIATION.txt}`

## 3. Recovered information

- Manifests for NB00286337 (3 docs), NB00375193 (4), NB00375309 (12) rebuilt from paid store — **metadata**, not bytes.  
- Previously ignored: `source_documents` / related keys.  
- Cached AI revalidated under `ai_doc_understanding/revalidated/`; false full quote promotions cleared (0 full ready).  
- NB00261711 correctly **partial** (unknown engine oil qty).

## 4. Remaining missing

- Original bytes for inventoried attachments (directory empty — prior recovery did not persist verified bytes).  
- 30 actives still need authorized retrieval; many others lack substantive body.  
- BidNet Direct unverified. Live AI not run.

## 5. Accuracy validation

- Added/executed: `tests/test_document_inventory_corrective.py`, `tests/test_solicitation_sop.py` → **16 passed**.  
- Evidence: fixtures + stored paid JSON. Mocked network. Live retrieval/AI: **not verified**.

## 6. Cost

| Item | Value |
| --- | --- |
| NextBid before/after this build | unchanged (0 consumed) |
| SAM / OpenAI / other | 0 |
| Emergency stop | ENGAGED |
| Recurring paid jobs | forbidden by killswitch |

Credit discrepancy (prior recovery): provider Δ **10** vs local commits **11** — consistent with uncertain timeout reservation; limits not weakened.

## 7. Git / deploy

See final chat report for commit hash / push / Railway. Deploy only if offline gates pass; paid acquisition remains disabled.

## 8. Proposed next live test

`docs/acceptance_archive/CORRECTIVE_BUILD_PROPOSED_LIVE_TEST.md` — ≤3 credits, start NB00375309 Bulk Propane PDF. **Not executed.**

## 9. Final determination (summary)

See chat §29 answers. Offline inventory/readiness/accounting: YES where tested. Live acquisition/AI/deploy verification: PENDING or PARTIAL as stated.

# Phase L.2 — Existing Enrichment Reconciliation

Audit before coding. Prefer adapters over new engines.

| Required capability | Existing implementation | Reuse directly | Extend | Link/adapt | Truly missing |
|---|---|---:|---:|---:|---:|
| Product identity (NSN/MPN/CAGE) | `phase_j/product_identity.build_product_identity` | ✓ | | | |
| Qty / UOM live parse | `sam_live_fallback.parse_qty_uom_from_text` | ✓ | | | |
| Qty / UOM from award text | `phase_j/history_reconciliation.parse_qty_uom` | ✓ | | | |
| CLIN / pack normalize | `micro_purchase_lab_integrity.normalize_quantity` | | | ✓ optional | |
| Or-equal / brand policy | `product_bid_compliance` / `execution_requirements` | | | ✓ | |
| Line items (portals) | `transactional_bom` / `m3_document_intelligence` | | | ✓ when docs present | |
| USAspending awards | `usaspending_client.fetch_awards_by_keywords` | ✓ | | | |
| History false-match guard | `phase_j/history_reconciliation.reconcile_awards` | ✓ | | | |
| History lookup budget pattern | `phase_h/deep_research._lookup_history` | | | ✓ copy pattern | |
| Bid-tab / gov revenue | `m3_government_revenue_benchmark` | | | ✓ state/local | |
| Public retail / commercial | `m3_public_pricing_evidence.retrieve_commercial_seller_evidence` | ✓ | | | |
| Supplier public price (AI) | `m3_supplier_intelligence.research_public_pricing_web` | | | ✓ optional gated | |
| Existing price evidence | `m3_supplier_intelligence.collect_existing_price_evidence` | ✓ | | | |
| Micro-lab market collect | `micro_purchase_lab_research.collect_current_market` | | | ✓ adapter shape | |
| Competition signal | `phase_l/competition.annotate_competition` | ✓ | | | |
| Economics / tiers | `phase_l/economics.build_phase_l_economics` | ✓ | | | |
| Normalize / owner state | `phase_l/normalize` | ✓ | | | |
| Access / easy registration | `phase_l/access_gate` (L.1) | ✓ | | | |
| Product research cache | AppSetting indexes in M3 modules | | | ✓ | file cache for L.2 |
| FPDS standalone client | — (SAM Contract Data route only) | | | | N/A — use USAspending |
| Phase L enrichment orchestrator | — | | | | **NEW thin** `phase_l/enrichment.py` |

## Canonical choices

| Concern | Canonical module |
|---|---|
| Identity | Phase J `build_product_identity` |
| Federal history | USAspending + Phase J `reconcile_awards` |
| Market baseline | `m3_public_pricing_evidence` commercial retrieval (no invented discounts) |
| Economics | Phase L `build_phase_l_economics` |
| Competition | Phase L `annotate_competition` |

## Gap that L.2 closes

Phase L hunt set `historical_*` / `public_retail_*` only when already on the row. Live discovery never called history or retail research (`fetch_details=False`). L.2 adds a **thin orchestrator** that feeds existing research into those fields — not a second research stack.

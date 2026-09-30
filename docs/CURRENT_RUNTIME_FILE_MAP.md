# Current Runtime File Map

Build: `20260929-m3-repository-fat-trim-runtime-consolidation`

| Subsystem | Canonical module | Canonical store | Generated outputs | Tests |
|-----------|------------------|-----------------|-------------------|-------|
| Discovery / hunt | `phase_l/hunt.py` | `artifacts/phase_l/accessible_latest.json` | hunt_latest, enrichment_latest | `tests/test_phase_l*` |
| Jurisdiction registry | `discovery/jurisdiction_registry.py` | `data/jurisdiction_procurement_registry.json` | — | coverage via L.17* |
| Canonical funnel | `phase_l/l23_full_population_funnel.py` | `data/l23_canonical_population_store.json` | l23_* summaries | `test_phase_l23_*` |
| Population repair | `phase_l/l231_population_audit_repair.py` | same store | l231_* artifacts | `test_phase_l231_*` |
| Progressive stages | `phase_l/progressive_funnel.py` | (in-memory / row) | — | `test_phase_l26_*` |
| Buyer history | `phase_l/l19_buyer_history_recovery.py` | data graphs + l19_* | l19_research_results | L.19 tests |
| Supplier acquisition | `phase_l/l20_supplier_acquisition.py` | supplier memory | l20_research_results | L.20 tests |
| Quote prep | `phase_l/l21_quote_outreach_prep.py` | l21_* | quote readiness | `test_phase_l21_*` |
| Call desk | `phase_l/l22_supplier_call_desk.py` | l22_workspaces / sessions | call sheets | `test_phase_l22_*` |
| App / UI | `app.py`, `static/` | — | — | API smoke |
| SAM park | `discovery/sam_api_parked.py` | — | — | park status asserts |
| BidNet park | `phase_l/bidnet_parked.py` | — | — | park asserts |

## Artifact retention

| Class | Rule |
|-------|------|
| PERMANENT | Unique intelligence in `data/` + evidence hashes |
| CURRENT | Latest L.18–L.23.1 operational JSON under `artifacts/phase_l/` |
| REPORT | Compact summaries only (no full population copies) |
| DEBUG / TEMP | Delete after run; gitignored |
| TEST | Small fixtures under `artifacts/fixtures/` / `tests/` |

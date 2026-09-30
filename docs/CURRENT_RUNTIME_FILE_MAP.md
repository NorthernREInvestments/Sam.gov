# Current Runtime File Map

Build: `20260930-m3-production-repository-consolidation`

| Subsystem | Canonical module | Canonical store | Tests |
|-----------|------------------|-----------------|-------|
| Discovery / hunt | `phase_l/hunt.py` | `artifacts/phase_l/accessible_latest.json` | phase_l / discovery tests |
| Jurisdiction registry | `discovery/jurisdiction_registry.py` | `data/jurisdiction_procurement_registry.json` | L.17* |
| Canonical funnel | `phase_l/l23_full_population_funnel.py` | `data/l23_canonical_population_store.json` | `test_phase_l23_*` |
| Population repair | `phase_l/l231_population_audit_repair.py` | same store | `test_phase_l231_*` |
| Progressive stages | `phase_l/progressive_funnel.py` | (in-memory / row) | domain smoke |
| Quote economics | `phase_l/quote_economics.py` | quote stores | quote economics tests |
| Quote prep | `phase_l/l21_quote_outreach_prep.py` | l21_* | `test_phase_l21_quote_outreach_prep.py` |
| Call desk | `phase_l/l22_supplier_call_desk.py` | l22 workspaces | `test_phase_l22_supplier_call_desk.py` |
| Operator UI | `phase_l/owner_ui_service.py` + `static/operator.*` | — | `test_owner_ui_operator_console.py` |
| Response R1–R4 | `response_engine/*` | `data/response_projects/` (runtime) | `test_response_engine_r*` |
| Preflight / submission R5 | `response_engine/r5_service.py` | project JSON + audit | `test_response_engine_r5.py` |
| Next action | `response_engine/operator_state_service.py` | — | R5 + domain smoke |
| App / UI entry | `app.py`, `/ops` | — | API smoke |
| SAM park | `discovery/sam_api_parked.py` | — | park asserts |
| BidNet park | `phase_l/bidnet_parked.py` | — | park asserts |

## Artifact retention

| Class | Rule |
|-------|------|
| PERMANENT | Unique intelligence in `data/` + evidence hashes + response corpus |
| CURRENT | Latest L.18–L.23.1 operational JSON under `artifacts/phase_l/` |
| REPORT | Compact summaries only |
| DEBUG / TEMP | Delete after run; gitignored |
| TEST | Small fixtures under `artifacts/response_engine/` / `tests/` |

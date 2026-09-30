# Artifact Retention Policy

| Class | Retention | Examples |
|-------|-----------|----------|
| PERMANENT | Keep indefinitely | `data/*` intelligence, evidence docs (content-hashed) |
| CURRENT | Keep latest only | `accessible_latest`, `l23_*`, `l231_*`, `l21_*`, `l22_*` |
| REPORT | Latest compact summary | phase summaries without full row arrays |
| DEBUG | Delete after run / gitignore | probes, `_t`, live_inspect |
| TEST | Small deterministic fixtures | `artifacts/fixtures/`, slim tiny_end_to_end |
| TEMP | Auto-clean | `__pycache__`, `.pytest_cache` |

## Document storage

Fetched documents: one canonical path under `artifacts/transactional_procurement_evidence/` keyed by content hash / solicitation id. Opportunities reference that path — do not store byte-identical copies per phase.

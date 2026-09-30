# Phase L.5 — Recovered Candidates

`RECOVERED_COMMERCIAL_CANDIDATES` count: **322**

These rows failed L.4 Stage 2 (`no_identity_anchor`) and now pass L.5 Stage 2/3.

Typical restore reasons:

- `descriptive_spec`
- `brand_or_equal` + `descriptive_spec`
- `brand_clue` + `partial_commercial`
- `document_signal` (attachment metadata)

Sample rows are in `artifacts/phase_l/l5_retention_audit.json` → `recovered_commercial_candidates.rows`.

# L.23.1 Legacy Cleanup

- Fixed `canonical_id_for` / `_solicitation_key` to use `solicitation_id`
- Removed score-as-hard-gate stranding; tiers order only
- Split federal WATCH from weak-scoring WATCH
- Call-ready counters use set-diff of opportunity IDs
- RESEARCH NEXT includes all `DEEP_RESEARCH_COMPLETE` / priority queue
- Canonical repair path: `phase_l.l231_population_audit_repair`

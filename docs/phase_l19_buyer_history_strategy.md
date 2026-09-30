# Phase L.19 — Buyer-Specific History Strategy

Build: `20260928-m3-phase-l19-buyer-specific-history-recovery`

## Verdict

`PHASE_L19_BUYER_HISTORY_RECOVERY_WORKING`

Buyer-first recovery order: prior solicitation → award → bid tab → PO → contract register → board/council → payments → term contracts → archives → open-data → platform history → cross-buyer only after exhaustion.

Canonical pipeline: `discover_and_profile_buyer` → structured harvest match → `run_exact_history_recovery` → `run_public_artifact_recovery` → `run_platform_history` → `grade_recovered_award` → `recompute_economics_from_recovery` → `audit_quote_positive`.

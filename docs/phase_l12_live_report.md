# Phase L.12 — Live Report

**Build:** `20260928-m3-phase-l12-auth-walled-history-recovery`  
**Verdict:** `PHASE_L12_PARTIAL_AUTH_HISTORY_RECOVERY`

## Fresh hunt

| Metric | Value |
|--------|-------|
| Terminal status | `COMPLETE_WITH_SOURCE_FAILURES` |
| Raw (live commercial) | 0 |
| Accessible (merged) | 673 |
| Stage 1 | 295 |
| Stage 2 | 176 |
| Stage 3 | 176 |

Resilient hunt retained: per-source wall clocks, process isolation, checkpoint/resume. All 20 commercial BidNet network sources failed/anti-bot (5 timed out); prior accessible corpus merged.

## Gov history upgrades

Starting Gov D: **95**

| Upgrade | Count |
|---------|-------|
| → A | 0 |
| → B | 0 |
| → C | 1 (old corpus) |
| remain D | 94 |

Fresh-corpus A/B/C: 0. Public buyer-path exhaustion ran on all Stage-3 Gov D rows (no cap). Platform block classified as `PLATFORM_HISTORY_BLOCKED` (not `HISTORY_NOT_AVAILABLE`).

## Recovery source productivity

Sparse on current SAM-heavy accessible corpus (commercial BidNet rows absent after hunt failure). Primary C upgrade from prior exact-history memory path.

## Auth barriers

| Barrier | Count / note |
|---------|----------------|
| Free registration | BidNet network unlock prioritized (roadmap) |
| Vendor account | 0 on current Stage-3 modes |
| Buyer account | 0 |
| Private restricted | 0 |
| Anti-bot | 20 BidNet hunt sources failed |
| No public history | SAM federal often needs USAspending/NSN exact path, not free-reg |

`HISTORY_ACCESS_GAP_QUEUE`: 82 · `PUBLIC_HISTORY_RECOVERY_QUEUE`: 12

## Registration priorities

1. **BidNet** — free registration · ~82 opportunities blocked at history layer · high multi-buyer leverage · setup: low · **do not auto-register**

## Supplier upgrades

Prior awardees recovered: 0 · C/D→B/A: 0 (no new award vendors on public paths this pass)

## Quote quality

| | Validated | Secondary | Recon-only |
|--|-----------|-----------|------------|
| Before (L.11) | 2 | 2 | 3 |
| After | 3 | 3 | 3 |

## Competition intelligence

Bidder counts recovered: 0 · bid-price distributions: 0

## Tests

54 passed (`test_phase_l12_auth_history` + `test_phase_l11_exact_history` + `test_phase_l10_exact_workflow`)

## Remaining bottleneck / next move

**Owner registration on high-value platforms (BidNet free vendor account)** — commercial award/history remains behind anti-bot; public buyer pivot alone did not yield Gov A/B on the SAM-dominated merged corpus. Manual authenticated history retrieval is secondary after owner registration. No Phase M. No outreach/quotes/bids.

# Phase L.23.1 — Population Audit + Conversion Repair

Build: `20260929-m3-phase-l231-population-audit-conversion-repair`

## Verdict

`PHASE_L231_POPULATION_FUNNEL_REPAIRED`

## Why 3,365?

L.23 primarily loaded artifacts/phase_l/accessible_latest.json (3,365 rows). That file is the materialized accessible population from the hunt/enrichment pipeline. hunt_latest.json retains summary counts (normalized≈3398) but does not embed full row arrays (research_queue=0, normalized_sample only), so it contributed 0 rows to the union. l10–l13 *_fresh_*.json files were empty or missing. Smaller feeds (l173/l172 accessible-now, L.18–L.21 results) add dozens of rows that largely overlap accessible_latest after dedupe. Therefore 3,365 is not a mysterious truncation — it is the current full accessible snapshot; other 'empty' feeds are superseded summaries or vacant fresh pointers, not a lost 10k pool.

## Population after repair

| Metric | Value |
|--------|-------|
| Raw union | 4441 |
| Canonical unique | 1465 |
| Dedupe reduction | 67.01% |
| READY_TO_CALL | 18 |
| DEEP_RESEARCH_COMPLETE | 376 |
| WATCH_FEDERAL_ACCESS | 673 |
| WATCH_OTHER | 26 |

## Call-ready reconciliation

Prior 17 → total 18 (promoted 14, demoted 13, net 1)

Prior snapshot 17 → current 18 (+14 promoted, -13 demoted, net 1). L.23's 'new=3' with total 17 from previous 15 was inconsistent because import/dedupe could rematch IDs (promotions overlapping imports) and counters used total-imported rather than set-diff of opportunity IDs.

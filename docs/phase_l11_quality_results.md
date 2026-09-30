# Phase L.11 — Quality Results

**Verdict:** `PHASE_L11_PARTIAL_EXACT_HISTORY`

## Why PARTIAL (not WORKING)

Exact-history workflow + resilient hunt are operational, but **Gov A/B upgrades from the Gov D corpus remain near-zero** because public award/tabulation evidence is largely:

- BidNet anti-bot (HTTP 202 empty)
- Auth-walled award tabs (`HISTORY_AUTH_REQUIRED` / `BUYER_VENDOR_ACCOUNT_REQUIRED`)
- Buyer board pivots seeded but rarely yielding parseable public awards without registration

Evidence bar was **not** lowered.

## Resilient hunt

| Status | Result |
|--------|--------|
| Terminal state | `COMPLETE_WITH_SOURCE_FAILURES` |
| Hung? | **No** |
| BidNet | Degraded — anti-bot / empty 202 |

## History outcomes (illustrative last run)

See `artifacts/phase_l/l11_summary.json` for exact counts. Typical:

- Many BidNet-derived Gov D → `HISTORY_AUTH_REQUIRED`
- Auth gap queue written for later registration prioritization (**no accounts created**)

## Artifacts

- `l11_gov_d_history_upgrade.json`
- `l11_history_outcomes.json`
- `l11_auth_history_gap_queue.json`
- `l11_fresh_hunt.json`
- `l11_state_coverage.json` / `l11_buyer_type_coverage.json` / `l11_category_coverage.json`
- `l11_summary.json`

## Bottleneck

**Exact buyer award documents are not publicly retrievable at scale** (anti-bot + auth). Next leverage is lawful registration on highest-scoring `AUTH_HISTORY_GAP_QUEUE` portals — outside L.11 stop rules.

## STOP

No Phase M · no outreach · no account creation · no auth bypass · final verification unchanged.

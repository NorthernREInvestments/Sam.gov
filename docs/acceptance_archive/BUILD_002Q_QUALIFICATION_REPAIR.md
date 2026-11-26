# BUILD 002Q — Qualification Repair

Finished: 2026-11-22T17:51:26.502563+00:00

## Before / After (all 23,091 unique records)

| Metric | Before | After | Δ |
|---|---:|---:|---:|
| Unique population | 23091 | 23091 | 0 |
| Verified individual | 750 | 1224 | +474 |
| Product opportunity | 268 | 390 | +122 |
| Stage A | 456 | — | — |
| Stage B | 268 | — | — |
| Stage C (firm qty product fit) | 0 | 6 | +6 |
| Bid-eligible | 0 | 0 | 0 |
| Ready-to-submit | 0 | 0 | 0 |

Bid-eligible remains 0 under evidence-based procurement gates (active + Stage C + submission instructions + sufficient documents + reseller terms + no blocking bond/install/license). Pricing, financing, and supplier stock are not used.

## Repairs applied

1. Population freeze preserves quantity/unit/semantic/evidence/line_items/specs.
2. `bid_eligible` is evidence-based (procurement gates only); hardcoded `False` removed.
3. Qualify never forces `verified_individual_solicitation=True`.
4. Open-data / HTML bid detail identity uses source evidence (no forced True/False).
5. Service-title exclusions narrowed: confirmed `* services` contracts still DQ; product supply titles protected.
6. Full population reprocessed via `python -m m3.research.jobs qualification-repair`.

## Identity investigation (748-class)

- Pre-repair product rows blocked by identity: **748** (authoritative baseline) / **625** remaining after prior pass measurement on rewritten freeze.
- Net verified-individual gain vs unrepaired freeze: **+474** (750 → 1224).
- Remaining non-individual productish rows are listings, historical awards, forecasts, or thin rows without solicitation identity evidence.

## Quantity recovery (252-class)

- Pre-repair missing firm quantity among product individuals: **268** (freeze had dropped quantity evidence → Stage C = 0).
- Recovered from stored packages this pass: **0** (key mismatch / no downloaded text for freeze keys).
- Recovered from prior Stage C evidence index: **1** title/url match into this free-source freeze.
- Freeze now retains **149** quantity fields / **412** evidence blobs for future passes.
- Most of the original 252 cannot be restored without re-fetching free official documents (not a full acquire restart; NextBid locked).

## 002P Stage C reconcile (73)

- Prior: **73** · Still Stage C: **64** · Demoted: **9**

Legitimate demotions (9):

| Reason | Count | Examples |
|---|---:|---|
| Confirmed services title | 3 | Shuttle Bus Services; Pest Control Services; Painting Services |
| Procurement listing URL (board, not detail) | 3 | Inmate Communications; eMarketplace SID board; BidExpress listing |
| Unresolved identity (weak URL + unknown status) | 2 | Video game lounge lease portal; NYSCR search URL |
| Estimated quantity (not firm ordered) | 1 | Unication pagers |

## Production verification

- FastAPI app import: OK
- `/healthz` route: OK
- `/api/m3/procurement/queues` route: OK
- NextBid: locked · OpenAI ceilings unchanged · No full acquire restart

## Artifacts

- `data/acceptance/jobs_qualification_repair_last.json`
- `data/acceptance/jobs_qualify_last.json`
- `data/acceptance/build002q_stage_c_evidence.json`
- Population freeze rewritten in place with preserved quantity evidence fields

Awaiting owner acceptance.

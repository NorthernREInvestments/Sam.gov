# Phase G — Live Run Report

**Date:** 2026-09-24  
**Mode:** `DEVELOPMENT_NO_OUTREACH` — no bids, emails, purchases, financing, or WAWF  
**Primary source:** SAM.gov public opportunities API (`run_federal_sam_bootstrap`)  
**Artifact:** `artifacts/phase_g/live_run_latest.json`

## Fetch

| Field | Value |
|-------|-------|
| Executed | Yes |
| LIVE_SAM_CALLS | 8 |
| Stop reason | `SAM_API_BUDGET` |
| Retrieved | **800** |
| Provenance | **100% LIVE_SOURCE** |
| Volume class | **sufficient** (≥50 target met; preferred 100+ met) |
| Days back | 14 |
| External actions | 0 |

## Funnel

| Stage | Count | % of retrieved |
|-------|------:|---------------:|
| Retrieved / parsed | 800 | 100% |
| Classified product (cheap+federal) | 136 | 17.0% |
| Hard rejected (service/construction/award) | 11 | 1.4% |
| Product held for action | 136 | — |
| Enriched through owner gate | 100 | — |
| READY_FOR_OWNER_APPROVAL | **0** | 0% of product |

### Blocker buckets (enriched products, rescored)

| Bucket | Count |
|--------|------:|
| identity_uom (incl. product identity) | 100 |
| enrich_capped (classified product, not fully enriched) | 36 |
| documents_tdp / deadline (explicit) | 0 |

All 100 enriched rows also carried economics, supplier/quote, financing, submission, and delivery UNKNOWN/incomplete blockers (multi-blocker). Primary bucket = identity first.

### Operator actions (rescored)

| Action | Count |
|--------|------:|
| HELD_FOR_ACTION | 689 |
| VERIFY_PRODUCT_IDENTITY | 100 |
| REJECT | 11 |
| READY_FOR_OWNER_APPROVAL | 0 |

### History / funding

| Signal | Result |
|--------|--------|
| Historical price found | 0 / 136 product (all NONE in this batch) |
| Funding SUPPORTED | 0 |
| Funding BLOCKED (enriched) | 100 |
| Funding UNKNOWN (capped/other) | 36 |

## False readiness / rejection

| Audit | Result |
|-------|--------|
| False READY | **0** (`docs/phase_g_ready_audit.md`) |
| False REJECT (audited 11/11) | **0** (`docs/phase_g_rejection_audit.md`) |

## Interpretation (reality over scorecard)

- Live SAM intake works at useful volume under budget.
- Listing-only federal product opportunities **correctly do not** become owner-READY.
- Deep package recovery, government history, and executable quotes were **not** completed in this supervised batch — so zero READY is expected, not padded.
- DIBBS direct remains unexercised (bot-blocked); DLA signal arrives via SAM titles/NSNs.

# Phase L.2.7 — Resilient Acquisition Pricing Audit

**Build:** `20260927-m3-phase-l27-resilient-acquisition-pricing`  
**Verdict:** `PHASE_L27_PARTIAL_RESILIENT_PRICING`  
**STOP:** No Phase M. No bids. No supplier contact. No financing. Verification gates unchanged.

## What changed

| Piece | Role |
|-------|------|
| `phase_l/resilient_fetch.py` | Connect/read timeouts, size cap, redirects, retries, fetch status taxonomy, domain circuit breaker |
| `phase_l/acquisition_pricing.py` | Direct source generation by family, PriceLead, acquisition ranges, preliminary bid window (RECON_ONLY), source learning, price memory |
| `phase_l/l27_rescue.py` | All Stage 3 candidates (81), economics-driven deep escalation hooks, failure taxonomy, manual queue |
| Bing/DDG | **Fallback only** (off by default in live run) |

Discovery ≠ retrieval ≠ verification ≠ usability are separate statuses.

## Live funnel (81 Stage 3)

| Metric | Result |
|--------|--------|
| Stage 3 candidates researched | **81** |
| Price searches | 81 |
| Pages attempted | 92 |
| Fetch successes | **78** |
| Bot blocks | 14 |
| Timeouts (fetch layer) | **0** (run completed; per-candidate wall timeout used) |
| Exact product pages | 0 |
| Price leads | 0 |
| Strong leads | 0 |
| Verified prices | 0 |
| Usable prices | 0 |
| History found | 14 |
| Both found | 0 |
| Apparent positive economics | 0 |
| Verified positive economics | 0 |
| ≥$10K | 0 |
| Manual fallback | 0 |

## Failure taxonomy (critical distinction)

| Class | Count | Meaning |
|-------|------:|---------|
| `NO_PRICE_INFORMATION` | 81 | Fetch often OK but no identity-tied price row / no snippet lead extracted |

Not collapsed solely into `CURRENT_PRICE_NOT_FOUND`. Circuit-breaker domains recorded separately.

## Circuit breaker / domain performance

**Skipped for run:** digikey.com, fastenal.com, ford.com, zoro.com (3× block each).

**High fetch-OK (search shells, no exact price row):** sourcewell-mn.gov, grainger.com, mscdirect.com, naspovaluepoint.org, mcmaster.com, mouser.com, bobcat.com.

## Infrastructure success vs price success

| Criterion | Status |
|-----------|--------|
| Live fetches no longer hang entire run | **Met** (81/81 finished ~2 min after timeout fix) |
| Pricing not primarily Bing/DDG | **Met** (direct family sources; Bing off) |
| Strong price leads preserved when blocked | **Not yet** (0 leads — search HTML lacked identity+price near tokens) |
| Alternate sources after bot block | **Met** (breaker + multi-domain targets) |
| Stage 3 acquisition ranges | **Not yet** (0 ranges) |
| Some verified usable prices | **Not met** (0) |
| Final verification strict | **Met** (READY_TO_BID=0; L.2.2 labels unchanged) |

## Remaining bottleneck

Direct distributor/OEM **search pages** fetch successfully but are shells without exact-model price rows. Next leverage: product-detail URL resolution from search results (bounded), public static PDF/XLSX catalogs, and stronger snippet/JSON-LD lead capture — without loosening identity verification.

## Tests

`tests/test_phase_l27_resilient_pricing.py` — timeout, breaker, 403/429, JS-empty, direct targets, PDF preference, price lead blocked≠eligible, ranges/bid window, learning, cache reuse, L.2.2 constants.

## Artifacts

- `artifacts/phase_l/l27_resilient_pricing.json`
- `artifacts/phase_l/l27_summary.json`
- `data/phase_l27_source_learning.json`
- `data/phase_l27_price_memory.json`

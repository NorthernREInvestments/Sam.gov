# Phase L.2.8 — Product-Detail Resolution + Bot-Resilient Price Recovery

**Build:** `20260927-m3-phase-l28-product-detail-resolution`  
**Verdict:** `PHASE_L28_PARTIAL_PRODUCT_DETAIL_RECOVERY`  
**STOP:** No Phase M. No bids. No supplier contact. No financing. L.2.2 verification unchanged.

## What changed

| Piece | Role |
|-------|------|
| `phase_l/product_detail_resolution.py` | Shell→ranked product links→depth≤3 detail/static; freshness; domain fallbacks; synthesize URLs; indexed snippet leads; sterile-shell recovery |
| `phase_l/resilient_fetch.py` | Soft-block detection (captcha / whoops / Just a moment) + sterile href shells |
| `phase_l/l28_rescue.py` | All Stage 3 (no row cap); deep-research all promising; freight not early kill; manual queue; ballpark economics; seller accessibility scores |
| Bing/DDG | Indexed **leads only** (UNVERIFIED / not economics-eligible) |

## Live funnel (81 Stage 3)

| Metric | Result |
|--------|--------|
| Stage 3 candidates / processed | **81 / 81** |
| Search shells observed | 52 |
| Candidate product links | **57** |
| Rows with detail candidates | 6 |
| Product-detail resolutions | 19 |
| Static artifacts | 0 |
| Blocked-domain events | 13 |
| Alternate-source attempts | 26 |
| Pages attempted | 79 |
| Fetch successes | 52 |
| Bot blocks | 17 |
| Timeouts | 3 (bounded) |
| Price leads | **14** |
| Strong leads | **14** |
| Rows with strong leads | 6 |
| Verified prices | **0** |
| Usable prices | **0** |
| History found (USAspending) | 14 |
| History+price joins | 0 |
| Apparent positive economics | 0 |
| Verified positive economics | 0 |
| Manual fallback queue | **6** |
| Deep escalations (all promising, no fixed cap) | **23** |
| Freight unresolved but Stage 3 kept | 13 |

## Failure taxonomy

| Class | Meaning |
|-------|---------|
| `NO_PRICE_INFORMATION` | Majority — mil-spec / sterile shells / no identity-tied public price |
| `STRONG_PRICE_LEAD_REQUIRES_VERIFICATION` | 6 — indexed/snippet leads preserved for Stage 3 + manual queue |

## Success criteria (diagnostic)

| Target | Status |
|--------|--------|
| Shells resolve toward product-detail candidates | Partial (synth + ranking; live HTML often captcha/sterile) |
| Bot-blocked → alternate recovery | **Met** (alternates + circuit breaker; no CAPTCHA bypass) |
| Strong exact-product price leads ≥15 | Near (14 strong leads) |
| Verified prices ≥8 | **Not met** (0) |
| Static artifacts usable | Not met (0) |
| Stage 3 processes every survivor | **Met** (81/81) |
| No unbounded hangs | **Met** |
| Final verification strict | **Met** (`ready_to_bid=false`) |

## Remaining bottleneck

Commercial OEM/distributor HTML is overwhelmingly captcha / soft-404 / JS shells under automated fetch. Indexed snippets now yield **UNVERIFIED** leads (e.g. ToolCat, F-150, DIBBS list), but exact product-page verification still fails without human or authorized static catalog access. Next leverage: richer public PDF/XLSX contract schedules and cooperative price books — without weakening L.2.2 gates.

## Tests

`tests/test_phase_l28_product_detail_resolution.py` + full L→L.2.8 suite: **147 passed**.

## Artifacts

- `artifacts/phase_l/l28_product_detail_resolution.json`
- `artifacts/phase_l/l28_summary.json`

## STOP

Phase M not started. No bids. No supplier outreach. No financing.

# Phase L.6 — Quote-Required Economics Design

**Build:** `20260927-m3-phase-l6-quote-required-economics`  
**Verdict:** `PHASE_L6_QUOTE_ECONOMICS_WORKING`

## Objective

`QUOTE_REQUIRED_COMMERCIAL` is a valid acquisition mode, not a dead end.

Pipeline:

**government-side value → allowable acquisition ceiling (max-buy) → supplier targets → quote target → economic decision**

Core owner question: *What is the maximum we can pay for this product and still make acceptable money?*

## Architecture

| Module | Role |
|--------|------|
| `phase_l/quote_economics.py` | Gov-value hierarchy, max-buy engine, quote bands/simulator, supplier enrich, unknown conversion, quote packets |
| `phase_l/l6_rescue.py` | Process **all** Stage 3 (no caps); priority queue; buyer/supplier memory |
| `scripts/run_phase_l6_quote_economics.py` | Live runner (`--refresh-hunt` optional) |

## Economic states

- `QUOTE_DEPENDENT_POSITIVE` — gov-side ceiling viable + suppliers exist; **supplier quote still required**
- `APPARENTLY_WITHIN_QUOTE_TARGET` — strong lead under max-buy (not verified)
- `PUBLIC_PRICE_WITHIN_TARGET` — verified public price under max-buy
- `MAX_BUY_PRICE_CALCULATED` — ceilings computed; not yet quote-dependent positive
- `INSUFFICIENT_EVIDENCE` — no usable gov-side value

Final `VERIFIED_POSITIVE` gate is **unchanged** (requires verified acquisition price).

## Operating constraints

- No supplier outreach / quote requests / financing / bids
- `CONTENT_ONLY_NO_SEND` quote packets
- Financing default ~5% of financed acquisition (configurable; not double-counted)
- Freight never silently $0
- Stage 3 / deep / manual queues: **no fixed caps**

# Phase L.14 — Regression

**Build:** `20260928-m3-phase-l14-nonbidnet-source-expansion`

## Must hold

- BidNet auth history parked; no login/CAPTCHA/account creation
- Canonical funnel only — no alternate live funnel
- Stage 3 / deep research: no fixed row caps
- Gov grades A–D strict; Gov D recon-only; no category-benchmark inflation
- Supplier grades A–D strict; generic seed remains D
- Resilient hunt: per-source wall clock, request timeout, retry, size cap, circuit breaker, checkpoint, exception isolation
- Fresh hunt terminal: `COMPLETE` or `COMPLETE_WITH_SOURCE_FAILURES` (never hang)
- Failed sources: preserve last-known inventory with freshness (no silent zeroing)
- Economic recompute on new history/qty/supplier/acquisition evidence
- Final verification / no-outreach flags unchanged
- Do not start Phase M

## Tests

`tests/test_phase_l14_nonbidnet.py` plus L→L.14 suite.

## Verdicts

- `PHASE_L14_NONBIDNET_EXPANSION_WORKING` — measurable non-BidNet commercial / history / Gov / quote improvement
- `PHASE_L14_PARTIAL_NONBIDNET_EXPANSION` — adapters/parked OK but weak useful yield
- `PHASE_L14_NONBIDNET_EXPANSION_FAILED` — park or pipeline broken

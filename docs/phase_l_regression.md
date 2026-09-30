# Phase L — Regression Report

## Observed failure / capability gap

Cross-government hunt lacked accessibility-before-pricing vocabulary and a unified Federal/State/Local/Cooperative ranking path.

## Root cause

Eligibility existed, but Phase L fields (`our_bid_access`, `competition_access_type`, retail+5% financing tiers, PRE_FILTERED competition) were not implemented as a shared gate.

## Smallest fix

`phase_l/` package + hunt script + thin owner chips/API. SAM live fallback **not** rewritten.

## Tests run

```bash
python -m pytest tests/test_phase_l_core.py tests/test_sam_live_fallback.py -q
```

**Result: 28 passed / 0 failed**

Coverage includes:

- Vehicle / BPA / sole-source → `NO`
- SB unknown → not `YES`
- Open market without SAM confirmation → not `YES`
- Registration vs runway blocker
- Local preference does not auto-reject
- PRE_FILTERED vs open offer ranking
- Retail baseline economics + tiers; discounts cannot fabricate PASS
- Normalize blocks READY without access YES
- Source level inference
- SAM fallback regressions (prior phase) still green

## Full suite

**Not run.** Changes are Phase L–scoped modules + thin API/UI injection; targeted coverage is sufficient per Phase L testing cost rules.

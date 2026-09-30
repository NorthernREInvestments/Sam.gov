# Phase L.3 — Regression

- No fixed Stage 3 / deep / manual caps
- Freight not early Stage 3 kill
- L.2.2 verification labels unchanged
- No supplier outreach / CAPTCHA bypass
- Specialty lanes not deleted — routed to specialty pipeline
- Fleet/equipment → quote-required (not UNKNOWN / not rejected)

## Test command

```
python -m pytest tests/test_phase_l_core.py tests/test_phase_l2_enrichment.py tests/test_phase_l21_market_rescue.py tests/test_phase_l22_product_resolution.py tests/test_phase_l23_commercial_identity.py tests/test_phase_l24_convergence.py tests/test_phase_l25_source_expansion.py tests/test_phase_l26_progressive_funnel.py tests/test_phase_l27_resilient_pricing.py tests/test_phase_l28_product_detail_resolution.py tests/test_phase_l29_maximum_source_coverage.py tests/test_phase_l3_commercial_rebalance.py -q
```

## Result

**192 passed** (L → L.3)

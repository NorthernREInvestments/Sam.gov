# Phase L.2.9 — Regression

- L.2.2 verification labels (`EXACT_VERIFIED` / `STRONG_VERIFIED`) unchanged  
- No CAPTCHA bypass introduced  
- Stage 3 / deep / manual queues have **no fixed opportunity-count caps**  
- Freight remains non-fatal at Stage 3 (`FREIGHT_NOT_YET_RESEARCHED`)  
- `ready_to_bid` remains false; Phase M not started  

Test command:

```
python -m pytest tests/test_phase_l_core.py tests/test_phase_l2_enrichment.py tests/test_phase_l21_market_rescue.py tests/test_phase_l22_product_resolution.py tests/test_phase_l23_commercial_identity.py tests/test_phase_l24_convergence.py tests/test_phase_l25_source_expansion.py tests/test_phase_l26_progressive_funnel.py tests/test_phase_l27_resilient_pricing.py tests/test_phase_l28_product_detail_resolution.py tests/test_phase_l29_maximum_source_coverage.py -q
```

# Phase L.2.1 — Failures

| Reason | Count | Action |
|---|---:|---|
| EXPIRED | 168 | Fixed — excluded from deep enrichment |
| UNKNOWN_QUANTITY | 82 | Still primary economics blocker; attachment qty recovery incomplete |
| MIXED_PRODUCT_SERVICE | 21 | Filtered from market budget |
| SERVICE | 16 | Filtered |
| NO_PUBLIC_PRICE_FOUND | 13 | After MPN/Bing/domain attempts |
| UNKNOWN_HISTORICAL_PRICE | 11 | USAspending miss / no NSN-MPN key |
| REPAIR_OVERHAUL | 7 | Filtered |
| UNKNOWN_MARKET_PRICE | 6 | Economics gate when retail missing |
| ENGINEERING_SUPPORT | 1 | Filtered |
| NEGATIVE_GROSS_RETAIL_SPREAD | 1 | Economics completed but fail |

## Search / extraction notes

- DDG often AUTH_REQUIRED → Bing used  
- OpenAI supplier path still daily-cap blocked (not required)  
- Off-topic Bing cite roots produced weak “prices” — domain allowlist added after live run  
- Military NSN-only items remain hard without authorized list prices  

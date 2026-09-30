# Phase L.2.1 — Live Report

**Artifact:** `artifacts/phase_l/l21_rescue_latest.json`  
**Verdict:** `PHASE_L21_PARTIAL_MARKET_PRICE_RECOVERY`

## Input / gates

| Metric | Count |
|---|---:|
| Access YES input | 482 |
| Deadline screened | 482 |
| Expired/stale removed | **168** |
| Non-resale (service/repair/engineering) removed | **45** |
| Live resale candidates | **269** |
| Exact/strong identity (among survivors) | 65 |
| Qty known (among survivors) | 22 |
| Deep selected | 100 |

## Research

| Metric | Count |
|---|---:|
| Historical attempted | 60 |
| Historical unit price found | 14 |
| Market attempted (rows) | 17 |
| Market page budget used | 104 |
| MPN query attempted | 4 |
| Public market prices found | **4** (was 0 in L.2) |
| Exact-price confidence hits | 4 |
| Economics completed | **1** |
| ≥$10K / $25K / $50K / $75K / $100K net | **0** |

## Price-source distribution (selected)

| Seller type | Count |
|---|---:|
| UNKNOWN | 3 |
| RETAILER | 1 |

## Failure reasons (top)

1. EXPIRED — 168  
2. UNKNOWN_QUANTITY — 82  
3. MIXED_PRODUCT_SERVICE — 21  
4. SERVICE — 16  
5. NO_PUBLIC_PRICE_FOUND — 13  
6. UNKNOWN_HISTORICAL_PRICE — 11  
7. REPAIR_OVERHAUL — 7  
8. NEGATIVE_GROSS_RETAIL_SPREAD — 1  

## What improved vs L.2

- Expired rows no longer consume deep budget  
- Service/repair filtered before market spend  
- MPN-first query chain + Bing fallback (DDG 403 no longer terminal alone)  
- `ACCESS_OK` fetch bug fixed (`"OK"` vs `"ACCESS_OK"`)  
- Deterministic JSON-LD / `$` extraction  
- `submission_readiness` no longer claims READY / READY_TO_BID  
- Pre-bid compliance model blocks READY_TO_BID  

## Remaining gap

Many Bing cite landings were off-topic roots; post-run domain allowlist tightened further. Commercial ToolCat/ASUS-style rows in the live pool often lack MPN extractability and qty. Owner ≥$10K queue still empty.

## Owner profit queue

Empty (0).

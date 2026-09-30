# Phase L.2.5 — Source Expansion Audit

**Build:** `20260927-m3-phase-l25-source-expansion`  
**Verdict:** `PHASE_L25_PARTIAL_SOURCE_EXPANSION`  
**STOP:** No Phase M. No bids. No supplier contact. No purchases. No financing.

## What shipped

| Module | Role |
|--------|------|
| `phase_l/pricing_sources.py` | `PricingSourceAdapter` model + HTML bid-tab, council/board, PDF row, XLSX/CSV parsers; identity-gated prices |
| `phase_l/procurement_intel.py` | Expanded history/current query strategies, direct portal URLs (Sourcewell/OMNIA/OEM/dealer), merge into history/market |
| `phase_l/recurring_buy.py` | `RecurringProductWatch` + buyer watchlist aggregation |
| `phase_l/l25_rescue.py` | Revisit 23 → expand toward ~100; source telemetry; funnel math; queues |
| `tests/test_phase_l25_source_expansion.py` | Bid tab, council, PDF, spreadsheet, coop access, OEM/dealer, gov-only reject, recurring, profit queue |

Verification gates from L.2.2–L.2.4 remain strict (no junk HTML prices; gov-only/coop not usable as acquisition).

## Original 23 convergence candidates

| # | History $ | Current $ | State | Title (truncated) | History source |
|---|----------:|----------:|-------|------------------|----------------|
| 1 | — | — | NEITHER | VALVE ASSEMBLY … 1266M27P09 | USASPENDING |
| 2 | 922.00 | — | HISTORY_ONLY | Spraybar 37D401750P104 | USASPENDING |
| 3 | — | — | NEITHER | Mid-Size Sport Utility Vehicle | USASPENDING |
| 4 | — | — | NEITHER | 2027 Ford F-150 Police Responder | USASPENDING |
| 5 | — | — | NEITHER | Police Electric LSV | USASPENDING |
| 6 | — | — | NEITHER | Long Reach Tracked Excavator | USASPENDING |
| 7 | — | — | NEITHER | New Midsize SUV | USASPENDING |
| 8 | — | — | IDENTITY_CONFLICT | Ford Expedition SSV | USASPENDING |
| 9 | — | — | NEITHER | Bobcat ToolCat UW56 | USASPENDING |
| 10 | — | — | NEITHER | Shuttle Bus Vehicles | USASPENDING |
| 11 | — | — | NEITHER | Sources sought (58--) | USASPENDING |
| 12 | 5937.18 | — | HISTORY_ONLY | Switch Pressure F110 … 1274M99P01 | USASPENDING |
| 13–23 | — | — | NEITHER | Mixed NSN/vehicle/equipment | mostly USASPENDING / none |

**Original 23 totals:** history found **2**, current usable **0**, both found **0**, positive economics **0**.

No new history/current hits on the original 23 from bid-tab/PDF/coop/dealer adapters in this live pass (`new_history_hit=False`, `new_current_hit=False` for all 23).

## Source types tried (live telemetry)

| Source | Searched | Results | Exact ID hits | Hist $ | Current $ | Usable acq | Blocked | Parse fail | Bot fail |
|--------|----------|---------|---------------|--------|-----------|------------|---------|------------|----------|
| USAspending | 31 | 21 | 14 | 14 | 0 | 0 | 0 | 0 | 0 |
| bid tabs | 23 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| cooperative | 23 | 0 | 0 | 0 | 0 | 0 | 0 | **66** | 0 |
| board/council | 23 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| PDFs | 23 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| spreadsheets | 23 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| OEM | 23 | 3 | 0 | 0 | 0 | 0 | 0 | 5 | **10** |
| dealer | 23 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| distributor | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 20 | 0 |
| local portals | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Successful source recoveries (≥10 hist; current still 0)

Live recoveries in this run are **USAspending exact-identity history** on the broader pool (14), including the same 2 from the original 23:

1. Spraybar PN 37D401750P104 — $922  
2. Switch Pressure F110 PN 1274M99P01 — $5,937.18  
3–14. Additional NSN/aerospace rows on expanded worklist (SPRTA1 / FD2020 family) — unit prices $462–$130,790  

Unit/fixture-level recoveries proven in tests (not live network):

- State HTML bid tab (ToolCat UW56 awarded row)  
- Local award table (Ford F-150 Police Responder)  
- Council resolution window extraction  
- PDF exact-row price  
- Spreadsheet exact-row contract/MSRP  
- Coop members-only gated as non-acquisition  
- OEM/dealer HTML identity+price path  
- Gov-only rejected as acquisition  

## Failures (10)

1. **Bobcat ToolCat UW56** — commercial model; no bid-tab/PDF/dealer price recovered live  
2. **Ford F-150 Police Responder** — same  
3. **Ford Expedition SSV** — identity conflict; no usable join  
4. **Mid-size SUV / excavator / shuttle bus / crane truck** — vehicle/equipment; history+current miss  
5. **Sourcewell/OMNIA portal search pages** — fetched but no exact-model price rows (66 coop parse failures)  
6. **OEM site fetches** — 10 bot failures  
7. **Bing live search** — frequently `SEARCH_PROVIDER_FAILED` (bot/empty); DDG skipped after stalls  
8. **PDF hits without identity** — 5 PDF results, 0 exact identity price rows  
9. **Dealer inventory** — searched; 0 exact advertised new-unit prices tied to model  
10. **Current usable acquisition** — 0 across all 78 researched (still primary bottleneck)

## Broader pool

- Fully researched: **78** (23 primary + 55 expanded; target was ≥100 if available)  
- Complete economics: **0**  
- Profitable / ≥$10K: **0**  
- History found (all): **14**  
- Current usable: **0**  
- Both found: **0**

## Recurring-buy intelligence

- Products tracked: **2**  
- Repeat-buy candidates (3+ buys): **0**  
- Buyer watchlist entries: **1**

## Funnel math (toward 80–100 profitable)

| Stage | N |
|-------|---|
| Live access YES input | 482 |
| Fully researched | 78 |
| History found | 14 |
| Current usable | 0 |
| Both found | 0 |
| Complete economics | 0 |
| Profitable | 0 |
| Projected live for 100 profitable | n/a (0 profitable) |

`profitable_bid_accessible_rate` = **0.0** (0 / 78).

## Remaining bottleneck

1. **Live discovery of award PDFs / bid tabs / dealer pages** — search providers bot-blocked; constructed coop portals return search shells without exact-row prices.  
2. **CURRENT_PRICE_NOT_FOUND** remains universal on this pool.  
3. **FREIGHT_UNRESOLVED** still blocks final economics for vehicles/heavy equipment even when prices eventually converge.

## Tests

Full L → L.2.5 suite: **104 passed**.

## Artifacts

- `artifacts/phase_l/l25_source_expansion.json`  
- `artifacts/phase_l/l25_summary.json`  
- `scripts/run_phase_l25_source_expansion.py`

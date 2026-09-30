# Phase L.2.4 — Live Report

**Artifact:** `artifacts/phase_l/l24_convergence_latest.json`  
**Verdict:** `PHASE_L24_PARTIAL_PRICE_HISTORY_CONVERGENCE`

## Summary

All **23** L.2.3 commercially researchable candidates were fully dual-branched (history + current price) with explicit convergence states. The join engine works; live co-recovery remains rare.

| Metric | Count | Target |
|---|---:|---:|
| Researched | **23** | 23 |
| History found | **2** | ≥10 |
| Current usable price found | **0** | ≥10 |
| Both found | **0** | ≥8 |
| Economics completed | **0** | ≥8 |
| Positive unit spreads | 0 | — |
| Positive total profits | 0 | — |
| ≥$10K | 0 | — |

History attempts used **63** USAspending approach calls (≥3 approaches/candidate where possible). Market used **106** page fetches / **15** product pages; L.2.2 verification kept current usable prices at 0 this run (Ford L.2.3 `$56,150` hit did not reappear as verified/accessible).

## Convergence states

| State | Count |
|---|---:|
| `NEITHER_FOUND` | 20 |
| `HISTORY_ONLY` | 2 |
| `IDENTITY_CONFLICT` | 1 |
| `BOTH_FOUND_*` | 0 |
| `CURRENT_PRICE_ONLY` | 0 |

## Candidate table (all 23)

| # | Product | History | Current Price | Price Access | Unit Spread | Total Net | Status |
|---:|---|---:|---:|---|---:|---:|---|
| 1 | VALVE ASSEMBLY,ANTI NSN 2995013130343  PN 1266M27P09 | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 2 | NSN: 2915-00-909-9119 P/N: 37D401750P104 Noun: Spray | 922.0 | — | UNKNOWN | — | — | HISTORY_ONLY |
| 3 | Mid-Size Sport Utility Vehicle (Qty. 2) | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 4 | One or More 2027 Ford F-150 Police Responder(s) or E | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 5 | Police Electric Selective Four Wheel Drive Low Speed | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 6 | (FY27) DL Long Reach Tracked Excavator 96K lbs | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 7 | 1 - New Midsize Sport Utility Vehicle | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 8 | One or More 2027 Ford Expedition SSV Vehicle(s) for  | — | — | UNKNOWN | — | — | IDENTITY_CONFLICT |
| 9 | Brand Name Bobcat ToolCat UW56 Utility Work Machine | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 10 | Purchase of One (1) 14-Passenger Shuttle Bus Vehicle | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 11 | 58--Government intends to procure, and is still seek | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 12 | Switch Pressure F110 NSN 5930012154689PR_PN_1274M99P | 5937.18 | — | UNKNOWN | — | — | HISTORY_ONLY |
| 13 | Blade Burn Pod Set [WSDC: 40A] End Item: Helicopter, | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 14 | NOM: Speed Sensor, Turbin; WSDC: [DUNCOP END ITEM: CO | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 15 | PN 355 - Guardrail Installed at Three Locations | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 16 | LINER,COMBUSTION CH_End_Item_F108_NSN_2840013443232P | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 17 | CASE AND NOZZLE ASS_End_Item_F110_NSN_2840012403588P | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 18 | AUFMENTER FUEL CONTROL_End_Item_F101_NSN_29150114821 | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 19 | Transmission, Mechan_End_Item_B-1B_NSN_1680011414918 | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 20 | NOZZLE SEGMENT, TURB_End_Item_F100_NSN_2840014559142 | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 21 | Spares for AH-64 Apache of NOUN: Insulation, Thermal | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 22 | F-16 Radar Antenna Phase Shift Driver Unit NSN: 1270 | — | — | UNKNOWN | — | — | NEITHER_FOUND |
| 23 | (FY27) DL T83 Bridge Inspection Crane Truck (1) | — | — | UNKNOWN | — | — | NEITHER_FOUND |

## Queues

All 23 → `RESEARCH_INCOMPLETE`. Profit queues empty.

## profitable_accessible_rate

23 researched · 0 positive net · 0 ≥$10K · **rate = 0.0**

## Primary failure causes

1. `CURRENT_PRICE_NOT_FOUND` (23) — verified accessible retail/dealer pages scarce; bot blocks / identity mismatches  
2. `HISTORY_NOT_FOUND` (21) — USAspending rarely returns commercial model awards (Ford/Bobcat)  
3. `FREIGHT_UNRESOLVED` (11) — heavy equipment/vehicles correctly blocked from final net  
4. `QUANTITY_UNKNOWN` (22)

## Remaining bottleneck

Convergence **instrumentation** works. Live **co-occurrence** of usable history + accessible verified current price on the same SKU does not. Next leverage: state/local bid-tabs for vehicles/equipment history, and stable OEM/dealer product-page acquisition (not SERP luck) — without weakening L.2.2 gates.

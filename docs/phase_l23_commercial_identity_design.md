# Phase L.2.3 — Commercial Identity Recovery Design

**Build:** `20260926-m3-phase-l23-commercial-identity-recovery`  
**Depends on:** L.2.2 verified product-page pricing (unchanged)

## Problem

L.2.2 made pricing safe but the funnel was too narrow: only ~11 “commercially priceable exact/strong” candidates. Products like **Bobcat ToolCat UW56** stayed Phase J `IDENTITY_PARTIAL` because they lack an MPN field.

## Separation of concerns

| Concept | Meaning |
|---|---|
| Commercial identity | Manufacturer + exact model/trim/SKU (may lack MPN) |
| Configuration completeness | RAM/CPU/options — tracked separately |
| `market_research_eligible` | May search for prices |
| L.2.2 `economics_eligible` | Verified product-page price only |
| Bid readiness | Still `ready_to_bid=False` |

Exact commercial model + partial configuration → **research allowed**, economics still gated by L.2.2 verification (+ qty for totals).

## Module

`phase_l/commercial_identity.py`

- States: `EXACT_COMMERCIAL_MODEL`, `EXACT_VEHICLE_TRIM`, `EXACT_EQUIPMENT_MODEL`, `EXACT_MPN`, …
- Priceability: `HIGH` / `MEDIUM` / `LOW` / `NONCOMMERCIAL`
- OEM recovery maps (ToolCat→Bobcat, PowerEdge→Dell, …)
- Careful model normalization (UW-56↔UW56; never R660↔R670)
- `compute_unit_spread` without quantity → `TOTAL_PROFIT_UNKNOWN_QUANTITY`

## Orchestration

`phase_l/l23_rescue.py` — commercial-first buckets (A model / B MPN / C strong), parallel history + market, audit samples.

## Chain (unchanged safety)

`commercial identity recovered` → `market research` → `product URL` → `L.2.2 verify` → `price` → economics

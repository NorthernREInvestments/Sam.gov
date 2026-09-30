# Phase L.2.6 — Progressive Funnel Audit

**Build:** `20260927-m3-phase-l26-progressive-funnel`  
**Verdict:** `PHASE_L26_PROGRESSIVE_FUNNEL_RESTORED`  
**STOP:** No Phase M. No bids. No supplier contact. No financing. `ready_to_bid=0`.

## Architecture restored

| Stage | Question | Cost |
|-------|----------|------|
| 0 Broad discovery | Worth keeping at all? | ~0 |
| 1 Cheap triage | KEEP / PROMISING / LOW / HARD_REJECT | Very cheap |
| 2 Identity | Any useful anchor (NSN/MPN/model/…)? PARTIAL OK | Cheap |
| 3 Economic recon | Promising enough to spend more? Ranges OK | Moderate |
| 4 Deep research | Only HIGH/MEDIUM | Expensive |
| 5 Final economics | Verified net only | Expensive |
| 6 Pre-bid | Strict READY_TO_BID unchanged | Expensive |

## Before / after vs L.2.4–L.2.5

| Metric | L.2.4/L.2.5 | L.2.6 |
|--------|-------------|-------|
| Raw live access YES | 482 | 482 |
| Researchable / Stage 3 | **23** (hard `market_research_eligible`) | **81** |
| Expansion ratio | 1.0× | **3.52×** |
| Stage 1 KEEP/PROMISING/LOW | n/a | 284 broad candidates |
| Deep-research queue | ~23 (all “eligible” spent equally) | 23 prioritized MEDIUM/HIGH from 81 |
| Apparent promising economics | 0 | 2 |
| Verified positive economics | 0 | 0 |
| ≥$10K verified | 0 | 0 |
| READY_TO_BID | 0 | **0** (strict gate held) |

## Research cost distribution (relative units)

| Stage | Units spent |
|-------|-------------|
| 0 | 0 |
| 1 | 24 |
| 2 | 609 |
| 3 | 405 |
| 4+ live | skipped in final pass (`--no-live`; live deep hung on market HTTP) |

Deep spend concentrated on 23 HIGH/MEDIUM rows only (58 Stage-3 rows stay in `ECONOMIC_RECON_QUEUE` without deep burn).

## Queues

| Queue | Count |
|-------|------:|
| BROAD_CANDIDATES | 284 |
| ECONOMIC_RECON_QUEUE | 58 |
| PROMISING_DEEP_RESEARCH | 23 |
| DEEP_RESEARCH_IN_PROGRESS | 23 |
| PROFITABLE_NEEDS_COMPLIANCE | 0 |
| BID_CANDIDATE | 0 |

## 20 rows that previously died early but now reach Stage 3

These lacked L.2.3 `market_research_eligible` (would be dropped by `_collect_eligible`) but have NSN / strong description anchors — enough to search intelligently.

| # | Title (truncated) | Missing | Why still researchable | Approx economic signal | Deeper survival |
|---|-------------------|---------|------------------------|------------------------|-----------------|
| 1 | Environmental Control Unit F100-60 (NSN…) | qty, l23 eligible | NSN + description | none yet | DEEP_LOW — needs hist/price |
| 2 | Microphone, Magnetic | qty, l23 | NSN | none | DEEP_LOW |
| 3 | LATCH, DOOR, VEHICULA 2540-01-375-7995 | qty, l23 | NSN | none | DEEP_LOW |
| 4 | Pump subassembly axial piston hydraulic | qty, l23 | NSN | none | DEEP_LOW |
| 5 | SPRTA126F0222 (NSN: 2840016486877) | l23 | NSN + description | none | DEEP_LOW |
| 6 | SPRTA1-26-F-0068 (NSN: 2840-01-599-4493) | l23 | NSN | none | DEEP_LOW |
| 7 | SPRTA1-24-C-0066 (NSN: 2840-01-448-7511) | l23 | NSN | none | DEEP_LOW |
| 8 | SPRTA1-26-C-0011 (NSN:2840-01-448-7511) | l23 | NSN | none | DEEP_LOW |
| 9 | SPRTA1-25-F-0255 (NSN: 2840014434069) | l23 | NSN | none | DEEP_LOW |
| 10 | SPRTA125R00168 (NSN:2915-01-310-2141) | l23 | NSN | none | DEEP_LOW |
| 11 | NSN: 1660013310068 HEAT EXCHANGER | qty, l23 | NSN | none | DEEP_LOW |
| 12 | NSN: 2915007821759 SPRAYBAR | qty, l23 | NSN | none | DEEP_LOW |
| 13 | RETAINER, RING, AIRCRAFT | qty, l23 | NSN | none | DEEP_LOW |
| 14 | SPRTA1-26-F-0074 (NSN: 6150-00-725-4952) | l23 | NSN | none | DEEP_LOW |
| 15 | SPRTA1-26-C-0021 (NSN: 2840-01-448-7511) | l23 | NSN | none | DEEP_LOW |
| 16 | NSN: 5998-01-253-7978 CIRCUIT CARD | qty, l23 | NSN | none | DEEP_LOW |
| 17 | NSN:1650-00-404-0445 VALVE,LINE | l23 | NSN | none | DEEP_LOW |
| 18 | Market Survey NSN 1560-01-662-03… | qty, l23 | NSN | none | DEEP_LOW |
| 19 | Lever, Remote Control NSN 1680-01-264-2104 | qty, l23 | NSN | none | DEEP_LOW |
| 20 | AIRCRAFT EAGLE F-15 SCREW ACTUATOR | qty, l23 | NSN | none | DEEP_LOW |

**Also kept (were L.2.4 eligible) with incomplete fields — not discarded:**

- Bobcat ToolCat UW56 — freight/config often unresolved → `FREIGHT_REQUIRED` / `CONFIGURATION_RESEARCH_REQUIRED` labels, still deep-queued  
- Ford F-150 Police Responder / Expedition SSV — vehicle trim identity; qty/freight missing does not kill Stage 3  
- Spraybar / F110 switch — history known from prior phases; qty missing → `QUANTITY_REQUIRED_FOR_TOTAL` path available when unit prices exist  

## Regression safety

- Services / construction / expired → Stage 0–1 HARD_REJECT (24 hard rejects this run)
- Approx evidence ranks but `can_finalize_economics=False`
- Stage 6: `ready_to_bid` remains false without full prebid package
- No auto-bid / Phase M

## Remaining bottleneck

Stage 3 is broad again; **verified acquisition prices** still scarce (live market HTTP stalls; `--no-live` deep used cache only). Next spend should stay on the 23 deep queue + improve L.2.5 source hits — not re-narrow Stage 2/3.

## Tests

`tests/test_phase_l26_progressive_funnel.py` + prior L suite — run at end of phase.

## Artifacts

- `artifacts/phase_l/l26_progressive_funnel.json`
- `artifacts/phase_l/l26_summary.json`
- `scripts/run_phase_l26_progressive_funnel.py`
- `phase_l/progressive_funnel.py`, `phase_l/l26_rescue.py`

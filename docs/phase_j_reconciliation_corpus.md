# Phase J — Reconciliation Corpus

**Size:** 30 cases from Phase I deep-research near misses (priority: history/identity mismatches first).  
**Source:** `artifacts/phase_i/hunt_latest.json`  
**Method:** Offline re-reconcile with Phase J identity + history rules (no new 2k scan).

## Failure-mode coverage

| Mode | Included |
|------|----------|
| Exact NSN + weak/stale history | Yes (e.g. diesel, transmission kit, pipefitter toolkit, turbine support) |
| Strong history + unknown identity | Yes (wheel assemblies) — noun history **rejected** |
| Truncated DLA/SAM title | Yes (`25--WHEEL…`) |
| NSN + MPN present, no history | Yes |
| Exact NSN, no USAspending hit | Yes |
| False-match risk (wheel noun ≠ NSN) | Yes — 8 awards rejected per wheel case |

Expected matches are only asserted when award description contains the **same NSN**. No invented joins.

## Representative cases

### Wheel Assembly, Pneumatic Tire (`7c1d8f0b72f740ae823e25f72db3f32a`)

- Current: title only; NSN missing  
- Prior logic: STRONG_HISTORY via “WHEEL ASSEMBLY” keyword (Meggitt MH-60 awards)  
- Expected: **MATCH_REJECTED** / history WEAK — no shared identity key  
- Observed after Phase J: rejected_awards=8; not quote-ready  

### Engine Diesel NSN `2815-01-536-9262`

- Current: exact NSN (+ P/N in history)  
- Prior: WEAK_HISTORY (age >5y hard cut)  
- History: 2020 award with exact NSN + QTY 38 EA  
- Expected: AGED + COMPARABLE if unit/lot usable  
- Observed: MODERATE_HISTORY → **READY_FOR_QUOTE_OUTREACH**  

### Transmission Kit NSN `2520-01-682-2226`

- Exact NSN in 2020 award (QTY 100)  
- Prior WEAK → after COMPARABLE → quote-ready  

### Pipefitter Toolkit NSN `5180-00-596-1509`

- Exact NSN (compact form in award)  
- Prior WEAK → after COMPARABLE → quote-ready  

### Turbine Support NSN `2840-00-411-8852` P/N `6870409`

- Exact NSN FMS award 2019 (25 EA)  
- Prior WEAK → after COMPARABLE → quote-ready  

### TEST SET Fuel Contr NSN `4920-01-592-7332`

- Exact NSN; no awards in packet  
- Expected: still NO_HISTORY unless USAspending returns hits  
- Observed: remains not quote-ready  

Full machine-readable rows: `artifacts/phase_j/corpus_cases.json`.

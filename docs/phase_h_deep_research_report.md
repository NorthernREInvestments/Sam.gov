# Phase H — Deep Research Report

**Run:** 2026-09-25  
**Cohort:** 25 LIVE_SOURCE (from Phase G; no new 800 scrape)  
**Artifact:** `artifacts/phase_h/deep_research_latest.json`

## Volume

| Metric | Value |
|--------|------:|
| Selected | 25 |
| Deep-researched | 25 |
| Docs reviewed (SAM noticedesc) | 25 |
| Public HTTP | 50 |
| USAspending calls | 6 |
| OpenAI | 0 (offline) |
| External actions / outreach | 0 |

## Outcomes

| State | Count |
|-------|------:|
| READY_FOR_QUOTE_OUTREACH | **1** |
| READY_FOR_BID_DECISION | **0** |
| RESEARCHED_NOT_READY | 24 |
| EXPIRED / REJECTED / BLOCKED | 0 / 0 / 0 |

## Evidence funnel

| Signal | Count |
|--------|------:|
| Identity STRONG_MATCH | 2 |
| Identity UNKNOWN | 23 |
| History STRONG/MODERATE | 2 / 1 |
| History NONE | 22 |
| Economics promising (quote required) | 1 |
| Economics blocked unknown revenue | 22 |
| Economics blocked unknown cost | 2 |
| Funding FINANCEABLE_IN_PRINCIPLE_BUT_UNPROVEN | 25 |

## Quote-ready live case

### BOAST RFOP — Panel, Power Distribution — NSN 6110-01-082-8958

| Field | Value |
|-------|-------|
| Canonical ID | `a907d7a2e92c4db898b353d91ed00b3c` |
| Solicitation | PANDTA-26-P-0000_029863 |
| Agency | ACC-DTA / Dept of the Army |
| Provenance | LIVE_SOURCE |
| Identity | STRONG_MATCH (NSN in title + noticedesc) |
| History | MODERATE_HISTORY — USAspending award `SPRDL118P0015`, awardee LCL ELECTRONICS INC |
| Revenue basis | ~$95,992.82 (USAspending amount; not fabricated) |
| Max supplier cost | **≤ $85,992.82** (for ~$10k profit floor) |
| Public market price | UNKNOWN / QUOTE_REQUIRED |
| Funding | FINANCEABLE_IN_PRINCIPLE_BUT_UNPROVEN |
| Docs | noticedesc retrieved (attachments/TDP not fully acquired) |
| Next action | Request pricing from authorized distributors for NSN 6110-01-082-8958; delivered cost ≤ $85,992.82. **No outreach sent.** |

## Why most of the cohort did not become quote-ready

1. **No NSN-matched government history** (22/25) → cannot back-solve max supplier cost without inventing revenue.  
2. **DLA truncated titles** (`12--PARTS KIT…`) → identity stays UNKNOWN without attachments.  
3. **Wheel assembly history ~$878** → even with history, economics cannot clear $10k profit floor (too thin).  
4. **Fuel test set NSN** found identity but **no USAspending match** → blocked on unknown revenue.  
5. **DIBBS package / drawings** not available via public noticedesc alone.

## Cost

| Item | Approx |
|------|--------|
| HTTP | 50 |
| USAspending | 6 |
| LLM | 0 |
| Est. paid LLM | $0 |

## Reality verdict

Deep research path works enough to produce **one** evidence-backed quote-outreach candidate. It does **not** yet reliably produce ≥3 quote-ready or any bid-decision-ready deal from listing+noticedesc+USAspending alone. Missing attachments/TDP and sparse NSN history dominate.

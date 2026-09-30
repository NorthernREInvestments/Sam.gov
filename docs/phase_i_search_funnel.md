# Phase I — Search Funnel

**Date:** 2026-09-25  
**Mode:** DEVELOPMENT_NO_OUTREACH

## Stage A — Broad live scan

| Source | Count |
|--------|------:|
| Phase G seed (LIVE_SOURCE) | 800 |
| SAM expand (broad windows) | 0–1209 unique new depending on run (checkpoint often saturated) |
| SAM product-keyword pulls | ~739–780 |
| **Peak unique inventory** | **2588** (run v1) |
| Final authoritative run inventory | 1501 (seed + keywords; expand unique_new=0) |

## Stage B — Product + eligibility survivors

| Metric | Final run |
|--------|----------:|
| Product-class LIKELY_PRODUCT_RESALE | 272 |
| Eligibility NOT_APPLICABLE | 271 |
| Eligibility NOT_CURRENTLY_ELIGIBLE (blocked early) | 3 |
| Stage B cap survivors | 200 |

Early rejects (final): not_product 1182 · service/repair/construction 44 · food 1 · eligibility blocked 3.

## Stage C — Research candidates

| Metric | Final |
|--------|------:|
| Scored top candidates | 80 |
| Prefer | NSN/MPN, DLA, product noun, no vehicle lock |
| Deprioritize | Repair-of, New Manufactured Material, TDP/FAT/SAR language |

## Stage D — Deep research

| Metric | Final |
|--------|------:|
| Deep researched | **80** |
| Quote-ready | **0** |
| Bid-ready | **0** |
| Stop | `deep_queue_exhausted_no_quote_ready` |

Across Phase I runs: 12 (v1, pre-audit) + 50 (v2) + 80 (v3) deep attempts; v1’s 3 quote-ready were **demoted** after manual audit (repair + ancient OEM history).

## Bottleneck (precise)

1. **Recent comparable USAspending history** is rare for live NSN solicitations → most STRONG_MATCH cases have NO/WEAK history.  
2. **Identity** missing on history-rich wheel assemblies (FSC--NOUN titles; noticedesc often lacks extractable NSN / DIBBS-gated).  
3. **Ancient awards (>5y)** correctly downgraded to WEAK — cannot justify quote outreach.  
4. **SAM broad expand** often returns `unique_new=0` (checkpoint saturation) — keyword hunt still works.  
5. **DIBBS bot limits** unchanged — no CAPTCHA bypass.

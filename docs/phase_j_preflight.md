# Phase J — Preflight

**Date:** 2026-09-25  
**Mode:** DEVELOPMENT_NO_OUTREACH — reconciliation only. No broad 2,000+ rescan. No outreach/bids/financing/Phase K.  
**Start from:** Phase I deep corpus (`artifacts/phase_i/hunt_latest.json`).

---

## Phase I bottleneck (verified)

Recent comparable government price history and strong current product identity **rarely co-occur**.

| Failure mode | Phase I final (n=80) | Notes |
|--------------|---------------------:|-------|
| Strong identity + no history | 21 | Exact NSN in title; USAspending returned empty |
| Strong identity + weak/stale history | 10 | Exact NSN awards exist but age >5y forced WEAK |
| Strong history + unknown identity | 4 | FSC--NOUN / “Wheel Assembly…”; keyword history may be **wrong item** |
| Unknown identity + no history | 45 | Truncated DLA titles; no NSN/MPN extracted |

Quote-ready after audit: **0**. False actionability retained: **0**.

---

## Current lookup paths

| Step | Module / behavior |
|------|-------------------|
| Identity | `phase_h.deep_research._identity_confidence` — NSN from title/desc only → STRONG/UNKNOWN |
| History | `_lookup_history` → `usaspending_client.fetch_awards_by_keywords` |
| NSN path | Keywords = NSN + compact NSN |
| No-NSN path | Title regex for TEST SET / WHEEL ASSEMBLY / etc. — **false-match risk** |
| History class | Recent ≤5y priced awards → MODERATE/STRONG; older → WEAK (blocks quote) |
| Economics | Median award_amount as revenue; no UOM/qty confirmation |
| Quote gate | Needs MODERATE/STRONG history + STRONG identity |

---

## Current normalization / confidence gaps

1. **No ProductHistoryReconciliation object** — awards accepted without field-level match/reject.  
2. **No UOM/qty unit-price reliability** — total award ÷ unknown qty never validated.  
3. **Recency binary** — >5y always WEAK; Phase J needs CURRENT/RECENT/AGED/STALE with exact-NSN AGED still usable as comparable anchor.  
4. **Wheel false-match risk** — “WHEEL ASSEMBLY” keyword pulled Meggitt MH-60 awards without shared NSN.  
5. **MPN/manufacturer not joined** — P/N in title unused for history search.  
6. **Title truncation** — `25--WHEEL ASSEMBLY,PNEUMAT` not expanded; noticedesc often empty of NSN.  
7. **Diesel NSN 2815-01-536-9262** — award descriptions contain exact NSN + QTY but classed WEAK due to age (~6y).

---

## Proposed smallest fixes (map to observed failures)

| Observed failure | Root cause | Smallest fix |
|------------------|------------|--------------|
| Wheel STRONG_HISTORY + UNKNOWN id | Noun-keyword history without NSN | Reject history without shared identity key; no bare-noun keyword search |
| Exact NSN + AGED awards → WEAK only | 5y hard cut | Recency bands; exact-NSN AGED can be COMPARABLE if unit price usable |
| No UOM/qty on economics | Award total treated as revenue | Parse QTY/UOM from award description; unit-price confidence gate |
| MPN unused | Lookup NSN-only | Add MPN (+ optional mfr) keyword search when NSN fails |
| Truncated titles | No nomenclature normalize | Deterministic ASSY/PNL/… normalize for support only |
| No reconciliation provenance | Flat awards list | `ProductHistoryReconciliation` result per candidate |

**Out of scope:** new 2k SAM scan, financing, dashboard, post-award, DIBBS CAPTCHA bypass.

---

## Success signal

Re-run Phase I near-miss corpus with reconciliation. Prefer converting exact-NSN + AGED usable history into quote-ready **only** when identity + UOM + comparability pass. Do not force promotions.

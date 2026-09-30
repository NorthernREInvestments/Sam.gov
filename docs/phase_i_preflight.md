# Phase I — Preflight

**Date:** 2026-09-25  
**Mode:** `DEVELOPMENT_NO_OUTREACH` — hunt + research only. No supplier email, CO contact, bids, financing apps, vehicle enrollment, JCP, purchase, or Phase J.  
**Business objective:** Find Brian at least one real, current, executable **product-resale** opportunity worth pursuing.

---

## Starting state (verified)

| Item | Value |
|------|------:|
| Phase H classification | LIMITED DEEP LIVE DEAL READY |
| Eligibility P0 | Fixed (BOAST → `ELIGIBILITY_ACTION_REQUIRED`) |
| Phase H quote-ready (post-gate) | 0 |
| Phase H bid-ready | 0 |
| Full suite | 1775 / 0 |

M3 can safely reject/block. Phase I asks: **can it find a deal?**

---

## Inputs to reuse

| Path | Use |
|------|-----|
| `artifacts/phase_g/live_run_latest.json` | Seed inventory (800 LIVE_SOURCE) |
| `eligibility_gate.py` + company profile | Hard eligibility before deep spend |
| `phase_h.deep_research.research_one_phase_h` | Deep path (noticedesc, history, economics) |
| `phase_g.live_batch.fetch_live_sam_batch` / federal SAM ingest | Expand live volume |
| `discovery.dla_product_extract` / `national_discovery_funnel.stage1_ultra_cheap` | Cheap product screen |

---

## Hunt plan

| Stage | Target | Action |
|-------|--------|--------|
| A | 1,000–2,000+ unique live notices | Seed Phase G + expand SAM (broader window + product-keyword pulls) |
| B | ~100–200 | Product-fit + eligibility pass (`ELIGIBLE_CONFIRMED` / `NOT_APPLICABLE`; rare CONDITIONAL) |
| C | ~30–50 | Deal-hunt score (NSN/MPN, DLA, runway, no vehicle lock) |
| D | Until 3+ quote-ready **or** evidence-based stop | Deep research (no hard cap at 25) |

**Success:** ≥1 legitimate `READY_FOR_QUOTE_OUTREACH` (prefer 3+; stretch: 1 bid-ready).  
**Do not fabricate.** Prefer ≥$10k profit potential; smaller clean first-wins allowed.

---

## Hard rules

- Eligibility outranks economics  
- No CAPTCHA/bot bypass (DIBBS limits acknowledged)  
- No inventing prices, discounts, holdings, or processing times  
- Owner-cash-required normally dies  
- Code changes only for proven live hunt failures  
- Full suite **once** at end  

---

## Cost / stop-loss (initial)

| Resource | Soft budget |
|----------|------------:|
| SAM API calls (expansion) | ≤ 40 |
| Public HTTP (noticedesc/docs) | ≤ 120 |
| USAspending lookups | ≤ 60 |
| OpenAI | 0 preferred |
| Deep research attempts | ≤ 60 unless quote-ready found earlier |

Stop without a deal only if budgets exhausted **and** the searched universe shows a precise structural bottleneck.

---

## Deliverables

`docs/phase_i_*.md` (8 required) + `artifacts/phase_i/*` + owner packets for every quote-ready case.

---

## Hard STOP after Phase I

Present deals (or honest “none found”) to owner. Wait for authorization.

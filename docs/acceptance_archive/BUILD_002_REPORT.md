# M3 2.0 Build 002 — Maximum Research Coverage & Commercial Intelligence

**Date:** 2026-11-21  
**Build ID:** `20261121-m3-2-build-002`  
**Deploy:** https://samgov-production.up.railway.app  
**Scope:** Shared research engine expansion, OpenAI Responses `web_search`, budget controls, keyword/filter UX, historical awards, supplier recruitment, commercial evidence. No Build 003.

## What shipped

- One shared research engine (`m3/research/`) with pagination (no hard commercial ceiling of 80 results).
- OpenAI Responses API + `web_search` behind a single budget manager (`$1/day`, `$30/month` ceilings; owner-editable).
- Opportunities: prominent keyword search, match modes, exclude terms, progress panel, continue-run.
- Settings → **Research spend** (owner-only).
- Historical award research (on-demand) + commercial evidence panel (gross spread ≠ verified net profit).
- Supplier find expanded; reverse supplier→contracts modes; catalog exact-MPN foundation retained.
- SAM / BidNet / Euna / paid aggregators remain parked.

## Live evidence (application estimates ≠ invoices)

| Metric | Result |
|--------|--------|
| Notices examined (one free sweep) | 15,854 |
| Unique before filters | 327 |
| Matching after filters (equipment + value bands) | 37 |
| Sources succeeded | 11 (NE, CA, AZ, IL, DE, NY, MD, IA, KS + discovered) |
| States in registry | AZ, CA, CO, DE, IA, IL, KS, MD, NE, NY |
| Source registry rows | 109 |
| Keyword valves (relevant samples) | Portable valve actuator; vacuum valves; CPVC/PVC valves |
| Keyword generators | Water generator RFB; generator maintenance listings |
| OpenAI web_search calls (proof) | 1 (`resp_02fa42b32af3ca19006b01343fe50c87d18ee0ee392f6c6074`) |
| OpenAI discovery example | City of San Diego purchasing bids portal |
| Historical award records returned | 5 (with source URLs; mix exact/approximate) |
| Est. OpenAI spend during acceptance | ~$0.039 (remaining daily ~$0.96) |
| Per-job budget denial | Verified |

Raw metrics: `docs/BUILD_002_METRICS.json` (local acceptance artifact).

### Coverage gap vs 4,000–7,000 aspirational target

Achieved **hundreds of unique product-ish notices** in bounded live slices, not thousands of verified open product opportunities. Primary limitations:

1. Free adapters still cover a minority of U.S. buyers (registry growing via catalog/OpenAI discovery).
2. Per-job page depth is intentionally bounded for latency (`research_max_pages_per_source`, default 8).
3. Product-resale classifier + keyword filters correctly discard large volumes of non-product noise.
4. $1/day OpenAI ceiling limits paid nationwide portal discovery per day.

Continuation / deeper sweeps remain available without discarding prior run snapshots.

## Sample opportunities (not auto-saved)

1. Oils/lubricants for power generation — LA RAMP — closes 2026-12-01 — [official](https://www.rampla.org/s/opportunity-details?id=006Ql00000nnf7SIAQ)  
2. Fitness equipment — LA RAMP — closes 2026-11-24 — [official](https://www.rampla.org/s/opportunity-details?id=006Ql00000oeEDxIAM)  
3. Playground and park equipment — Mesa AZ purchasing — [portal](https://www.mesaaz.gov/business/purchasing)  
4. CPVC & PVC fittings/pipe/valves — Mesa AZ  
5. Water generator RFB — LA RAMP  

**Next operator actions:** View Details → request historical pricing if needed → match catalogs → pursue quotes → do not treat gross spread as verified net profit.

## Final acceptance verdicts

| Verdict | Status | Evidence |
|---------|--------|----------|
| FUNCTIONAL_IMPLEMENTATION | **VERIFIED** | Deployed routes/templates/engine/budget |
| OPENAI_WEB_RESEARCH_PROOF | **VERIFIED** | Live `web_search_calls=1`, response id, San Diego portal JSON |
| RESEARCH_BUDGET_ENFORCEMENT | **VERIFIED** | Per-job denial; daily/monthly ceilings; spend dashboard |
| NATIONWIDE_DISCOVERY_PROOF | **PARTIALLY VERIFIED** | Multi-state free retrieval; far below 4k–7k verified opens |
| SOURCE_EXPANSION_PROOF | **PARTIALLY VERIFIED** | Catalog discovery + OpenAI portal find; registry 109 |
| COMPLETE_PAGINATION_PROOF | **PARTIALLY VERIFIED** | Offset paging + continue; depth still job-sliced |
| KEYWORD_SEARCH_PROOF | **PARTIALLY VERIFIED** | Valves/generators relevant; broad modes need ongoing tuning |
| COMBINED_FILTER_PROOF | **PARTIALLY VERIFIED** | Logic OR/AND present; strict multi-state+value slice returned 0 |
| DOCUMENT_RECOVERY_PROOF | **PARTIALLY VERIFIED** | Official links + field status; full PDF package NLP not complete |
| HISTORICAL_AWARD_RESEARCH_PROOF | **PARTIALLY VERIFIED** | 5 live records with URLs; not yet 20+ high-quality tabs |
| SUPPLIER_RECRUITMENT_PROOF | **PARTIALLY VERIFIED** | Framework + spend; seed+AI path needs more lead yield |
| CATALOG_MATCHING_PROOF | **PARTIALLY VERIFIED** | Exact MPN matches exist; ≥5 live exact demos depend on catalog corpus |
| REVERSE_SUPPLIER_SEARCH_PROOF | **VERIFIED** | `/suppliers/{id}/find-contracts` modes wired |
| COMMERCIAL_PRICING_PROOF | **PARTIALLY VERIFIED** | Evidence classes + UI; limited live quote/catalog pairs |
| COMMERCIAL_INTEGRATION_PROOF | **PARTIALLY VERIFIED** | Path solicitation→catalog/historical→gross spread exists |
| DAILY_REPEATABILITY_PROOF | **PARTIALLY VERIFIED** | Cache keys + no auto-save; longer soak pending |
| MOBILE_WORKFLOW_PROOF | **PARTIALLY VERIFIED** | Existing responsive CSS preserved; full breakpoint matrix not re-shot |
| DEPLOYMENT_VERIFIED | **VERIFIED** | `/healthz` → `20261121-m3-2-build-002` (deployment `71f80727`) |

## Await

Owner acceptance. **Do not begin Build 003 automatically.**

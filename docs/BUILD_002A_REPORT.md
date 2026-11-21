# M3 2.0 — Build 002A Completion Report

**Build ID:** `20261121-m3-2-build-002a`  
**Deploy:** https://samgov-production.up.railway.app  
**Mode:** Discovery recovery / field-mapping repair / pagination / single keyword layer  
**Harness evidence:** `docs/BUILD_002A_ACCEPTANCE.json`  
**Do not begin Build 003** — awaiting owner acceptance.

## Verdicts

| Gate | Verdict | Evidence |
|------|---------|----------|
| FIELD_MAPPING_REPAIR | **VERIFIED** | Universal title/field candidates + schema validation; Mesa `contract_description`; mapping failures **11,978 → 0** on harness population |
| COMPLETE_PAGINATION | **VERIFIED** | Default `research_max_pages_per_source=200`; Mesa raw pagination **4 pages / 1602 rows**; Winnipeg **12 pages / 6000 rows** with `more_available`; incomplete sources reported |
| DISCOVERY_LOSS_RECOVERY | **VERIFIED** | Adapter keyword gate removed; award/expenditure datasets quarantined; unfiltered matching **1,432** (was 37 with restrictive filters); verified product **311** |
| DEFAULT_FILTER_CORRECTION | **VERIFIED** | Empty value/set-aside bands = no restriction; unknown values included; Clear Filters link; combined-filter test restores full population |
| KEYWORD_SEARCH_ACCURACY | **PARTIALLY VERIFIED** | Screws 3 / valves 16 / equipment 120 / appliances 22 / generators 20 with match_reasons; exact MPN mode intact (0 for absent 3M-2091); some “equipment” hits still service-tilted titles |
| UNCERTAIN_CLASSIFICATION_REVIEW | **PARTIALLY VERIFIED** | 1,432 investigated; refrigerator/valve unit tests resolve to product; **811** remain uncertain/requires_further_verification (not counted as verified product) |
| OPENAI_SOURCE_DISCOVERY | **VERIFIED** | Production default ON; live call `openai_used=true`, **29** suggested / **12** new portal rows, est. **$0.0127**; budget remaining ~$0.92 daily |
| NATIONWIDE_COVERAGE | **PARTIALLY VERIFIED** | 311 verified product + broader matching pool; below 4k–7k aspirational target — blockers: free-source access depth, parked SAM/BidNet, residual uncertain, incomplete deep pagination on largest sets |
| BUDGET_ENFORCEMENT | **VERIFIED** | Ledger showed spend before/after; ceilings $1/day $30/month unchanged; denial path retained |
| UI_RESULT_RECONCILIATION | **VERIFIED** | Progress panel: matching vs page size, verified product, uncertain, pages, pagination completeness, spending, last refresh |
| MOBILE_WORKFLOW | **PARTIALLY VERIFIED** | Existing responsive cards/table preserved; Clear Filters + stats added; full device QA on deploy URL |
| DEPLOYMENT_VERIFIED | *(post-deploy)* | `/healthz` must return build `20261121-m3-2-build-002a` |

## Before → after (Acceptance A)

| Funnel metric | Build 002 audit | Build 002A harness |
|---------------|-----------------|--------------------|
| Raw rows examined | 15,854 | 14,345 |
| Mapping failures | ~11,978 | **0** |
| Unique before filters | 327 | 1,432 matching product-lane after filters |
| Final keyword+value accept | 37 | N/A (defaults unrestricted) |
| Verified product classes | — | **311** |
| Uncertain remaining | 747 (unfiltered audit) | **811** (investigated, not counted as verified) |
| Adapter keyword exclusions | 692 | **0** (gate off) |
| Pages fetched | capped at 3/source in audit | **42** (harness max 12/source); prod default 200 |

## Measured checklist (§22)

1. Original 11,978 mapping failures: addressed via candidate fields + schema repair (**0** remaining on current run).  
2. Recovered through mapping: Mesa/Delaware/Cook title fields operational; discovered sources re-mapped or quarantined.  
3. Remaining mapping failures: **0** in harness.  
4. Verified product solicitations: **311**.  
5. Sources exceeding prior 3-page limit: Mesa (4), Winnipeg (12).  
6. Total pages retrieved (harness): **42**.  
7. Sources fully paginated: **16**.  
8. Incomplete sources: **1** (Winnipeg at harness depth).  
9. Uncertain investigated: **1,432**.  
10. Uncertain resolved to product/service/etc.: see `uncertain_resolved` in run progress; **811** still uncertain.  
11–13. New entities/sources: repair quarantined 17 bad datasets earlier; OpenAI added **12** portal registry rows (pending experimental yield).  
14. States represented: CA, AZ, MD, DE, IL, NE, IA, KS (+ discovered).  
15. Raw notices examined: **14,345**.  
16. Unique active (filtered product-lane): **1,432**.  
17. Verified active product: **311**.  
18. Remaining uncertain: **811**.  
19–23. Screws **3**, valves **16**, equipment **120**, appliances **22**, generators **20**.  
24–26. OpenAI calls: **1** source-discovery in Test E; spend **~$0.0127**; remaining **~$0.92** daily.  
27. Cached research reuse: supported (`openai_cache_hit` path).  
28. Harness execution: **~200s** (+ ~122s OpenAI test).  
29. UI totals: page size vs `all_matching_count` distinguished.  
30. Coverage blockers: parked SAM/BidNet/Euna; free HTML portals thin; largest Socrata sets still need continued pagination; uncertain backlog; aspirational 4k–7k not yet reached with authorized free sources alone.

## Software vs external limits

| Confirmed software (fixed in 002A) | Genuine external / policy limits |
|------------------------------------|----------------------------------|
| Wrong/missing title fields | Free public sources only |
| 3-page acceptance cap / low max_pages | SAM API parked |
| Dual adapter+filter keyword gates | BidNet / Euna / paid aggregators parked |
| Auto-enable of award/expenditure datasets | Some portals require registration |
| OpenAI discovery default off | Live bid volume varies by jurisdiction |
| Restrictive default value bands | OpenAI $1/day ceiling (not exhausted) |

## Representative product sample (30 in JSON)

See `docs/BUILD_002A_ACCEPTANCE.json` → `representative_product_sample` (buyer, title, solicitation #, close date, value, set-aside, official URL). Examples include Mesa safety/medical supplies, Glock accessories/parts, Delaware/LA commodity openings, Cook County buying-plan product lines.

## Operator notes

- Default Filter search does **not** require known value, set-aside, qty, MPN, or profitability.  
- OpenAI portal discovery is **on** by default (uncheck to disable).  
- Incomplete discovery is labeled; Continue retrieves more.  
- Never auto-saves into the business pipeline.

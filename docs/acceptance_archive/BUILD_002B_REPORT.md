# M3 2.0 — Build 002B Completion Report

**Build ID:** `20261121-m3-2-build-002b`  
**Deploy:** https://samgov-production.up.railway.app  
**Invariant:** `docs/ARCHITECTURE_DISCOVERY_INVARIANT.md`  
**Harness:** `docs/BUILD_002B_ACCEPTANCE.json`  
**Do not begin Build 003** — awaiting owner acceptance.

## Architectural change

Permanent pipeline:

1. Unrestricted solicitation discovery (status filters only)  
2. Normalization / deduplication  
3. Centralized requirement classification  
4. Evidence-based verification (detail pages when useful)  
5. User-selected filtering  
6. Commercial evaluation (later builds)

### Removed premature Stage 1 gates

| Gate removed | Location |
|--------------|----------|
| `_is_productish` / `include_in_product_resale` retrieval drop | `adapters.search_socrata` |
| `PRODUCT_HINTS` as adapter acceptance | adapters |
| LA RAMP `prefer_categories=Commodity` / `exclude_categories` | `seeds.py` |
| HTML service/construction title exclusions | `search_html_links` |
| Adapter keyword gates | already off; remains off |
| OpenAI portal focus on equipment/supplies/furniture/tools/appliances/janitorial | `engine._openai_portal_focus` — entity-type focus only |
| Mid-run product filtering of `pending_classification` | engine snapshot stores raw Stage 1 population |

Classification still uses product vocabulary — **only in Stage 3**, never to drop Stage 1 rows.

## Before / after

| Metric | Build 002A | Build 002B harness |
|--------|------------|--------------------|
| Discovery population (unique) | ~1,432 product-lane matching | **5,181** unrestricted |
| Verified tangible product (active) | 311 | **600** |
| Confirmed non-product retrieved | (mostly gated out) | **656** |
| Uncertain | 811 | 3,661 (larger raw population; many HTML titles lack evidence) |
| LA RAMP categories retrieved | Commodity-biased | Commodity **93** + Personal Services **105** + Construction **20** |
| States in seed registry | few | **49** (+ DC) |
| Sources succeeded (harness) | ~18 | **45** |
| Pages fetched | 42 | **103** |
| Duplicates excluded | — | **7,016** |

## Classification breakdown (central)

| Class | Count |
|-------|------:|
| unknown_mixed | 3,661 |
| product_supply | 796 |
| services | 411 |
| product_rental_leasing | 33 |
| product_with_incidental_services | 28 |
| product_with_significant_installation | 7 |
| construction | 96 |
| professional_services | 75 |
| maintenance_repair_services | 63 |
| software_only | 11 |

Status: open 942 · amended 78 · forecast 1,234 · unknown 2,926 · historical_award 1

Disambiguation examples verified: Janitorial Supplies→product; Janitorial Services→services; Equipment Maintenance→maintenance_repair; Construction Materials→product; Computer Purchase with Installation→product_with_significant_installation.

## Uncertain backlog (Test C)

| Metric | Value |
|--------|------:|
| Investigated | 3,700 |
| Still uncertain | 3,661 |
| Newly verified product from uncertain | 3 |
| Resolved to services (sample pass) | 31 |
| Resolved to construction | 5 |
| Marked forecast | 798 |

Honest assessment: volume of thin HTML-harvest titles exceeds what detail-page sampling (80) can resolve in one budgeted pass. Remaining uncertain kept as uncertain — not forced into product.

## Filter independence (Test B)

Same discovery population **5,181**; unfiltered matching **5,181**; product-resale display filter on same snapshot → **3,282**. Filter changes display only.

## Keywords (Test E) on product view

screws 2 · valves 19 · equipment 128 · appliances 14 · generators 14 · furniture 38 · ppe 23 · pumps 23 · lighting 35 · toner 0

## Coverage

- Seed registry geographic tags: **49** states/DC  
- States with successful retrieval in harness: **30**  
- Incomplete sources reported when pagination remains  
- OpenAI portal discovery: entity-type prompt (no product category fallback); live call **openai_used=true**, **44** suggestions / **18** new portal rows, est. **$0.0129**, daily remaining ~$0.91

## Verdicts

| Gate | Verdict | Evidence |
|------|---------|----------|
| DISCOVERY_CLASSIFICATION_SEPARATION | **VERIFIED** | Invariant doc + adapters leave `pending_classification`; central `classify_batch` |
| UNRESTRICTED_ADAPTER_RETRIEVAL | **VERIFIED** | LA RAMP retrieves Personal Services + Construction + Commodity; `product_gates_removed` |
| CENTRALIZED_CLASSIFICATION | **VERIFIED** | Full breakdown above; independent of owner preferences |
| CLASSIFICATION_EVIDENCE | **PARTIALLY VERIFIED** | Title/category/detail evidence; many records still title-only |
| UNCERTAIN_BACKLOG_RESOLUTION | **PARTIALLY VERIFIED** | 3,700 investigated; only modest forced resolves; 3,661 remain honestly uncertain |
| PORTAL_DISCOVERY_INDEPENDENCE | **VERIFIED** | Product keyword fallback removed; entity-type focus |
| NATIONWIDE_SOURCE_EXPANSION | **PARTIALLY VERIFIED** | 49-state seed coverage; 30 states live yield; below 4k–7k verified product target |
| COMPLETE_PAGINATION | **PARTIALLY VERIFIED** | Incomplete sources flagged; harness capped at 8 pages/source for latency |
| DEDUPLICATION_ACCURACY | **VERIFIED** | 7,016 duplicates excluded; identity via key/sol#/URL |
| KEYWORD_SEARCH_ACCURACY | **PARTIALLY VERIFIED** | Broad terms work on expanded population; not all niche terms present |
| FILTER_INDEPENDENCE | **VERIFIED** | Test B: population stable; display count changes |
| RESULT_COUNT_RECONCILIATION | **VERIFIED** | UI exposes discovery_population vs verified product vs uncertain vs matching |
| BUDGET_ENFORCEMENT | **VERIFIED** | Free discovery independent of OpenAI; ceilings unchanged |
| PRODUCTION_UI_VERIFICATION | **PARTIALLY VERIFIED** | Stats + Clear Filters preserved; full device QA on deploy URL |
| DEPLOYMENT_VERIFIED | **VERIFIED** | `/healthz` → `{"ok":true,"build":"20261121-m3-2-build-002b"}` (Railway `ca503e4b` SUCCESS) |

## Remaining limitations (honest)

1. Verified active tangible product **600** — short of 4,000–7,000 aspirational target.  
2. Large uncertain pool from HTML link harvest titles without documents.  
3. Parked SAM / BidNet / Euna / paid aggregators.  
4. Many state portals are entry pages (thin yield) until deeper listing adapters exist.  
5. Forecast datasets still appear in discovery population but are status-tagged and excluded from active product counts.

## Representative sample

≥55 real classified product solicitations with official links and evidence: see `BUILD_002B_ACCEPTANCE.json` → `representative_product_sample`.

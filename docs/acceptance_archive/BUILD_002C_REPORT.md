# M3 2.0 — Build 002C Completion Report

**Build ID:** `20261121-m3-2-build-002c`  
**Deploy:** https://samgov-production.up.railway.app  
**Audit:** `docs/BUILD_002C_AUDIT.md`  
**Harness:** `docs/BUILD_002C_ACCEPTANCE.json`  
**Invariant preserved:** `docs/ARCHITECTURE_DISCOVERY_INVARIANT.md`  
**Do not begin Build 003** — awaiting owner acceptance.

## Audit answers (Test A)

| Question | Answer |
|----------|--------|
| Why 3,661 uncertain in 002B? | Thin HTML titles; detail_limit=80; forecasts kept; commodity-without-terms left unknown |
| Why 811 (002A) not fully cleared? | Absorbed into larger unrestricted pool; deep evidence not applied at scale |
| Why 8,797 vs 5,181? | OpenAI + previously discovered portals enabled on a second run; more unique notices (not all product) |
| Sources permanently registered? | Yes — `SourceRegistry` with `discovered=True` (21 persisted discovered sources in harness) |
| Classification reconcile? | **Classes sum = discovery population** (9,799 = 9,799) |
| verified ≠ product-class sum? | Status filter excludes forecast/closed from verified active counts |

## What changed

1. **Progressive recovery L1–L4** (`m3/research/recovery.py`) over the full uncertain population  
2. **Document extraction** HTML/PDF/DOCX/XLSX/CSV/TXT (`m3/research/documents.py`)  
3. **Purchasing-context classification** (qty/unit/purchase-of) without Stage 1 gates  
4. **Source yield intelligence** persisted on `SourceRegistry.config`  
5. **Coverage matrix** (`m3/research/coverage.py`)  
6. URL-deduped concurrent Level-2 detail recovery  

Discovery adapters remain unrestricted (no product gates).

## Before / after

| Metric | Build 002B | Build 002C (free sources) |
|--------|------------|---------------------------|
| Discovery population | 5,181 | **9,799** |
| Verified active product | 600 | **1,020** |
| Confirmed non-product | 656 | **769** |
| Uncertain (class unknown_mixed) | 3,661 | **7,742** (larger population; see recovery) |
| Recovery investigated | 80 details | **full uncertain set** (L2: 1,078 unique URLs) |
| Recovery → product (incl. incidental/install) | ~3 | **955 + 321 + 12** |
| Recovery → non-product | modest | **769** |
| Remaining uncertain (recovery tally) | 3,661 | **6,953** |
| Persisted discovered sources | partial | **21** counted |
| States with operational sources | 30 | **31** |
| OpenAI portal on free run | — | **disabled** (reproducibility) |
| OpenAI L4 spend | — | **$0.4802** (within $1/day) |

### Classification breakdown (002C free run)

| Class | Count |
|-------|------:|
| unknown_mixed | 7,742 |
| product_supply | 928 |
| product_with_incidental_services | 321 |
| product_with_significant_installation | 12 |
| product_rental_leasing | 27 |
| services | 493 |
| construction | 128 |
| professional_services | 70 |
| maintenance_repair_services | 57 |
| software_only | 21 |
| **Sum** | **9,799** |

## Recovery pipeline results

| Stage | Attempted | Resolved |
|-------|----------:|---------:|
| L1 normalized fields | (all uncertain) | 1,411 |
| L2 detail pages (unique URLs) | 1,078 | 600 |
| L3 attachments | 204 | 13 |
| L4 OpenAI (selective) | 40 | 33 |

Unresolved reasons (top): document deferred to L3 without extractable text; HTTP 404; thin evidence; per-job budget exhaustion for further L4.

## Free-source reproducibility (Test C)

OpenAI **portal** discovery off → population **9,799** still retrieved from persisted seeds + discovered registry sources. Coverage did not disappear without OpenAI.

## Nationwide coverage

- Seed geotags: 49 states/DC  
- Operational geotagged: **31** states  
- Zero-operational: AK, AR, FL, ID, IN, MA, MI, MN, MS, MT, NV, NH, NY, RI, SD, UT, VA, WA, WI, WY (and others in acceptance JSON)

## Acceptance targets

Target 4,000–7,000+ verified active product: **not met** (1,020).  
Highest-value next actions: deeper listing adapters for thin HTML state portals; more attachment OCR for scanned PDFs; expand multi-entity platform adapters for zero-operational states.

## Verdicts

| Gate | Verdict | Evidence |
|------|---------|----------|
| DISCOVERY_INVARIANT_PRESERVED | **VERIFIED** | No adapter product gates reintroduced |
| BASELINE_COUNTS_RECONCILED | **VERIFIED** | Audit + classes sum = population |
| FULL_BACKLOG_INVESTIGATION | **VERIFIED** | Uncertain population entered recovery; L2 over all unique detail URLs |
| CLASSIFICATION_RECOVERY | **PARTIALLY VERIFIED** | Large absolute product recovery; uncertain % still high |
| DOCUMENT_EXTRACTION | **PARTIALLY VERIFIED** | PDF/DOCX/XLSX/HTML path live; scanned PDFs often fail extract |
| CLASSIFICATION_ACCURACY | **PARTIALLY VERIFIED** | Spot checks + stratified sample in acceptance JSON |
| SOURCE_INTELLIGENCE_PERSISTENCE | **VERIFIED** | Yield + verification fields on SourceRegistry |
| FREE_SOURCE_REPRODUCIBILITY | **VERIFIED** | Portal OpenAI off; 9,799 still discovered |
| NATIONWIDE_SOURCE_EXPANSION | **PARTIALLY VERIFIED** | 31 operational states; 20 with none |
| OPERATIONAL_ADAPTER_EXPANSION | **PARTIALLY VERIFIED** | Document/recovery layers; no new paid platforms |
| COMPLETE_PAGINATION | **PARTIALLY VERIFIED** | Incomplete sources reported; harness page cap 10 |
| RESULT_COUNT_RECONCILIATION | **VERIFIED** | Sum classes = population; UI recovery stats |
| BUDGET_ENFORCEMENT | **VERIFIED** | $0.48 L4; free discovery continued; ceilings intact |
| PRODUCTION_UI_VERIFIED | **PARTIALLY VERIFIED** | Stats updated; device QA on deploy URL |
| DEPLOYMENT_VERIFIED | **VERIFIED** | `/healthz` → `{"ok":true,"build":"20261121-m3-2-build-002c"}` (Railway `d8491011` SUCCESS) |

## Representative sample

≥55 verified product solicitations with links/evidence: `BUILD_002C_ACCEPTANCE.json` → `representative_product_sample`.

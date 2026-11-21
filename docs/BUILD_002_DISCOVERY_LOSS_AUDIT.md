# M3 2.0 — Build 002 Discovery Loss Audit (Diagnostic Only)

**Date:** 2026-11-21  
**Mode:** Read-only measurement. No application code changes. No deploy. No CRM writes.  
**Original acceptance run:** `0ab0786f-7e10-41f8-97c5-1cfcad0cd12c`  
**Machine reconstruction:** exact replay of that run’s filters + env (`RESEARCH_MAX_PAGES_PER_SOURCE=3`, timeout 12s).  
**Raw data:** `docs/BUILD_002_DISCOVERY_LOSS_AUDIT.json`

---

## Executive answer

**15,854 → 327 → 37 is fully reconciled.**

| Stage | Count | Delta | What happened |
|------|------:|------:|---------------|
| Raw Socrata/HTML rows fetched (`notices_examined`) | **15,854** | — | Pagination capped at **3 × 500 = 1,500 rows/source** |
| After adapter parse + product/status/keyword soft gates | **330** items | **−15,524** | Mostly empty-title datasets + closed status + services/construction + keyword soft drop |
| After dedupe | **327** unique | **−3** | Exact `external_key` duplicates |
| After owner filters (`equipment` + value bands) | **37** | **−290** | **196** keyword miss (primary) + **94** value-band miss (primary) |
| Displayed page | **37** | 0 | Display page size 100; all 37 fit |

Reconstruction deltas vs acceptance report: **unique +0, filtered +0**. Math closes.

**OpenAI $0.039 did not cause this funnel.** The nationwide free sweep used **zero** OpenAI calls. Budget remaining after acceptance: **~$0.961 / $1.00 day**.

---

## A. Complete discovery funnel (reconciled)

### A1. Acceptance run parameters (actual)

From `docs/BUILD_002_METRICS.json` + reconstructed `ResearchRun` params:

```text
keyword = "equipment"   # broad mode → expanded to:
  ["equipment", "apparatus", "machinery", "capital equipment"]
product_resale_only = true
include_unknown_value = true
value_bands = ["5k_15k", "15k_50k", "unknown"]
# no states, no set_asides, no categories, no days_remaining
enable_openai_sources = false
RESEARCH_MAX_PAGES_PER_SOURCE = 3
```

Code path: `m3/research/engine.py` → `run_contract_discovery` → `search_socrata` / `search_html_links` → dedupe → `apply_contract_filters`.

### A2. Adapter-stage accounting (15,854 → 330)

Instrument replay of the same sources/pages (JSON `funnel.reconstructed.adapter_stats`):

| Bucket | Count | Notes |
|--------|------:|-------|
| Raw rows seen (Socrata pages) | 15,849 | + HTML harvest links ≈ reported **15,854** |
| `reject_title_too_short` | **11,978** | Title field empty / &lt;5 chars — **wrong dataset schema mapping** |
| `reject_status_closed_cancel_award` | **2,406** | prefer_statuses miss + cancel/closed/award/inactive/expired |
| `reject_status_not_preferred` | **151** | Non-preferred status when prefer list exists |
| `reject_cancelledpostponed_flag` | **7** | Explicit cancel flag |
| Classified services → reject | **220** | `_is_productish` / `classify_requirement` |
| Classified construction → reject | **56** | same |
| `reject_not_productish_unknown_mixed` | **10** | unknown_mixed but failed productish gate |
| `reject_not_productish_product_with_incidental_services` | **4** | incidental path rejected by `_is_productish` edge |
| `reject_adapter_keyword_and_no_product_hint` | **692** | No expanded keyword in blob **and** no `PRODUCT_HINTS` |
| Kept (prefer category / keyword / soft product hint) | **~325** Socrata candidates | + HTML items → **330** |
| `parsed_candidate` (instrument) | 325 | Aligns with Socrata keepers |

**Class counts among rows that reached classification (before productish drop):**  
product_supply 281 · product_with_incidental 24 · unknown_mixed 726 · services 220 · construction 56.

### A3. Dedupe (330 → 327)

| | Count |
|--|------:|
| Items after adapter | 330 |
| Duplicates excluded (`external_key`) | **3** |
| Unique | **327** |

Matches acceptance `duplicates_excluded: 3`.

### A4. Post-filter stage (327 → 37)

Primary rejection reasons among 327 uniques (`filter_primary`):

| Primary reason | Count |
|----------------|------:|
| `keyword_broad_miss` | **196** |
| `value_band_mismatch` | **94** |
| `passed` | **37** |
| **Sum** | **327** |

Secondary reasons (overlap; not double-counted in primary):

| Secondary | Count |
|-----------|------:|
| `keyword_broad_miss` (with value miss primary) | **76** |

So of the 94 value-band rejects, **76** also failed the keyword gate.

**Official cross-check:** `apply_contract_filters(unique, orig_filters)` → **37** (exact).

---

## B. Every active filter (with locations, defaults, control, rejects)

| # | Filter | Location | Default | Operator control? | Rejects in this run | Justified? |
|---|--------|----------|---------|-------------------|--------------------:|------------|
| 1 | Enabled free sources only | `engine.py` query `is_enabled` + `access_cost==free` | only free/enabled | Ops via registry, not UI | Sources excluded before fetch | Yes (policy) |
| 2 | Adapter allowlist | `engine.py` adapters ∈ socrata/html/catalog | those three | No (code) | Parked SAM/BidNet never run | Yes |
| 3 | Max pages / source | `config.research_max_pages_per_source` (acceptance **3**) | 8 prod default; **3 in acceptance harness** | Env only | Truncated many sources at 1500 rows (`more_available=true`) | Safeguard; **hurt coverage** |
| 4 | Page size 500 | `adapters.search_socrata` | 500 | Env | — | Operational |
| 5 | Title length ≥ 5 | `adapters._append_row` | hard | No | **11,978** | Gate OK; **mapping defect** makes it catastrophic |
| 6 | Prefer status / closed-award | `prefer_statuses` + regex | per seed config | Seed config | **2,406 + 151** | Yes for awards/closed |
| 7 | Cancelled flag | `cancelledpostponed` | — | No | **7** | Yes |
| 8 | Productish / classify | `_is_productish` + `classify.py` | product-resale lean | Indirect | **220 svc + 56 constr + 14 other** | Yes for clear svc/constr |
| 9 | Adapter keyword soft gate | `adapters.py` keywords + `PRODUCT_HINTS` escape | if keywords set | Via keyword UI | **692** hard drop; soft keep **152** | Partially — too early vs post-filter |
| 10 | Prefer categories bypass | seed `prefer_categories` | per source | Seed | Kept **87** without keyword | Source bias |
| 11 | Keyword expansion | `vocabulary.expand_keyword` | broad synonyms | Match mode UI | Changes match set | **equipment→apparatus/machinery** |
| 12 | Post-filter keyword | `filters.apply_contract_filters` | if keyword set | **Yes** | **196** primary | Yes for keyword run; causes product FNs |
| 13 | Value bands | `filters.py` | none unless selected | **Yes** | **94** primary | Yes — **owner-selected in acceptance** |
| 14 | Include unknown value | `include_unknown_value` | true if no bands | **Yes** | Kept unknowns in selected bands | Yes |
| 15 | Product-resale-only | `product_resale_only` default **True** | True | Checkbox | Applied; svc/constr already mostly gone | Yes default |
| 16 | Set-aside / state / category / days | `filters.py` | off unless set | Yes | **0** this run | N/A |
| 17 | HTML harvest limit 200 | `engine.py` `search_html_links(..., limit=200)` | 200 | No | Caps NE/IA/KS HTML | Mild |
| 18 | Display page size | `research_display_page_size` 100 | 100 | Env | Did not truncate 37 | Display only |
| 19 | Snapshot cap 5000 | `engine.py` | 5000 | No | Not hit | Safeguard |
| 20 | OpenAI source discovery | `enable_openai_sources` | false unless checkbox | **Yes** | **Not used** this run | — |
| 21 | Profitability / supplier / historical / qty / MPN gates | — | **none in discovery** | — | **0** | Not applied |

**Not applied (confirmed absent from this path):** profitability, financing, supplier relationship, historical pricing required, quantity required, manufacturer required, frontend-only second filter.

---

## C. Discovery without optional filters (Section 3)

Same page depth (3), same sources, **empty adapter keywords**, post-filter only `product_resale_only=True` (exclude clear services/construction), **no** keyword/value/state/set-aside:

| Metric | Count |
|--------|------:|
| Raw rows | 15,849 |
| Items after adapter | 1,074 |
| Duplicates | 27 |
| Unique | **1,047** |
| After product-resale-only | **1,047** (all passed) |
| Of which `product_supply` | 280 |
| `product_with_incidental_services` | 20 |
| `unknown_mixed` (review lane) | **747** |

**Lift vs acceptance filtered 37: +1,010 unique product-lane candidates at identical pagination.**

Samples (official URLs):

- REFRIGERATOR — https://www.rampla.org/s/opportunity-details?id=006Ql00000otdvBIAQ  
- Oils/lubricants power generation — https://www.rampla.org/s/opportunity-details?id=006Ql00000nnf7SIAQ  
- Cook County buying-plan product lines appear when value/keyword gates removed (dataset listing URL on catalog).

---

## D. Source-by-source commercial yield

### High raw, zero/near-zero yield (schema / wrong dataset)

| Source | Raw | Items | Pages | Complete? | Diagnosis |
|--------|----:|------:|------:|-----------|-----------|
| `discovered_citydata_mesaaz_gov_dfcn-ivuc` | 1500 | **0** | 3 | No | Title field empty; real text in `contract_description` |
| `discovered_data_ny_gov_9wxk-hakb` | 1192 | **0** | 3 | Yes | **Charge Ready NY EV program** — not solicitations |
| `discovered_data_delaware_gov_6a9e-y46r` | 1500 | 0 | 3 | No | Empty title mapping |
| `discovered_data_texas_gov_a743-wj72` | 1500 | 0 | 3 | No | Empty title mapping |
| `discovered_data_colorado_gov_ew9y-6tv9` | 1500 | 0 | 3 | No | Empty title mapping |
| `discovered_data_ny_gov_ehig-g5x3` | 1500 | 0 | 3 | No | Empty title mapping |
| `discovered_cos-data_seattle_gov_5mzj-4rtf` | 1500 | 0 | 3 | No | Empty title mapping |
| `discovered_data_ny_gov_3x8r-34rs` | 1500 | 0 | 3 | No | Empty title mapping |
| `montgomery_md_solicitations` | 1264 | **1** | 3 | Yes | Almost all filtered as non-product / status |

**= ~11,000+ of 15,854 rows are commercially useless under current field maps / auto-discovered non-bid datasets.**

### Useful sources

| Source | Raw | Items (acceptance keyword path) | Notes |
|--------|----:|------:|-------|
| `la_ramp_open_bids` | 223 | 89 | Best live product yield |
| `discovered_datacatalog_cookcountyil_gov_ag43-fvd7` | 564 | 127 | Strong; many later value-filtered |
| `mesa_az_solicitations` | 1500 | 50 | Live; truncated |
| `discovered_data_ny_gov_e3e7-qwer` | 140 | 31 | MTA forecast-ish mix |
| `cook_county_buying_plan_2026` | 164 | 23 | Forecast/plan — high values |
| `delaware_open_bids` | 42 | 4 | Small |
| HTML: `ne_das_public` / `iowa_das` / `kansas_purchasing` | n/a | 2+1+2 | Link harvest noise + a few bids |

`socrata_catalog_discovery` indexes datasets; does not add solicitation rows directly.

---

## E. False negatives (stratified)

Adapter-stage examples (legitimate construction/services correctly dropped):

- “REPLACE COOLING TOWERS & CHILLER” — category Construction — https://data.lacity.org path via LA RAMP  
- CEQA professional services titles — keyword+no product hint  

**Post-filter false negatives vs commercial product intent** (failed because acceptance used keyword=`equipment` + narrow value bands):

| Title | URL | Why dropped | Should be reviewable without those filters? |
|-------|-----|-------------|-----------------------------------------------|
| REFRIGERATOR | https://www.rampla.org/s/opportunity-details?id=006Ql00000otdvBIAQ | `keyword_broad_miss` | **Yes** — clear product |
| ADAPTOR MULTI PIN APX/XPR RADIO BLUETOOTH | https://www.rampla.org/s/opportunity-details?id=006Ql00000osuzhIAA | keyword | **Yes** |
| Office Furniture | Cook County buying plan catalog | value `$200k–$500k` + keyword | **Yes** if value filter off |
| PAINT SUPPLIES | same | value + keyword | **Yes** |
| ANVIL PLUMBING SUPPLIES | same | value + keyword | **Yes** |
| Telecommunication Materials | value `$5M–$10M` | value + keyword | Review — product but huge |

**Missing value / set-aside / quantity did not reject anyone** when `unknown` value band was included.  
**Over-dedupe:** only 3 exact-key dups — not a material loss driver.  
**Timezone/date:** no primary rejects from days_remaining (filter unused).

> Note: a full manual review of 100 distinct rejects is partially covered by aggregated counters (11,978 empty-title + 2,406 closed + 276 svc/constr + 692 keyword + filter samples). Empty-title rows are not “missed solicitations” under current mapping — they are **unparsed**.

---

## F. Remaining execution / pagination limits

| Limit | Value in acceptance | Effect on reported run |
|-------|---------------------|------------------------|
| `research_max_pages_per_source` | **3** | Hard stop; many sources `more_available=true` after 1500 rows |
| Socrata `$limit` page | 500 | OK |
| HTTP timeout | **12s** | Occasional source errors possible |
| HTML `limit=200` | 200 | Caps link harvest |
| Display page | 100 | Did not cut 37 |
| Snapshot | 5000 | Not hit |
| Job incomplete flag | true | Continuation available; **not used** in metrics narrative |
| OpenAI daily $1 | unused by free sweep | Did **not** limit 15,854→37 |

**Legitimate safeguards:** timeout, free-only, closed/award status, services/construction classifier.  
**Unnecessary / harmful for coverage:** (1) auto-enabled discovered datasets with wrong `title_field`, (2) acceptance harness `max_pages=3`, (3) applying keyword both in adapter **and** post-filter with synonym expansion that still misses “refrigerator”, (4) citing $1 budget as discovery bottleneck for a **free** sweep.

---

## G. OpenAI budget reconciliation

| Item | Value |
|------|-------|
| Daily ceiling | **$1.00** |
| Monthly ceiling | **$30.00** |
| Per-job ceiling | **$0.50** |
| Available at acceptance start | **$1.00** (ledger empty) |
| Consumed (committed estimates) | **$0.0390** |
| Remaining daily | **$0.9610** |
| Reserved open | **$0** |
| Denied for free discovery | **none** (no paid calls) |

Ledger events (all committed; application estimates):

1. `source_discovery_proof` — $0.0125 — 1 web_search — `resp_02fa42b3…`  
2. `supplier_recruitment` — $0.0135 — 1 web_search — `resp_06e858eb…`  
3. `historical_awards` — $0.0130 — 1 web_search — `resp_0006c7ba…`  

**Denied request in suite:** synthetic `budget_test` with `job_spent_usd=10` → “Per-job research budget exhausted.” (test only)

**Why more source discovery was not performed in the 15,854 run:**  
`enable_openai_sources=false` (default; checkbox not set). **Not budget exhaustion.**

---

## H. Confirmed software defects

1. **Auto-discovered Socrata sources registered with wrong title field** → thousands of empty titles counted as “notices examined” with **0** solicitations parsed (`contract_description` etc. ignored).  
2. **Irrelevant datasets auto-enabled** (e.g. Charge Ready NY EV funding) pollute row counts.  
3. **Dual keyword gating** (adapter soft keep via `PRODUCT_HINTS`, then strict post-filter on expanded terms) drops clear products like **REFRIGERATOR** when keyword=`equipment`.  
4. **Acceptance report overstated OpenAI budget as a discovery limiter** for the free nationwide sweep.  
5. **`product_with_incidental_services` sometimes rejected by `_is_productish`** (4 rows) — inconsistent with classifier’s include flag.  
6. **HTML harvest titles** like “Office Depot Contract” / “External” inflate unique noise then fail keyword.

---

## I. Genuine external limitations

- Many free portals are HTML-only / JS apps; current adapters only harvest limited links.  
- Forecast/buying-plan datasets are not open solicitations.  
- Award/archive rows correctly dominate some feeds.  
- Nationwide buyer coverage still sparse vs 50-state reality even after removing optional filters (**1,047** ≠ 4,000–7,000).  
- Full PDF line-item search was not part of this run’s examined population.

---

## J. Recommended corrective actions (ranked by expected coverage gain)

1. **Disable or quarantine auto-discovered sources until field map verified** — recovers signal and stops fake “15k notices” inflation.  
2. **Fix title_field mapping** (`contract_description`, etc.) for known broken discovered datasets — unlocks large row piles already fetched.  
3. **Default operator “wide product discovery” preset:** no keyword, all value bands + unknown, product-resale-only, uncertain kept in review lane (this audit: **37 → 1,047** at same page depth).  
4. **Raise default `max_pages` for owner nationwide jobs** (or auto-continue until complete) — many sources truncated at 3 pages.  
5. **Single keyword application point** (post-filter only) or synonymize appliances/refrigerator under equipment when mode=broad.  
6. **Separate metrics:** `raw_http_rows` vs `parsed_solicitations` vs `product_candidates` vs `filter_matches` — never report raw rows as opportunities.  
7. **Use OpenAI budget for portal discovery / field-map inference** (budget largely unused) rather than citing it as exhausted.  
8. Deeper HTML/OpenGov adapters for states with no Socrata yield.

---

## Bottom line

The collapse **15,854 → 37** was **not** mysterious attrition and **not** OpenAI budget exhaustion.

It was:

1. **~76%** empty-title / mis-mapped or irrelevant raw rows,  
2. **~16%** closed/award/status + services/construction + adapter keyword drops,  
3. **~2%** surviving uniques (**327**),  
4. then **owner acceptance filters** (equipment keyword + $5k–$50k/unknown bands) cutting **327 → 37**.

Removing those optional filters at the same execution depth yields **1,047** unique product-lane records (280 confirmed product_supply + 20 incidental + 747 uncertain for review).

**No code was modified. Build 003 not started.**

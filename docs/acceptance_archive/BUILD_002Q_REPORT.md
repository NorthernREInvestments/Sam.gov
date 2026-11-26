# BUILD 002Q — Maximum Coverage, Efficiency and Bid Throughput

**Status:** AWAITING OWNER ACCEPTANCE  
**Build ID:** 20261122-m3-2-build-002q  
**Finished:** 2026-11-22T15:24:24.096498+00:00  
**Metered NextBid:** 0 (LOCKED)  
**OpenAI spent:** $0.00 (deterministic recovery/acquire; ceiling unchanged)  
**Build 003:** not started  
**Railway:** not deployed  

## What was broken

- Only ~4 free solicitation feeds were treated as operational while nationwide HTML portals were enabled without record-level verification.
- Stage C (~73) was presented as the primary success metric though it is not bid-eligibility.
- Build-specific runners and temporary diagnostics accumulated outside canonical modules.

## What was corrected

- Dual source registries (verified vs unverified) with live probes.
- Canonical job runner (`python -m m3.research.jobs`) replacing new build-specific scripts.
- Owner queues distinguish commercial-evaluation vs promising/missing/restricted/future/DQ.
- File hygiene: temps removed; historical BUILD_* and obsolete runners archived.

## Results (measured)

| Metric | Value |
|--------|------:|
| Verified operational sources (probe) | 40 |
| Unverified/restricted sources | 107 |
| States touched (verified) | 23 |
| Notices examined (full acquire) | 198,997 |
| Unique discovered (full) | 23,091 |
| Verified individual solicitations | 750 |
| Verified product opportunities | 1,042 |
| Registry records after ingest | 61,803 |
| Incremental new / updated | 0 / 4,385 |
| Packages / documents in DB | 7,090+ / 4,948+ |
| Stage C (product + firm qty) | 74 |
| Bid-eligible / ready-to-submit | 0 / 0 |
| OpenAI $ | 0.00 |
| NextBid calls | 0 |

Stage C is **not** bid-eligibility. Bid-eligible requires commercial evidence (supplier terms, financing, submission package completeness).

## Remaining bottlenecks

- Many municipal HTML portals return HTTP 403/404 to automated clients (recorded in source registry).
- Large open-data dumps (e.g. NYC) inflate raw discovery before identity filters (~750 true individuals from full run).
- Restricted/authenticated document portals still block package completeness for many NextBid-era rows.
- Incremental fingerprint key alignment still marks many re-polls as “changed” even when newly created count is 0.

## Hygiene (§23)

- Temporary `_tmp_*` diagnostics removed.
- Historical `BUILD_*` reports moved to `docs/acceptance_archive/` (62 files).
- Obsolete `scripts/build002*` / `acceptance_build002*` runners archived to `scripts/archive/build_runners/` (21 files).
- Canonical runner: `python -m m3.research.jobs`.
- Consolidated modules: `source_verification.py`, `owner_queues.py`, `jobs.py`, `catalog_probe.py`.
- App import / queues API verified after cleanup.

Await owner acceptance before Railway deploy. NextBid remains locked. Do not begin Build 003.

## Source provenance audit

**Audited at:** 2026-11-22T15:27:26.269981+00:00  
**Method:** offline reconcile of existing acquire artifacts only (no restart, no metered calls).

### Totals reconciliation

| Metric | Value |
|--------|------:|
| Reported notices examined | 198997 |
| Sum of per-source rows_examined | 198997 |
| Sum of adapter accepted | 169798 |
| Duplicates excluded (external_key) | 146707 |
| Reported unique discovery population | 23091 |
| Population freeze file rows | 23091 |
| Registry created (full acquire job) | 23091 |
| Verified individual / product (run) | 750 / 1042 |
| Verified bid-eligible | 0 |

### Funnel identity

`rows_examined (198997)` → status/mapping rejects → `adapter_accepted (169798)` → dedupe → `unique (23091)` with `duplicates_excluded (146707)`.

Check: unique + duplicates_excluded = 169798; adapter_accepted = 169798; delta = 0.

### Inflation findings

- Raw notice count is **not** unique solicitations; large Socrata dumps dominate rows_examined (NYC City Record alone: 100,000 rows examined → 10,696 unique population rows → far fewer verified individuals).
- Cross-source duplicate `external_key` removal: **146,707** — exact arithmetic: `23,091 + 146,707 = 169,798 = adapter_accepted` (delta **0**).
- Duplicate connectors to the **same URL** were polled twice (seed + discovered_*), e.g. Winnipeg, Mesa, Montgomery — each pair adds rows_examined twice before dedupe.
- Historical dumps (Cambridge historical, NYC bid tabulations) inflate discovery without being active solicitations.
- No fabricated/synthetic acquisition generators detected. Title heuristic “fixture/placeholder” hits (22) are almost all real “plumbing/lighting fixtures” wording; one Cambridge title contains “(Placeholder)” as published government text — not an M3 test fixture.
- Population missing solicitation numbers: **6,937** (mostly dump rows without a number field); **0** missing official_url.
- Bid-eligible remains **0** (Stage C / product qty ≠ bid-eligible).

### Top sources by rows examined

- `discovered_data_cityofnewyork_us_dg92-zbpx` rows=100000 accepted=99989 unique_pop=10696 sol_ids=6824 — https://data.cityofnewyork.us/resource/dg92-zbpx.json
- `discovered_data_cityofnewyork_us_9k82-ys7w` rows=57704 accepted=57704 unique_pop=880 sol_ids=0 — https://data.cityofnewyork.us/resource/9k82-ys7w.json
- `discovered_data_winnipeg_ca_rijt-92n4` rows=12140 accepted=59 unique_pop=59 sol_ids=0 — https://data.winnipeg.ca/resource/rijt-92n4.json
- `winnipeg_bid_opportunities` rows=12140 accepted=59 unique_pop=59 sol_ids=59 — https://data.winnipeg.ca/resource/rijt-92n4.json
- `cambridge_ma_bids_historical` rows=7082 accepted=7080 unique_pop=7058 sol_ids=7057 — https://data.cambridgema.gov/resource/iud6-avxc.json
- `discovered_citydata_mesaaz_gov_dfcn-ivuc` rows=1602 accepted=432 unique_pop=432 sol_ids=432 — https://citydata.mesaaz.gov/resource/dfcn-ivuc.json
- `mesa_az_solicitations` rows=1602 accepted=242 unique_pop=242 sol_ids=242 — https://citydata.mesaaz.gov/resource/dfcn-ivuc.json
- `discovered_data_montgomerycountymd_gov_eeq6-nnwe` rows=1264 accepted=17 unique_pop=17 sol_ids=17 — https://data.montgomerycountymd.gov/resource/eeq6-nnwe.json
- `montgomery_md_solicitations` rows=1264 accepted=17 unique_pop=17 sol_ids=17 — https://data.montgomerycountymd.gov/resource/eeq6-nnwe.json
- `discovered_data_cityofnewyork_us_ww83-bcks` rows=940 accepted=940 unique_pop=479 sol_ids=0 — https://data.cityofnewyork.us/resource/ww83-bcks.json
- `discovered_datacatalog_cookcountyil_gov_ag43-fvd7` rows=564 accepted=564 unique_pop=522 sol_ids=522 — https://datacatalog.cookcountyil.gov/resource/ag43-fvd7.json
- `nj_purchase` rows=286 accepted=286 unique_pop=286 sol_ids=2 — https://www.nj.gov/treasury/purchase/

### Discrepancies

- `dedupe_funnel`: {"metric": "dedupe_funnel", "sum_adapter_accepted": 169798, "unique_plus_duplicates_excluded": 169798, "delta": 0, "explanation": "rows_examined includes closed/mapping rejects; adapter_accepted is Stage-1 keepers; duplicates_excluded is cross-source/external_key dedupe of keepers; unique is post-dedupe discovery population before owner filters."}

Per-source full table: `docs/acceptance_archive/BUILD_002Q_SOURCE_COVERAGE.json` → `provenance_audit.sources`.

## Source probe follow-up

- Integrated rla_bid_openings_results (https://data.brla.gov/resource/u9zk-8nix.json) from probe; continued acquire for this source only (not a full restart).
- Fetched **2708** adapter-accepted rows; registry created/updated recorded in SOURCE_COVERAGE source_probe_followup.
- Probe artifact archived to docs/acceptance_archive/BUILD_002Q_SOURCE_PROBE.json (removed from data/acceptance/).

## Qualification funnel audit

**Audited at:** 2026-11-22T17:14:59.484564+00:00  
**Population:** 23091 unique freeze rows (offline; no acquisition restart).

### Headline counts

| Step | Count |
|------|------:|
| Unique population | 23091 |
| Genuinely active (`open`/`amended`) | 1203 |
| Verified individual solicitation | 750 |
| Product opportunities (pipeline) | 252 |
| Stage C (firm qty fit) | 0 |
| Bid-eligible | 0 |
| Confirmed hard DQ | 22643 |

### Filters in execution order (eliminations)

- **F0 unique_population_freeze** — eliminated `0` — `m3/research/engine.py write_population_file(run.id, unique)`
- **F1 verified_individual_solicitation** — eliminated `22341` — `m3/research/identity.py:verify_record_identity → commercial_criteria.py:evaluate_procurement_pipeline ~L139-149`
- **F2 inactive_status** — eliminated `0` — `commercial_criteria.py ~L151-154 status in closed/cancelled/historical_award/forecast`
- **F3 requirement_class_exclude** — eliminated `282` — `commercial_criteria.py INITIAL_EXCLUDE_CLASSES ~L165-166`
- **F4 it_software_exclude** — eliminated `14` — `commercial_criteria.py IT_MARKERS + it_focus_detected ~L171-187,268-280`
- **F5 confirmed_services_title** — eliminated `6` — `commercial_criteria.py class:services_title_confirmed ~L297-313`
- **F6 product_opportunity** — eliminated `196` — `commercial_criteria.py product_opportunity / INITIAL_PRODUCT_OK ~L179-184,348+`
- **F7 firm_quantity_and_unknown_flags_for_stage_c** — eliminated `252` — `commercial_criteria.py quantity firm_semantics + hard_unknown ~L189-346`
- **F8 stage_c_verified_m3_procurement_fit** — eliminated `n/a` — `commercial_criteria.py STAGE_C return ~L315-346`
- **F9 bid_eligible_stages_d_g** — eliminated `0` — `commercial_criteria.py STAGE_D–G declared but unused; jobs.py cmd_qualify hardcodes 0`

### Product opportunity blockers (not Stage C)

{
  "missing_quantity_extraction": 252
}

### Root cause of ZERO bid-eligible

002P 73 were Stage C (product+firm qty), not bid-eligible. 002Q bid-eligible=0 because stages E/F/G (supplier quote, financing, ready-to-bid) are never populated. Current pipeline Stage C count on population=0; prior 002P rows still Stage C under current rules=65.

002P unique Stage C rows: 73; still Stage C now: 65; lost under current rules: 8.

### Acquire 1,042 verified-product reconciliation

Offline recount of product-class rows in the freeze: **1,019** (≈ acquire 1,042).

| First blocking condition | Count | Type |
|--------------------------|------:|------|
| Not verified individual (listing / unresolved / supporting doc) | 748 | identity — not bid DQ |
| Missing quantity extraction | 252 | missing information |
| IT/software hard DQ | 14 | confirmed DQ |
| Confirmed services title DQ | 5 | confirmed DQ |
| Stage C from freeze | 0 | incomplete freeze fields |

Pricing, financing, and reseller eligibility **do not** appear as first blockers for these rows.

**Why Stage C = 0 on the freeze while 002P had 73:** `write_population_file` drops quantity fields (all 23,091 have `quantity=null`). Re-scoring 002P Stage C evidence with stored quantities: **65/73 still Stage C**; 8 lost to service-title / estimated-qty rules. Bid-eligible remains 0 because stages E–G are unimplemented (`cmd_qualify` hardcodes `bid_eligible=False`).

### Code findings

- `BID_ELIGIBLE_NEVER_ASSIGNED` (defect_or_incomplete_processing) — `m3/research/jobs.py:cmd_qualify` (`bid_eligible` hardcoded False); `m3/research/commercial_criteria.py` STAGE_D–G reserved
  - ZERO bid-eligible by construction even when Stage C > 0
- `QUALIFY_ASSUMES_ALL_INDIVIDUAL` (defect) — `cmd_qualify` forces `verified_individual_solicitation=True` on every registry row
- `POPULATION_FREEZE_DROPS_QUANTITY` (defect) — `m3/research/population_acquire.py:write_population_file` slim field list omits quantity/semantic/evidence
  - Freeze re-qualification cannot reach Stage C; looks like a funnel failure
- `DOCUMENT_ACCESS_NOT_IN_PIPELINE_GATE` (incomplete_processing) — no `package_status` predicate; missing docs → `quantity_unknown` (missing info, not confirmed DQ)
- `SERVICE_TITLE_SOFT_CLEARS_QUANTITY` (restrictive_filter) — `commercial_criteria.py` ~L282–313
- `PRICING_FINANCING_NOT_BLOCKING_DISCOVERY` (by_design) — STAGE_E/F unused; do not eliminate the product-class set

# µLab Integrity Build — Baseline Failure Classification

Build: `20260922-m3-micro-lab-integrity-1`
Prior build: `20260922-m3-micro-lab-pipeline-1`
Baseline suite: 1609 passed / 26 failed (1970s)

## Classification legend

| Field | Meaning |
|-------|---------|
| Pre-existing | Failed on prior pipeline suite |
| µLab-related | Touches µLab integrity/pipeline/UI this build |
| App defect | Real production bug vs WIP pin |
| This build touches | Codepath modified in integrity build |

## The 26 baseline failures

| # | Test | Module | Reason | Pre-existing | µLab | App defect | WIP pin | This build touches |
|---|------|--------|--------|--------------|------|------------|---------|-------------------|
| 1 | test_build_tag_and_app_version | test_m3_action_orchestration | expects `20260922-m3-micro-lab-1` | Y | N | N | Y | N (APP version only) |
| 2 | test_build_pin | test_m3_award_product_projection | same build pin | Y | N | N | Y | N |
| 3 | test_build_tag_and_app_version | test_m3_capital_requirement_gate | same | Y | N | N | Y | N |
| 4 | test_build_tag_and_app_version | test_m3_contract_lifecycle | same | Y | N | N | Y | N |
| 5 | test_build_pin | test_m3_demand_signal | same | Y | N | N | Y | N |
| 6 | test_build_pin | test_m3_discovery_planner | same | Y | N | N | Y | N |
| 7 | test_build_pin | test_m3_dla_clause_extraction | same | Y | N | N | Y | N |
| 8 | test_build_tag_and_app_version | test_m3_economic_learning | same | Y | N | N | Y | N |
| 9 | test_build_pin | test_m3_eligibility_evidence | same | Y | N | N | Y | N |
| 10 | test_build_tag_and_app_version_alignment | test_m3_execution_os | same | Y | N | N | Y | N |
| 11 | test_build_tag_and_app_version | test_m3_human_os | same | Y | N | N | Y | N |
| 12 | test_build_pin | test_m3_intelligence_graph_read | same | Y | N | N | Y | N |
| 13 | test_build_tag_and_app_version | test_m3_intelligence_retrieval | build pin + missing `/api/m3/intelligence-graph/{opportunity_id}` | Y | N | Partial — WIP API | Y | N |
| 14 | test_existing_apis_unchanged | test_m3_market_hunt | missing intelligence-graph route | Y | N | WIP missing API | Y | N |
| 15 | test_build_pin | test_m3_market_hunt | build pin | Y | N | N | Y | N |
| 16 | test_regression_existing_apis… | test_m3_market_hunt_handoff | missing `/api/m3/market-hunt` | Y | N | WIP missing API | Y | N |
| 17 | test_build_pin | test_m3_market_hunt_handoff | build pin | Y | N | N | Y | N |
| 18 | test_build_tag_and_app_version | test_m3_master_record | build pin | Y | N | N | Y | N |
| 19 | test_engines_unchanged_endpoints | test_m3_offer_readiness | missing `/api/m3/offer-readiness/{opportunity_id}` | Y | N | WIP missing API | Y | N |
| 20 | test_build_pin | test_m3_offer_readiness | build pin | Y | N | N | Y | N |
| 21 | test_build_pin | test_m3_research_queue | build pin | Y | N | N | Y | N |
| 22 | test_build_pin | test_m3_source_qualification | build pin | Y | N | N | Y | N |
| 23 | test_build_tag_and_app_version | test_m3_strategic_intelligence | build pin | Y | N | N | Y | N |
| 24 | test_build_tag_and_app_version | test_m3_supplier_capital_ops | build pin | Y | N | N | Y | N |
| 25 | test_build_pin | test_m3_supplier_product_graph | build pin | Y | N | N | Y | N |
| 26 | test_build_tag_and_app_version | test_m3_supply_intelligence | build pin | Y | N | N | Y | N |

## Decision

- **Not fixed in this build** — intentional unfinished WIP modules outside µLab integrity scope.
- Expected post-build: same 26 failures (assertions will show `integrity-1` instead of `pipeline-1`; failure class unchanged).
- µLab-adjacent pins in automation / federal_dla / product_resale_discovery updated to `integrity-1` (were already advanced to pipeline-1).

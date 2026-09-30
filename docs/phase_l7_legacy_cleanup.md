# Phase L.7 — Legacy Cleanup

**Build:** `20260927-m3-phase-l7-quote-outreach-readiness`

## Canonical funnel (sole live path)

broad discovery → Stage 0 cheap hard rejection → Stage 1 permissive triage → Stage 2 identity/acquisition enrichment → Stage 3 economics → deep validation → **quote readiness** → owner approval → (later) supplier outreach → final economics → compliance/bid readiness

Entry points:

- Funnel: `phase_l.progressive_funnel.run_progressive_stages_cheap`
- Lanes: `phase_l.acquisition_lanes.classify_acquisition_lane`
- Economics: `phase_l.quote_economics.evaluate_quote_opportunity`
- Readiness: `phase_l.quote_readiness.evaluate_quote_readiness`

## Obsolete rule IDs (inactive on live path)

- `LEGACY_STAGE1_EXACT_IDENTITY_REQUIRED`
- `LEGACY_STAGE2_EXACT_MPN_GATE`
- `LEGACY_FIXED_DEEP_RESEARCH_COUNT_CAP`
- `LEGACY_STAGE3_ROW_COUNT_CAP`
- `LEGACY_NO_PRICE_INFORMATION_AS_DEAD_END`
- `LEGACY_QUOTE_REQUIRED_EQUALS_FAILURE`
- `LEGACY_AGENCY_WIDE_BUYER_MEDIAN`

## Status migrations

| Old | New semantic |
|-----|----------------|
| `NO_PRICE_INFORMATION` | `PUBLIC_PRICE_NOT_FOUND_OR_QUOTE_REQUIRED` |
| `PRICE_NOT_AVAILABLE` | `PUBLIC_PRICE_NOT_FOUND_OR_QUOTE_REQUIRED` |
| `LEGACY_STRICT_STAGE2_FAIL` | `L5_PERMISSIVE_ADMISSION_COUNTERFACTUAL_ONLY` |

## Duplicate logic reconciled

- Cap flags: `source_roles` now **re-exports** `STAGE3_NO_ROW_CAP` / deep / manual caps from `acquisition_lanes` (single source of truth)
- Lane classifier: only `acquisition_lanes.classify_acquisition_lane`
- Agency-wide buyer median: removed in L.6; documented obsolete here

## Dead code / compatibility retained

Historical `l21`–`l29`, `l3`–`l6` rescue runners retained for telemetry/replay — **not** invoked by L.7 live runner. Stored-data compatibility kept.

`NO_PRICE_INFORMATION` remains a public-price failure class in `acquisition_pricing` / product-detail paths; it is **not** a quote-required dead end.

`legacy_strict_would_pass` retained as Stage 2 counterfactual telemetry only.

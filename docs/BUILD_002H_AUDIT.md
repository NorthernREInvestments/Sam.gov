# Build 002H Pre-Implementation Audit

**Production at start:** `20261121-m3-2-build-002g`  
**Sources:** BUILD_002G_REPORT / ACCEPTANCE, BUILD_002F_ROOT_CAUSE_AUDIT, live DB samples

## Why 260 broad product → 0 strict M3 fits

Evidence from `BUILD_002G_ACCEPTANCE.json` evidence manifest (120 samples) + re-run of `apply_initial_m3_criteria`:

| Outcome | Count | Dominant cause |
|---------|------:|----------------|
| unknown | 116 | **`quantity_unknown` only** |
| not_eligible | 4 | IT focus / rental / significant installation |

**Root cause category: H — Quantity extraction failure (software-controlled), not genuine incompatibility.**

The 002G commercial gate required a populated `quantity` field for `verified_initial_fit`. Adapters almost never populate quantity from open-data/HTML listings. Package document excerpts were not mined for quantity schedules before qualification. Result: every otherwise product-class individual solicitation stayed `unknown`.

Secondary issues:

| Code | Finding |
|------|---------|
| C | Many Level 0–1 HTML portals never resolve to detail pages |
| D/E | RAMP dynamic shell → no public attachments (external + software labeling fixed in 002G) |
| F/G | Scanned PDFs return thin text; no OCR fallback |
| J | Unknown status often left unresolved after listing harvest |
| A | Mesa Open rows empty; NYC/Cook correctly non-solicitation |

## Failure split (002G population)

- **Software-controlled (fixable):** quantity not extracted from docs/titles; progressive package not run on all product individuals; OCR absent; Delaware public details under-used for packages.
- **Genuine external:** RAMP attachments need vendor session; Mesa HTML 403.
- **Genuine incompatibility:** rental/install/IT subset (small).
- **Not solicitations:** forecasts/open-data already excluded by 002G identity (correct).

## 002H fix plan

1. Separate pipeline stages A–G (procurement fit ≠ supplier quote ≠ ready to bid).
2. Deterministic quantity/line-item extraction from titles, HTML, PDF/XLSX text; OCR fallback when pdf text thin.
3. Re-enrich from existing COMPLETE/PARTIAL package excerpts before declaring unknown.
4. Activate high-yield public detail platforms (Delaware MMP, Montgomery, NJ PDFs, LA RAMP metadata).
5. Owner UI: surface procurement-fit vs investigation vs restricted — not junk.
6. Full-population reprocess + fresh run; report honest yields.

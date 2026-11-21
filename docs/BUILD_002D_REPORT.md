# Build 002D Report — Complete Solicitation Package Acquisition

**Build ID:** `20261121-m3-2-build-002d`  
**Baseline:** Build 002C deployed  
**Status:** Implementation complete; acceptance metrics filled from live harness (`docs/BUILD_002D_ACCEPTANCE.json`)

## Root causes (002C → ~204 attachments)

See `docs/BUILD_002D_AUDIT.md`. Primary causes: generic listing URLs stored as `official_url`, L3 capped at 2 attachments and gated on pre-discovered candidates only, no persistent document manifest, no shared package engine.

## Architectural improvements

1. **One shared engine** — `m3/research/package.py` (`acquire_solicitation_package`) used by recovery L3, saved opportunities, discovery detail, and manual URL/upload import.
2. **Models** — `SolicitationPackage`, `PackageDocument`, `PackageAcquisitionJob` with create_all (no destructive resets).
3. **URL resolution** — `classify_url_kind` + listing→detail resolution by solicitation number/title; rejects generic boards as authoritative packages.
4. **Manifest honesty** — identified vs downloaded vs missing; 10/12 reports PARTIAL, never COMPLETE.
5. **Original file storage** — content-hash dedupe under `M3_DATA_ROOT/package_blobs`; safe ZIP extract limits.
6. **Extraction** — PDF/DOCX/XLSX/CSV/HTML/text; download success ≠ extraction success.
7. **Package states** — NOT_ACQUIRED → COMPLETE / PARTIAL / ACCESS_RESTRICTED / VERIFICATION_REQUIRED / FAILED / UPDATE_AVAILABLE.
8. **Amendments** — detect amendment/addendum links and flag documents; supersession only with evidence.
9. **Platform patterns** — successful hosts cached in `ResearchCache` (`platform_acquisition_pattern`).
10. **UI** — opportunity detail: package status, counts, acquire/refresh/retry/upload, per-file and ZIP download.
11. **Recovery** — L3 uses package engine for any uncertain item with an HTTP official URL (not only attachment candidates).
12. **Budgets** — free acquisition continues with OpenAI portal discovery off; existing $1/$30 policy preserved.

## Before / after (live harness)

Harness bounds: L3 packages=100, extra=50, max_pages=6, OpenAI portal off ($0).

| Metric | 002C | 002D harness |
|--------|------|----------------|
| Discovery population (free) | ~9,799 | 8,204 |
| Verified product (active) | ~1,020 | 639 |
| Packages acquired | 0 | 150 |
| Identified documents | — | 59 |
| Downloaded documents | 204 investigated (transient) | 54 durable |
| Missing documents | — | 5 |
| Extracted | — | 51 |
| Doc acquisition rate | — | 91.5% of identified |
| COMPLETE (pre-tighten) | — | 93 (many zero-doc; rule tightened before deploy) |
| FAILED | — | 54 |
| VERIFICATION_REQUIRED | — | 3 |
| OpenAI spend | — | $0.00 |

Representative packages (≥50) are listed in `BUILD_002D_ACCEPTANCE.json`.

Platform highlights: Mesa Socrata (`discovered_citydata_mesaaz_gov_dfcn-ivuc`) 48/48 downloads; Winnipeg generic_http mostly FAILED (no usable solicitation package URL).

## Remaining limitations

- Generic listing URLs without solicitation-number match stay `VERIFICATION_REQUIRED`.
- Auth-walled portals (Bonfire/OpenGov/BidNet parked) surface `ACCESS_RESTRICTED` for owner action — no auto-registration.
- Scanned PDFs may download but fail extraction without OCR runtime.
- Full national inventory verification depends on source exposing a reliable attachment list; otherwise completeness is not claimed.

## Acceptance gates

Filled in `docs/BUILD_002D_ACCEPTANCE.json` after harness + Railway deploy.

## Owner acceptance

Await owner acceptance. Do not begin Build 003.

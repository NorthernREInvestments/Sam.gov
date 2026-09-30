# Phase L.2.2 — Price Evidence Audit Sample

Manual review of resolver accept/reject behavior.

## Accepted prices (target ≥10)

**None in final live run** (0 verified economics-eligible hits). Accuracy prioritized over count.

## Rejected / noisy candidates (≥10)

| # | Opportunity / product | Target MPN/model | Candidate URL | Page type | Observed identity | Observed price | Accept/Reject | Reason |
|---|---|---|---|---|---|---|---|---|
| 1 | Switch Pressure F110 | `1274M99P01` | `https://www.cdw.com/product/getac-f110-...` | LIKELY_PRODUCT_PAGE | Getac F110 tablet | ~$2982.99 | **REJECT** | MPN expected/absent; short model F110 without Eaton match → `NO_MATCH` / not economics-eligible (post-fix) |
| 2 | Valve assembly | `1266M27P09` | (discovery) | — | — | — | REJECT | `FETCH_FAILED` |
| 3 | AH-64 insulation | `7-511141641-059` | — | — | — | — | REJECT | `FETCH_FAILED` |
| 4 | F-16 phase shift driver | `562R218H01` | — | — | — | — | REJECT | `FETCH_FAILED` |
| 5 | Spraybar | `37D401750P104` | — | — | — | — | REJECT | `FETCH_FAILED` |
| 6 | Speed sensor | `SK2000DAP-300` | — | — | — | — | REJECT | `FETCH_FAILED` |
| 7 | Blade burn pod | `12-1012-01` | — | — | — | — | REJECT | `FETCH_FAILED` |
| 8 | Guardrail PN 355 | `355` | — | — | — | — | REJECT | `FETCH_FAILED` / weak identity |
| 9 | Pump subassembly | (NSN-oriented) | — | — | — | — | REJECT | `NO_EXACT_IDENTITY` (NSN-only skipped for commercial L.2.2) |
| 10 | Oil pump assembly | (NSN-oriented) | — | — | — | — | REJECT | `NO_EXACT_IDENTITY` |
| 11 | Generic search listing | any | retailer `/search?...` | SEARCH_RESULTS_PAGE | — | any `$` | REJECT | `SEARCH_PAGE_ONLY` (unit-tested) |
| 12 | Homepage root | any | `https://www.grainger.com/` | HOMEPAGE | — | — | REJECT | not product evidence (unit-tested) |

Artifact mirror: `artifacts/phase_l/l22_price_evidence_audit.json`

## Precision note

Interim false accept (Getac) is documented above as **reject after rule fix**. Final live verified set size = 0 → no false accepts in the locked run. Unit tests assert the Getac/F110 case cannot enter economics.

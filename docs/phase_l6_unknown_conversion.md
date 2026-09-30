# Phase L.6 — Unknown Lane Conversion

Cheap acquisition-path pass on Stage 3 `UNKNOWN_ACQUISITION_CHANNEL` rows.

## Live result

| Metric | Count |
|--------|------:|
| Unknown before | 198 |
| Converted | **31** |
| Unknown after | 167 |

Converted primarily into `QUOTE_REQUIRED_COMMERCIAL` when commercial score ≥ 30 or manufacturer/model present.

## Target lanes

commercial distributor · quote-required commercial · commercial open · mil-spec open · specialty · source-approval · true unknown

Does not force distributor/open into quote-required when public acquisition evidence exists.

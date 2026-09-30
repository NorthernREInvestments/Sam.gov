# Phase L.5 — Attrition Audit

## L.4 Stage 1 → Stage 2 cliff

| Metric | Count |
|--------|------:|
| Stage 1 | 1,617 |
| Stage 2 (legacy) | 102 |
| Rows lost | **1,515 (93.7%)** |

## Top loss reasons (among dispositions / buckets)

| Reason | Count |
|--------|------:|
| no exact MPN/model (identity incomplete) | 1,167 |
| weak product identity | 185 |
| deadline (pre-Stage1 / Stage0) | 171 |
| true non-product | 161 |
| descriptive specification (false negative) | 159 |
| advanced under legacy | 102 |
| brand-or-equal (false negative) | 4 |

**Root cause:** Stage 2 required NSN/MPN/regex-model anchors. State/local descriptive commercial titles failed as `no_identity_anchor` even when commercially researchable.

# Phase L.3 — Original Source Audit

Every opportunity resolves:

- `DISCOVERY_SOURCE` (may be aggregator)
- `AUTHORITATIVE_SOLICITATION_SOURCE` (bid submission truth)

If authoritative URL missing/unverified:

`ORIGINAL_SOLICITATION_SOURCE_UNVERIFIED` → final bid readiness false.

Module: `phase_l/original_solicitation.py`

## Live Stage 3

| Metric | Count |
|--------|------:|
| Authoritative sources resolved | 81 |
| Unresolved | 0 |

Submission-path checklist attached per row; final readiness still requires full amendment/deadline verification before any bid.

# R1.1 Document Package Completeness

**Module:** `response_engine/package_completeness.py`

## Flag

`document_package_complete` is **true** only when:

- base solicitation (or equivalent controlling docs) present
- known referenced material documents accounted for
- no unresolved material missing references
- not auth/fetch blocked without docs

If uncertain → false / review status.

## Statuses

| Status | Meaning |
|--------|---------|
| `COMPLETE` | Package accounts for required referenced material |
| `PARTIAL` | Some docs present; gaps or amendment uncertainty |
| `MISSING_REFERENCED_DOCUMENT` | e.g. “Attachment C” cited but absent |
| `AUTH_REQUIRED` | Authoritative portal needs login |
| `FETCH_BLOCKED` | Anti-bot / fetch blocked |
| `PARSE_REVIEW_REQUIRED` | Low confidence / OCR material |
| `UNKNOWN` | Insufficient information |

## Reference discovery

Patterns include Attachment/Exhibit/Appendix/Schedule, Pricing/Cost Sheet, Technical Specifications, Q&A, Bidder Response Form, Amendment/Addendum.

Missing refs create `REFERENCED_DOCUMENT_MISSING` hard blocks and prevent `DOCUMENTS_READY`.

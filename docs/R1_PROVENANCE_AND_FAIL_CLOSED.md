# R1 — Provenance and Fail-Closed

## Provenance

Every extracted fact stores document ID, location, excerpt, extractor, confidence, timestamp.

LLM proposals (future) = `PROPOSED_REQUIREMENT` until source-bound validation. LLM confidence alone never creates PASS.

## Fail-closed

| Situation | Result |
|-----------|--------|
| Country of origin unknown | UNKNOWN + material unresolved + block |
| Exact brand mismatch | FAIL |
| Generic “equal” claim | REVIEW_REQUIRED (not PASS) |
| Required signature missing | Hard block |
| Timezone unresolved on deadline | Hard block |

## Clean-room firewall

Namespaces: INTERNAL_EVIDENCE · VERIFIED_RESPONSE_FACTS · GOVERNMENT_SUBMISSION_CONTENT

Supplier “pricing subject to change” stays INTERNAL.

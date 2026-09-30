# R1.2 DLA Master Store

**Module:** `response_engine/master_store.py`  
**Path:** `data/shared_master_documents/`

Versioned immutable masters keyed by authority + title + version + hash.

Projects attach via `master_references` + `MASTER_FOR` graph edge without duplicating bytes.

Unresolved applicability → `MASTER_VERSION_REVIEW_REQUIRED`.

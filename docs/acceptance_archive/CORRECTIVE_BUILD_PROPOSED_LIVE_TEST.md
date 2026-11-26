# Proposed live retrieval test (DO NOT EXECUTE without owner auth)

Zero spend in corrective build. Smallest meaningful live validation of the repaired path.

## Cap

- NextBid: **≤ 3 credits** total  
- OpenAI application: **$0** until separately authorized  
- Prefer reuse of stored `list_documents` (no re-list when durable store hit)

## Test A — Propane product PDF (highest value)

| Field | Value |
| --- | --- |
| Solicitation | **NB00375309** |
| Agency | (from stored opportunity) |
| Existing evidence | Paid `list_documents` with `source_documents` (12 inventoried); prior failed/wrong download attempt; local body text may exist in full-text store |
| Exact missing | Original bytes for **Bulk_Propane_Fuel_Supply.pdf** (sha256 `996c4913…`) — not Specimen Contract |
| Why necessary | Product/supply requirements; proves correct document selection + byte save + association |
| API ops | Reuse cached list_documents (0). `get_document_download` for Bulk Propane hash (1). Object-store GET (non-metered). Optional: skip if free `source_url` works (0) |
| Credit max | **1** (2 if list cache miss — should not) |
| Expected saved | Validated PDF under `nextbid_original_documents/NB00375309/`, inventory → DOWNLOADED, package association |
| Next analysis | Offline extract + (later) authorized AI |
| Success criteria | Correct filename selected; PDF magic; checksum match if provided; associated; inventory status DOWNLOADED; Specimen not chosen |

## Test B — Manifest reuse without repurchase

| Field | Value |
| --- | --- |
| Solicitations | **NB00286337**, **NB00375193** |
| Existing | Stored manifests; inventories rebuilt offline (3 and 4 docs) |
| Missing | Original bytes for highest-scoring product/spec docs |
| Ops | Cache-hit list (0) + one `get_document_download` each if still open/suitable |
| Credit max | **2** combined |
| Success | Bytes saved; no second list_documents charge |

## Explicitly out of scope for first live shot

- Broad 30-opportunity document campaign  
- New OpenAI live comprehension  
- BidNet enablement  
- Re-get_opportunity for all 82  

## After live success (separate auth)

1. Extract text from saved PDFs  
2. Re-run `verify_analysis_against_sources`  
3. Optionally authorize bounded OpenAI on assembled evidence only  

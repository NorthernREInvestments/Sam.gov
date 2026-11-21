# Build 002D — Pre-implementation Audit (from Build 002C)

## Why ~9,799 discoveries produced only ~204 attachment investigations

| Root cause | Evidence | Effect |
|------------|----------|--------|
| **Generic listing URLs stored as `official_url`** | Acceptance samples: `procuretap.com/bids/{state}`, `das.nebraska.gov/.../bid-opportunities.html` | No solicitation-specific attachment inventory |
| **L3 only for still-uncertain with candidates** | `recovery.py` Level 3 gated | Product/classified rows never entered attachment acquisition |
| **Hard cap `max_attachments=2`** | `_level3_attachments(..., max_attachments=2)` | Even when candidates exist, max 2 fetched |
| **L3 attempted 204 / resolved 18** | BUILD_002C_ACCEPTANCE | Attachment path was a recovery side-channel, not package acquisition |
| **No persistent document manifest** | No package tables/files in 002C | Downloads not durable; no 10/12 completeness accounting |
| **Socrata `official_url` often dataset/listing** | Adapter fallback to dataset URL | Not a solicitation detail page |
| **Detail fetch treated PDFs as HTML until fixed** | NUL-byte cache failures | Deferred to L3 without full inventory |
| **No platform resolvers** | HTML harvest is link-only | Cannot walk listing → solicitation → attachments |

## URL quality buckets (qualitative from 002C samples + recovery blockers)

| Bucket | Typical pattern | Classification impact |
|--------|-----------------|----------------------|
| Solicitation-ish document URL | `.pdf` direct | Deferred to L3; not full package |
| Generic state/city bid board | `/bid-opportunities`, `/bids/{state}` | Thin title only → unknown_mixed |
| Socrata resource / portal home | `resource/*.json`, RAMP home | Partial fields; rare attachments |
| HTTP 403/404 | recovery blockers | Marked inaccessible; not re-routed |
| True detail with attachment links | Minority | Only path that yielded L3 work |

## What 002C did not provide

- Authoritative solicitation identity resolution  
- Complete attachment inventory verification  
- Original file preservation + content-hash dedupe  
- Package completeness states (COMPLETE/PARTIAL/…)  
- Amendment/version tracking  
- Shared engine for saved + discovery + manual import  
- UI to inspect/download acquired originals  

## Design implication for 002D

Build **one** `acquire_solicitation_package` engine that:
1. Resolves or rejects generic listing URLs  
2. Builds a document manifest before claiming completeness  
3. Downloads and extracts originals under `M3_DATA_ROOT`  
4. Feeds evidence back into classification without Stage 1 product gates  

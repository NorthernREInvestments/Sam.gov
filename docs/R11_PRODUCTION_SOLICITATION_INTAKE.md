# R1.1 Production Solicitation Intake

**Build:** `20260929-m3-r11-production-intake-legacy-cutover`  
**Module:** `response_engine/production_intake.py`

## Purpose

Bridge R1’s solicitation compiler to **real package files** so Bid Prep does not require manual solicitation-text paste for supported cases.

```
M3 opportunity → local/authoritative package → parse → document graph
→ atomic requirements → R1 compliance matrix → Bid Prep UI
```

## Entry points

| Action | API / function |
|--------|----------------|
| START BID PREP | `POST /api/response-projects/from-opportunity/{id}` → `start_bid_prep_production` |
| Intake | `POST /api/response-projects/{id}/intake` |
| Refresh | `POST /api/response-projects/{id}/refresh` |
| Manual upload | `POST /api/response-projects/{id}/documents/upload` |
| Status | `GET /api/response-projects/{id}/intake-status` |

## Authoritative source order (R1.1)

1. Local already-stored artifacts (`artifacts/transactional_procurement_evidence/`, known PDFs)
2. Operator manual upload (`OWNER_UPLOADED_FROM_BUYER`)
3. Optional public non-SAM URL fetch (`try_url_fetch=true` only)
4. Never consume SAM 10-call/day budget for intake

Discovery pages are **not** treated as authoritative controlling sources.

## Package completeness

`document_package_complete` is true only when base solicitation is present, referenced material docs are accounted for, and no material missing references remain. Otherwise statuses include `PARTIAL`, `MISSING_REFERENCED_DOCUMENT`, `AUTH_REQUIRED`, `FETCH_BLOCKED`, `PARSE_REVIEW_REQUIRED`.

## Operator readiness ceiling

Highest R1.1 claim: **`READY_FOR_RESPONSE_BUILD` / `SOLICITATION_COMPILED`**. Never `READY TO SUBMIT` / `BID READY`.

## Zero SAM

`sam_api_calls` on intake results is always `0`. SAM notice URLs are recorded as `AUTH_REQUIRED` / manual retrieval, not fetched via API.

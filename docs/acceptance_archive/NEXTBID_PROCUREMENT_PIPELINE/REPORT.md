# M3 NextBid Procurement Pipeline — Acceptance (Offline Preflight)

**Commit (pre):** 76315c7bc7a951de0791eb115dbdfc92dd95a12e  
**API expenditure:** **ZERO**  
**Live provider call:** **NO**  
**Killswitch:** LOCKED  
**Ledger:** used 3454 / remaining 2546 / reserve 1000

## 1. Selected test opportunity

- **NB00375309** — Bulk Propane Fuel Supply (City of Pembroke Pines / OpenGov)  
- Solicitation: **CS-2026-QQ-02**  
- Deadline retained: 2026-11-30T17:00:00.000Z  
- Product rationale: on-demand bulk LP delivery to existing 5,000-gal tank (tangible goods).  
- **Rejected** NB00321439 courtroom furniture: listing is remove/build/install (construction).

### Listing evidence before retrieval
- Description chars: **279**  
- Already paid endpoints: ['get_opportunity', 'list_documents', 'list_files', 'read_file', 'search']  
- Source document metadata count: **10** (PDFs stored=true = metadata only, not local bytes)  
- document0.md retrieved locally: **False**

### Missing fields
- full_notice_body_document0_md_not_retrieved
- firm_ordered_or_estimated_propane_quantity_UNKNOWN
- unit_price_basis_UNKNOWN
- delivery_schedule_terms_beyond_listing_UNKNOWN
- technical_specs_from_Bulk_Propane_Fuel_Supply.pdf_not_retrieved_as_bytes
- submission_method_detail_UNKNOWN
- eligibility_set_aside_UNKNOWN
- live_open_status_UNVERIFIED

## 2–5. Storage + cheapest path + offline extract

Persistence preflight: **PASS** (complete raw_text, content hash, original_bytes_sha256, reload after restart, thicker-wins, body retained on extract failure).  
Truncation bug fixed: non-JSON 
aw_text no longer capped at 50k in controller.

Cheapest justified next call (not executed):
- Endpoint: **read_file**
- Path: nb2/published/opengov/document0/NB00375309/NB00375309.document0.md
- Max cost: **1 credit**
- Skips: get_opportunity, list_documents, list_files (indexes already retained)

### Offline owner card (listing only)
- PRODUCT CONFIRMED
- REQUIREMENTS MISSING
- SOURCE RETAINED
- SUPPLIER QUOTE PARTIAL
- BID STATUS UNVERIFIED

## 6. Quote-ready reconciliation (2 vs 10)

- 14-audit YES (**2**): strict manual bar on pricing_now only (c565006).  
- Population YES (**10**): automated thresholds on 8,088 inventory rows (76315c7).  
- **Not contradictory** — different universes/bars. Do not inflate the trustworthy set.

## 7. ONE-CALL APPROVAL (awaiting owner)

| Field | Value |
|---|---|
| Opportunity | NB00375309 |
| Endpoint | read_file |
| Artifact | nb2/published/opengov/document0/NB00375309/NB00375309.document0.md |
| Max credits | 1 |
| Remaining | 2546 |
| Reserve | 1000 |
| Gate | OWNER_EXPLICIT_AUTHORIZATION_REQUIRED |

**DO NOT EXECUTE** until owner authorizes this single-use request. Killswitch stays locked.

## Success checkpoint

Pipeline code + persistence proofs + offline processing + approval package are complete with **zero metered calls**. Live one-call proof is a separate, owner-gated step.

**STOP FOR OWNER REVIEW.**

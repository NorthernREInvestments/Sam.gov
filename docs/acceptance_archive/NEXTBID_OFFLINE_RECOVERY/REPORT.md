# M3 NextBid Offline Recovery Report

**Verdict:** Entire retained paid NextBid inventory (450 `by_request` bodies → 7,954 NB packages) was reprocessed offline with zero API calls. Parser repairs for ignored `raw_text` and `source_documents` are applied on the load path. **Pricing-now did not increase** — recovered document0 markdown is notice text; firm qty/specs still sit in NextBid-stored originals not yet retrieved as bytes.

| Field | Value |
|---|---|
| Git (pre-commit) | `8db85397900eb9e6388211749d35dc131575c7f5` |
| API expenditure | **ZERO** |
| Recovery runtime | 15.78s |
| Staged inventory rebuild | 103.1s, `external_calls=0`, fresh evaluations=7 |
| Ledger | used 3,454 / remaining 2,546 / reserve 1,000 (unchanged) |

## BEFORE / AFTER owner inventory

| Metric | Before | After | Delta |
|---|---:|---:|---:|
| all_active_products | 6758 | 6757 | -1 |
| potential_products | 5715 | 5715 | 0 |
| requirements_incomplete | 174 | 173 | -1 |
| access_required | 6574 | 6574 | 0 |
| pricing_now | 14 | 14 | **0** |
| submission_verified | 0 | 0 | 0 |
| needs_investigation | 2339 | 2339 | 0 |
| ready_to_bid | 0 | 0 | 0 |

## Exact recovery counts

1. Retained NextBid responses inspected: **450**
2. Previously ignored Markdown bodies recovered: **5**
3. Source characters recovered: **1,032,770** (markdown 26,579 + descriptions 979,617 + document text 26,574)
4. Attachment references recovered: **52** (`source_documents` = 38)
5. Original document **bodies** available locally: **5** (markdown/text only). `stored=true` PDFs are **metadata-only** — not claimed as read.
6. Product descriptions on rich NBs: **11**
7. Opportunities gaining **verified ordered quantities**: **0** (one parser candidate `2 EA` on NB00372858 rejected — narrative “two (2) printers”, not supply qty)
8. Gaining specifications from retained text: **0**
9. Gaining complete line-item schedules: **0**
10. Gaining submission instructions: **3** (NB00350518, NB00339409, NB00360977)
11. Promoted into PRICING NOW: **0**
12. Remaining incomplete (owner file): **173**
13. Still requiring paid retrieval (category C rich set): **2** (NB00321439, NB00375309) plus PDF bytes for NBs whose notice already says specs are in the project PDF

Endpoint mix in paid store: search 416, read_file 11, list_files 7, list_documents 7, get_opportunity 9.

## Validation

- Every retained `raw_text` markdown body is recoverable via `nextbid_file_text` (0 still ignored).
- Markdown fidelity: original == recovered after strip (trailing newline only).
- `source_documents` merged into attachments on load; never treated as PDF bytes.
- No duplicated spend; killswitch locked; staged rebuild `external_calls=0`.
- Semantic accuracy is **not** claimed merely because records processed — see `EXTRACTION_RESULTS.json`.

## What we actually own now

Five document0 `.md` notices were previously dropped (`ok:false` + `raw_text`). They are now in `solicitation_full_text` and staged `document_text`. Example (NB00360977 Dewalt Fans) explicitly states full structured spec is inside the project PDF — so offline recovery correctly surfaces the gap rather than inventing quantities.

## Remaining paid gaps (category C) — do not auto-download

| Priority | NB | Why | Cheapest next call | Est. credits |
|---|---|---|---|---:|
| 1 | **NB00375309** Bulk Propane | Only JSON sidecar retained; `.md` never read | `read_file` …/NB00375309.document0.md | 1 |
| 2 | **NB00321439** Courtroom Furniture | 18 `source_documents` stored=true; no local bytes; `.md` never read | `read_file` …/NB00321439.document0.md | 1 |
| 3 | NB00360977 Dewalt Fans | Notice recovered; specs in `Outright_Purchase_of_Dewalt_Fans_and_Accessories.pdf` | Owner/OpenGov PDF or future NextBid original-bytes path (not auto) | TBD |

Full gap export: `PAID_GAPS.json`.

## Recommended ONE-call test (if owner authorizes later)

**NB00375309** `read_file` on `nb2/published/opengov/document0/NB00375309/NB00375309.document0.md` — expected **1 credit**. Rationale: product propane supply, list_documents already paid, markdown never retrieved (only classification JSON).

Do **not** spend on the 18 courtroom PDFs until document0.md is evaluated.

## Evidence preservation

- `nextbid_paid_responses/by_request` **unchanged**
- Additive packages: `govtracker/data/nextbid_offline_recovery/`
- Full-text thickened only when recovered text longer
- Auditable export: this directory (`REPORT.json`, `EXTRACTION_RESULTS.json`, `PAID_GAPS.json`, `RICH_RECOVERED_NBS.json`)

## Code changes

- `m3/research/nextbid_offline_recovery.py` — full `by_request` walk + packages
- `m3/research/nextbid_full_inventory_audit.py` — `gather_original_evidence` loads recovery
- `m3/research/staged_inventory.py` — merges `source_documents` into attachments
- `m3/research/nextbid_acquire.py` — `nextbid_file_text` + list_documents fields (prior repair)
- Tests: `tests/test_nextbid_offline_recovery.py` (+ evidence-gap suite)

**STOP FOR OWNER REVIEW.** No acquisition run. No deploy. No schedule enable.

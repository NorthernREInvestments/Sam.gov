# BUILD 002O — Document Recovery

**Metered NextBid calls:** 0 (LOCKED)

## Truncation finding (500 characters)

NextBid search/list `summary` field is provider-capped at 500 characters. Slim checkpoints previously stored that summary as description and dropped full document_text. Build 002O stores full extracted text under data/solicitation_full_text/ and prefers package excerpts over listing summaries.

Stats: `{"full_text_le_500": 238, "no_text": 102, "listing_summary_500": 276, "full_text_gt_500": 102}`

Full text now stored under `data/solicitation_full_text/`. UI/listing previews may remain 500 chars; extraction uses full package/document text.

## Free public recovery

- Processed URLs: `483`
- Stats: `{"total": 483, "COMPLETE": 8, "PARTIAL": 10, "ACCESS_RESTRICTED": 237, "VERIFICATION_REQUIRED": 193, "FAILED": 35, "UPDATE_AVAILABLE": 0, "ACQUISITION_IN_PROGRESS": 0, "NOT_ACQUIRED": 0, "errors": 0, "newly_product": 0, "newly_resolved": 0, "identified_docs": 114, "downloaded_docs": 100}`
- Metered NextBid: `0`

## Package outcomes (post-002O deferred set)

- Complete: `10`
- Partial: `14`
- Restricted: `442`

Restricted sources were not bypassed. Public alternatives attempted via official_url only.

# BUILD 002Q — NextBid Data Loss Investigation & Repair

Finished: 2026-11-23T00:29:04.635910+00:00 · Runtime: 74.5s · Metered delta: **0** · OpenAI: **$0**

## Verified root cause (multi-factor)

1. **Paid detail/document/file responses not retained in local HTTP cache** (B/H) — Dashboard get_opportunity=958 read_file=958 but cache has detail=4 read_file=0
   Impact: Cannot offline-replay most paid document bodies from cache

2. **Checkpoint slim persistence dropped document_text; recovery ignored solicitation_full_text** (B) — full_text_store had 623 files / 577 usable but pipeline usable packages reported as ~67 from package excerpts only
   Impact: Paid/extracted text existed on disk but was not loaded into qualification

3. **Free government URL fetch stored portal HTML/CSS as package documents** (F/E) — blob_magic={'html': 33, 'office': 35, 'pdf': 22, 'other': 9}; ACCESS_RESTRICTED=237
   Impact: Wrong files indexed; packages marked restricted/unusable

4. **Listing summaries provider-capped (~500 chars) used as description** (D) — enriched descriptions max ~500; 1863 listings have summary/description only
   Impact: Thin text insufficient for quantity extraction without documents

5. **Local controller ledger only records ~300 metered commits vs provider used 3354** (H) — ledger={'exists': True, 'lines': 650, 'metered_flagged_rows': 0, 'free_rows': 650, 'ops': {'usage': 29, 'free': 21, 'reserve': 300, 'commit': 300}, 'paths': {'search': 296, '/usage': 12, '/account/activity': 6, 'get_opportunity': 4, '/account': 1, '/activity': 1, '/tradelines': 1}, 'note': 'Local ledger under-counts pre-controller / remote (Railway) spend'}; usage used=3354
   Impact: Cannot fully reconcile every historical request to a local artifact

6. **Duplicate search pages in cache (296 search responses, 6359 unique IDs seen in cache)** (G) — search cache files=296; unique=6359
   Impact: Discovery spend duplicated across overlapping queries/pages

## Spend reconciliation

- Provider used credits: **3354** (remaining 2646)
- Owner dashboard ops sum: **3355** (search/detail/docs/files)
- Local ledger metered commits: **0** (incomplete vs provider)
- Free activity attribution: **unavailable** (free /account/activity returned ok=false / no usable rows; cannot attribute dashboard calls to IDs)

## Retention vs paid ops

| Metric | Count |
|---|---:|
| Unique IDs in search cache | 6359 |
| Unique inventory retained | 2255 |
| Usable full_text before → after | 577 → 1777 |
| Usable package collect before → after | 71 → 71 |
| get_opportunity dashboard − cache | 954 |
| read_file dashboard − cache | 958 |
| Portal/HTML wrong blobs | 33 |
| Verified quantities after | 22 |
| Ready supplier pricing / bidding | 22 / 0 |

## Responsibility buckets (inventory)

`{"A_nextbid_or_gov_text_retained": 1777, "B_provider_info_discarded_or_not_persisted": 96, "H_historical_response_unavailable": 8, "D_incomplete_listing_only_no_detail_in_checkpoint": 309, "B_listing_summary_not_promoted_to_full_text": 65}`

## Defects fixed this pass

- Offline recovery now loads solicitation_full_text into reprocessing path
- Listing/enriched descriptions promoted into full_text_store when richer
- PDF/DOCX package blobs re-extracted; portal HTML packages demoted from success
- Pipeline repair re-run over all 2255 after recovery

## Cannot establish

- Per-request attribution of all 3354 provider credits to opportunity IDs (activity API empty)
- Body content of most paid read_file/get_opportunity responses (not in local HTTP cache)
- Whether Railway/production workers retained separate response archives outside this data_root

Awaiting owner acceptance.

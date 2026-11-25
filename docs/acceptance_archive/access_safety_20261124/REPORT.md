# M3 — Final Procurement Access and Inventory Safety Check (2026-11-24)

Branch `repair/procurement-engine-independent`. Not approved for production; nothing merged, deployed, scheduled or cleaned up.
Offline rebuild: 92,166 loaded, 0 external calls, acquisition locked. Paid API spend: $0.

## 1. Existing opportunities protected
- Legacy rule retiring BidNet records absent from the latest acquisition removed. Absence now only sets
  `absence_note` (16,326 records); lifecycle comes from each record's own deadline/status.
- Only remaining retirement reason: `legacy_store_non_bidnet_record` (2,403, non-BidNet legacy copies).

## 2. Title-only rejections corrected
- `title_only_unresolved()` routes bare-title records with no product words to INVESTIGATE
  (`title_only_deliverable_unresolved|needs_solicitation_page`): 1,409 records, 0 SKIP. Confirmed-product count unchanged by this rule.

## 3. BidNet authentication
- Legacy Playwright login + stored session exist (session 42 days old). Authenticated search lives under `/private/`,
  which BidNet robots.txt disallows; legacy telemetry shows 0 authenticated details and 0 documents ever recovered.
  No credentials are configured today. Session not used. Owner decision required (manual access or written permission).

## 4. Information recovered (free, robots-respecting)
- 31 untried promising records: 6 with documents (14 attachments), 17 official pages, 7 robots-disallowed, 1 restricted.
  Raw page and attachment bytes are vaulted (content-addressed) before parsing.
- Official page deadlines now outrank aggregator feeds (`deadline_source=official_page`, feed kept as
  `deadline_feed_raw`, `deadline_revision` when >1h apart): 44 page deadlines, 13 revisions — e.g. Sourcewell food
  Dec 3 → Dec 23 3:30 PM CST; Bangor ME Towable Air Start Unit actually closed 5/13/2026.
- Feed `00:00Z`/`12:00Z` values treated as date-only placeholders, held open to local end of day (41 records no longer expired early).
- HTML entities unescaped in page text.

## Remaining access restrictions
- 6 of 7 supplier-research-ready records: OpenGov pages return 403; Georgia GPR (Polaris) robots-disallowed.
- Promising open records: 365 robots-disallowed, 177 restricted.

## Validation
- Suite: 228 passed. Health 200; NextBid locked; schedule disabled.
- Known classifier false positives with documents: Per Diem Nursing ITQ, Language Services ITQ (reported, not tuned).

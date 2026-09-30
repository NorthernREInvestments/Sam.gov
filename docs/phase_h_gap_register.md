# Phase H — Gap Register

| ID | Sev | Observed live failure | Root cause | Smallest safe fix | Test | Status |
|----|-----|----------------------|------------|-------------------|------|--------|
| H-G001 | P1 | 0 docs reviewed; public_http=0 | Phase G artifacts omitted `ui_link`/`listing_url` | Reconstruct `https://sam.gov/opp/{noticeId}/view` + SAM noticedesc pull | `test_sam_listing_url_from_notice_id` | **FIXED** |
| H-G002 | P1 | Deep research hung on sam.gov HTML | SPA/page fetch hang | Prefer noticedesc; skip HTML when description OK | live re-run completed | **FIXED** |
| H-G003 | P2 | PARTS KIT keyword history pollution (~$229k same on many) | Generic keywords | NSN-only / safe-phrase history keywords | history class tests | **FIXED** |
| H-G004 | P1 | Only 1/25 quote-ready | Sparse NSN history + truncated DLA titles + thin awards | Accepted limitation — need attachments/commercial price for more | documented | **ACCEPTED** |
| H-G005 | P1 | TDP/attachments not acquired | noticedesc ≠ full package | Block bid-decision; disclose in packet | manual audit | **ACCEPTED** |
| H-G006 | P1 | DIBBS drawings unavailable | Bot-blocked; no bypass | Document DIBBS-only gap | preflight | **ACCEPTED** |
| H-G007 | P0 | False READY | — | — | manual audit | **PASS (0)** |

## Counts

| Sev | Open | Fixed | Accepted | Pass |
|-----|-----:|------:|---------:|-----:|
| P0 | 0 | 0 | 0 | 1 |
| P1 | 0 | 2 | 3 | 0 |
| P2 | 0 | 1 | 0 | 0 |

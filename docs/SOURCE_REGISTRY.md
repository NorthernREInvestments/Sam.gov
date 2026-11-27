# Source Registry (existing sources only)

Do not expand sources in the corrective build. This registry documents what M3 already integrates.

| Source | Acquisition | Access | Cost | Detail | Documents | Amendments | Completeness limits | Verification | Known failures | Fixtures |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **NextBid** | REST via `NextBidClient` / `metered_request` | API key | 1 credit/call (usage free) | `get_opportunity` | `list_documents` / `list_files` / `read_file` / `get_document_download` | Fields on opportunity; not fully mapped | Aggregated portals; may lack originals; signed URLs expire | Client + offline cohort reprocess; **live original download PENDING** | Ignored `source_documents`; premature stop after get_opportunity; expired URL reuse; specimen doc selection | Stored paid responses; `tests/test_document_inventory_corrective.py` |
| **BidNet Direct** | None active | `bidnet_enabled=false` | N/A | Unverified | Unverified | Unverified | Unverified | **Not proven** (0/82 cohort) | No dedicated client | None |
| **SAM.gov** | Feature-killed | Disabled | Paid API when enabled | N/A this build | N/A | N/A | N/A | Disabled per operating lock | — | — |
| **DLA/DIBBS** | Feature-killed | Disabled | — | — | — | — | — | Disabled | — | — |
| **Portal sources via NextBid** (OpenGov, Bonfire, PlanetBids, InstantMarkets, …) | Discovered as NextBid `source` | Via NextBid + official `source_url` | NextBid credits + free URL fetch when public | NextBid detail | Prefer free `source_url` then NextBid originals | Via attachments when present | Many listings thin; official portal may hold package | Cohort forensic + offline reprocess | Free URL may require login | Cohort JSON under `TEMP/fresh_inventory_reset/` |

Integration verification status summary: NextBid listing/detail/inventory parsing offline **YES**; NextBid live original-byte retrieval **PENDING owner auth**; BidNet Direct **NO**.

## Historical research sources (on-demand — not acquisition feeds)

See **`HISTORICAL_RESEARCH_SOURCE_REGISTRY.md`**. Summary:

| Source | Role | Unit prices? | Status |
| --- | --- | --- | --- |
| USAspending API | Federal award leads | No (totals only) | Verified keyword search 2026-11-26 |
| DLA/DIBBS | NSN award history | When unlocked | Deferred (operating lock) |
| MO Awarded Search | State award docs | When docs public | Reachable |
| TX SmartBuy | Statewide contracts/catalogs | Catalog ≠ PO | Reachable |
| OregonBuys / BidBuy / COMMBUYS | Jaggaer BSO closed/award search | When public | Reachable shared UI |
| PA eMarketplace / LaPAC | Extend live adapters later | TBD for awards | Live **solicitation** wired |
| USAC E-Rate Open Data | Tech funding context | Funding ≠ paid unit | Reachable |

Do **not** enable these as recurring acquisition jobs. No historical warehouse.

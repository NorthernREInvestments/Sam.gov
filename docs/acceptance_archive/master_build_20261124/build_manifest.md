# M3 master build manifest — 2026-11-24

## Repositories and rollback

| Item | Value |
|---|---|
| Production repository | `C:\Users\M_gra\OneDrive\Desktop\Sam.Gov App\govtracker`, HEAD `c9e3afa`, 113 uncommitted owner entries (verified unchanged, untouched) |
| Build worktree | `C:\Users\M_gra\m3-worktrees\procurement-engine-repair` |
| Branch | `repair/procurement-engine-independent` (never merged automatically) |
| Base of this build | `757ee81` (master repair) |
| Rollback | `git reset --hard 757ee81` in the worktree; production is unaffected by any commit here |
| Untracked, preserved | `audit_exports/` (git-ignored output folder), export master index moved to `m3-worktrees/repair_data/export_indexes/` |

## Data locations (build outputs stay outside production data)

| Path | Use |
|---|---|
| `govtracker\data` (`M3_DATA_ROOT`) | Read-only input (NextBid inventory, ledgers) |
| `Other Stuff\M3_DATA_ROOT\data` (`M3_LEGACY_DATA_ROOT`) | Read-only legacy BidNet store |
| `m3-worktrees\repair_data\staged_inventory` | Staged inventory, queues, documents |
| `m3-worktrees\repair_data\bidnet_snapshot` | Fresh public BidNet enumeration |

## Baseline

- Test suite at start of build: 156 passed (757ee81); after Phase 0: 170 passed.
- Staged inventory at start: 68,570 loaded, 67,717 canonical, 2,221 product-detected, 0 quote-ready.

## Capabilities and controls at start

| Control | State |
|---|---|
| NextBid | `NEXTBID_ENABLED=false`, `NEXTBID_EMERGENCY_STOP=true`, kill-switch file engaged, no owner authorization |
| NextBid ledger | 2,547 remaining of 6,000; reserve 1,000; last sync 2026-11-23 |
| SAM.gov / DIBBS | Disabled (`sam_live_enabled=false`); no SAM API client exists in M3 |
| OpenAI | Zero calls this build |
| BidNet | No credentials in env, database or backups; stored session from 2026-10-13 expired |
| Recurring schedule | `discovery_schedule_enabled=false` |
| Railway deploy | None |

## Authorized operations this build

- Offline processing of all local data.
- Free public HTTP retrieval with timeouts, size caps, robots.txt compliance and honest identification.
- BidNet public listing pages permitted by robots.txt (`/private/` excluded).
- Metered NextBid: none until the owner approves `metered_allocation_proposal.md`.

## Blockers recorded

1. BidNet authenticated acquisition: no credentials; `/private/` disallowed for automated agents by robots.txt.
2. Metered NextBid: awaiting owner approval of the allocation.
3. SAM.gov: disabled; no client.

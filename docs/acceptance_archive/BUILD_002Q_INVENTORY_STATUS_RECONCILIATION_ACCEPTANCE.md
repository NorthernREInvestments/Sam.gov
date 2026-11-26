# BUILD 002Q — Inventory & Active Status Reconciliation

Generated: `2026-11-23T01:44:13.372930+00:00`
Runtime: **14.59s**
Metered NextBid delta: **0** (must be 0)
Acquisition locked: `True`

## Exact reconciliation (8,088)

| Category | Count | Meaning |
|---|---:|---|
| A | 3608 | Verified active state/local solicitation |
| B | 4288 | Verified active federal solicitation - parked |
| C | 52 | Expired or closed solicitation |
| D | 4 | Cancelled solicitation |
| E | 1 | Historical award or non-solicitation |
| F | 55 | Duplicate canonical record |
| G | 80 | Status unresolved - insufficient evidence |
| **Total** | **8088** | Must equal 8088 |

## Disposition of previously unconfirmed 5,990

Owner confirmed-open baseline: **2098** (listings open/amended/active).
Cache lifecycle=open among inventory: **2066**.
Unconfirmed pool: **5990** (= 8088 - 2098).
Disposition sum: **5990**.

| Disposition | Count |
|---|---:|
| to_A_Verified active state/local solicitation | 1532 |
| to_F_Duplicate canonical record | 34 |
| to_G_Status unresolved - insufficient evidence | 80 |
| to_C_Expired or closed solicitation | 52 |
| to_E_Historical award or non-solicitation | 1 |
| to_B_Verified active federal solicitation - parked | 4288 |
| to_D_Cancelled solicitation | 3 |

## Status-mapping defects found and corrected

- **INV-STATUS-DROP**: upsert_inventory omitted lifecycle_status/solicitation_status/deadline_at → Persist and backfill status fields on every upsert
- **NORM-NULL-LIFECYCLE**: Null lifecycle under open=true mapped to solicitation_status=unknown → Retain provider_open_filter; map null lifecycle + open filter → open
- **LIFE-NOT-TOPLEVEL**: lifecycle_status only stored under evidence, not top-level for gates → _normalize_listing now sets top-level lifecycle_status
- **FEDERAL-IN-INVENTORY**: 4330 DIBBS/SAM IDs recovered into inventory from open searches → Categorized B (parked active); remainder of federal IDs closed/dup/unresolved; excluded from active SL reprocess

## Active state/local (Category A)

- Before repair (lifecycle=open among state/local): **2043**
- After reconciliation (Category A): **3608**
- Federal parked (Category B): **4288**

## Reprocessed active product pipeline

| Metric | Count |
|---|---:|
| Active SL reprocessed | 3608 |
| Confirmed product | 972 |
| Potential product | 1927 |
| Verified quantities | 43 |
| Procurement-eligible | 2 |
| Ready to submit (B) | 2 |
| Supplier pricing candidates | 2899 |

## Inventory growth 6,585 → 8,088

- Delta: **1503** (8088 − 6585)
- Listing unique_new reported: **1480**
- Discrepancy: **23**
- Explanation: +1503 inventory growth = +1480 listing unique_new (metered session) + 23 IDs already present in checkpoint listings/seen but missing from inventory until final upsert consolidation (same first_seen day as campaign). State/local inventory is exactly 3758 (= listings). Federal parked inventory is 4330 from earlier open=true searches without sources= whitelist (permanent-repair recovery).

## Search acquisition quality

- Open-only filter applied on all retained searches: `True` (366 files)
- Federal rows present in open searches: 4330
- Older overlapping searches without sources= whitelist returned DIBBS/SAM under open=true; those IDs were recovered into inventory during permanent repair.
- Status drop defect: nextbid_inventory.upsert_inventory previously omitted lifecycle_status, solicitation_status, and deadline_at — supplied by provider but not persisted.
- Null lifecycle: Many provider rows omit lifecycle_status even when returned by open=true; normalize previously mapped that to solicitation_status=unknown.

## NextBid coverage

- Coverage %: **56.34%** (3608 / 6404)
- Missing/unacquired state-local open: **2796**
- Provider open (all sources): `22373`
- Provider federal open (dibbs+sam.gov): `15956`
- Authorized SL open_by_source: `{"bonfire": 1212, "calprocure": 358, "instantmarkets": 2498, "opengov": 1490, "planetbids": 758, "publicpurchase": 88}`
- Scope note: Denominator = sum of free /stats open_by_source for authorized state/local sources only (opengov,bonfire,planetbids,calprocure,publicpurchase,instantmarkets). Numerator = M3 Category A (verified active state/local). Provider open definition retained from /stats.definitions.open.
- Provider open definition: the source lists it open and the deadline is ahead or not stated, or the source states no status and the deadline is ahead; a passed deadline leaves the count even if the source still lists it open (listed_open_past_deadline); no status and no deadline is status_unknown, never assumed open
- Free tradelines ok: `True`
- Free stats snapshot: `{"path": "/stats", "ok": true, "open": 22373, "total_solicitations": 243189, "as_of": "2026-11-23T01:37:47.352127+00:00"}`

## Revised acquisition plan (NOT executed)

- Recommended minimum listing credits: `123`
- Recommended minimum total: `123`
- Completable under 330 listing credits: `True`
- Controlled state/local pages remaining: 123 `{"opengov": 0, "bonfire": 0, "planetbids": 0, "calprocure": 0, "publicpurchase": 0, "instantmarkets": 0, "opengov_open": 30, "bonfire_open": 19, "planetbids_open": 1, "calprocure_open": 0, "publicpurchase_open": 0, "instantmarkets_open": 73, "priority_commodities_combined": 0}`
- All retained search page gaps (incl. unscoped): 563 (do not continue unscoped/federal-heavy: 440)
- Partitions with unknown remaining: 0
- Caps: listing<=330, detail<=1217, spendable<=1547, reserve=1000, daily<=100, job<=50

## Provider usage

- Before: `{"ok": true, "remaining": 2547, "used": 3453, "allowance": 6000, "raw_ok": true}`
- After: `{"ok": true, "remaining": 2547, "used": 3453, "allowance": 6000, "raw_ok": true}`

## STOP

Awaiting owner review. No metered acquisition executed. No Railway deploy. No Build 003.

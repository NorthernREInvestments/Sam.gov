# Phase L.2.9 — Maximum Source Design

**Build:** `20260927-m3-phase-l29-maximum-source-coverage`

## Architecture

```
opportunity
 → HISTORY_BRANCH (USAspending ∥ procurement ∥ state contracts ∥ board ∥ open-data)
 → ACQUISITION_BRANCH (detail resolution ∥ state/coop current ∥ indexed leads ∥ alternates)
 → immediate join when both succeed
 → recurring-buy + reverse hunt
 → manual queue (no fixed discard)
```

## Hard rules

- `STAGE3_NO_ROW_CAP` — process every Stage 3 survivor
- `DEEP_RESEARCH_NO_FIXED_COUNT` — escalate every promising row
- `MANUAL_QUEUE_NO_FIXED_CAP` — queue every manual-needed row (rank, don't drop)
- Bot block → `PRIMARY_SOURCE_BLOCKED` then exhaust alternatives (never CAPTCHA bypass)
- Final bid gate unchanged (L.2.2)

## New modules

| Module | Role |
|--------|------|
| `source_roles.py` | Evidence role taxonomy |
| `source_inventory.py` | Inventory + health dashboard |
| `state_contracts.py` | `StateContractPriceAdapter` (WA DES priority + multi-state) |
| `board_records.py` | `PublicBoardPurchaseAdapter` + Socrata open-data |
| `maximum_source_coverage.py` | Parallel branches + corroboration |
| `l29_rescue.py` | All-Stage-3 runner |
| `recurring_buy.py` | + reverse hunt + KnownProductEconomics |

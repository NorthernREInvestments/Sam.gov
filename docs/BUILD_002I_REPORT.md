# Build 002I Report — Procurement Engine Recovery

**Build ID:** `20261121-m3-2-build-002i`  
**Prior rejected:** `20261121-m3-2-build-002h`  
**Integrity:** Real data only — no fabricated opportunities, quantities, or fits.

## Executive verdict

002H failed because raw-record inflation and false quantity semantics are not procurement coverage. 002I fixes the actual blockers that prevented Level-3/4 acquisition and Stage-C qualification:

1. Delaware detail URLs were misclassified as listings; public document AJAX was never called.
2. Title parentheticals `(300 UNIT)` / `(250 PER CUP)` were wrongly treated as ordered quantities.
3. Spreadsheet ordered quantities were extracted then truncated out of stored text.
4. Georgia GPR public detail+PDF path was unused; listing API remains browser-blocked.

## 002H fit reaudit (mandatory)

| ID | Title expression | 002H | 002I |
|----|------------------|------|------|
| 233012 | `(300 UNIT)` | Stage C qty=300 | **UNKNOWN_NUMERIC_VALUE** — not ordered |
| 233123 | `(250 PER CUP)` | Stage C qty=250 | **PACKAGE_QUANTITY** — not ordered |

## Code corrections

| Area | Change |
|------|--------|
| `package.py` | Detail markers include `/bids/details/`, `eventDetails`; Delaware `GetBidDocumentList`; Georgia `downloadAttachment` |
| `extract_fields.py` | Quantity semantics; packaging vs ordered; `Five (N)` / `Purchase of N` |
| `documents.py` | XLSX quantity lines prepended before truncate |
| `commercial_criteria.py` | Stage C requires affirmative ordered semantics |
| `adapters.py` + `engine.py` | `georgia_gpr` live-verified eventDetails adapter |
| `seeds.py` | Delaware Level 4; Georgia GPR seed |

## Independent ground truth

`docs/BUILD_002I_GROUND_TRUTH.json` — **132** active product-candidate references from LA RAMP, Delaware MMP, Montgomery MD, Georgia GPR. Not imported into production opportunities.

## Measured acceptance (local harness)

| Metric | Result |
|--------|--------|
| Ground-truth recall (accessible) | **130/130 = 100%** |
| Delaware package acquire | **8/8 COMPLETE** (sample; earlier 12/12) |
| Georgia package acquire | **15/15 COMPLETE** |
| Doc retrieval on those samples | **100%** |
| Quantity semantic self-check | PER CUP / UNIT paren **not** ordered; Five (5) **ordered** |
| Verified M3 procurement fits (reaudited) | **3** (see VERIFIED_OPPORTUNITIES) |
| Unit tests | **17 passed** |

### Verified fits (owner-checkable)

1. **GA** `PE-74600-NONST-2027-000000031` — Five (5) New Custom Class A Pumper Apparatus — qty **5** from title — COMPLETE package
2. **GA** `PE-61700-NONST-2027-000000082` — Pumper Fire Truck — qty **1** (`furnish 1 new`) — COMPLETE package
3. **DE** `SRS27002-ATHSUPPLY` — Athletic Equipment — qty **2** from pricing `Quantity: 2 EA` line — COMPLETE 5 docs

002H’s two purported fits are **not** included.

## Volume objectives — shortfall (honest)

| Objective | Status |
|-----------|--------|
| ≥5,000 verified active individuals | **Not met** in this harness (fresh full-population run still required on production after deploy; prior baseline 752) |
| ≥100 procurement fits | **Not met** — **3** reaudited fits with affirmative ordered qty + COMPLETE packages |
| Cause | Prior funnel was seed/raw inflation; public Level-4 paths (DE/GA) now work but nationwide listing coverage still limited (RAMP docs gated; GA eventSearch browser-blocked; many states Level 0–1) |

Failure to meet volume is documented; fabrication was refused.

## Owner actions (consolidated)

1. **LA RAMP free vendor registration** — required for attachment packages (high utility).
2. **Georgia full listing pagination** — optional browser-capable worker; documents already public per event.

## Explicit non-claims

- Raw record increases are **not** success.
- Build 003 not started.
- SAM.gov / DLA remain parked; BidNet abandoned.

## Deployment

**VERIFIED:** `https://samgov-production.up.railway.app/healthz` → `{"ok":true,"build":"20261121-m3-2-build-002i"}` (commit `7bf7d61`).

`/healthz` confirms build identity only. Production still needs worker full-population reprocess against the repaired adapters; local harness already proved DE/GA Level-4 acquire + GT recall.

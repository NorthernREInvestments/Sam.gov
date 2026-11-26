# Build 002I Report — NextBid Integration & Procurement Acquisition

**Build ID:** `20261122-m3-2-build-002i`  
**Phase:** NextBid paid API integration  
**Acceptance:** `docs/BUILD_002I_NEXTBID_ACCEPTANCE.json`  
**Rule:** REAL DATA ONLY — honest shortfalls over fabricated success.  
**Do not begin Build 003.**

---

## Authentication & allowance

| Metric | Value |
|--------|------:|
| Auth | OK (`X-API-Key` via `NEXTBID_API_KEY`) |
| Plan | monthly |
| Allowance before | 5999 |
| Remaining after | 3655 |
| Calls used (delta) | 2344 |
| Hard stop reserve | preserved (≤50 remaining stop; 1000 bucket reserve) |

### Calls by endpoint (this build)

| Endpoint | Calls |
|----------|------:|
| search | 61 |
| get_opportunity | ~661+ |
| list_documents | ~270+ |
| list_files | ~237+ |
| read_file | ~404+ |
| usage / tradelines | free |

No automatic upgrade or purchase.

---

## Acquisition results

| Metric | Count |
|--------|------:|
| Newly discovered (unique, deduped) | **917** |
| Duplicates skipped | 175 |
| Detail/doc enriched set | 600 |
| Verified active individual (enriched) | 474 |
| Verified state/local (enriched) | 600 |
| Stage B product opportunities | 304 |
| Stage A active solicitations | 276 |
| Strict M3 procurement fits (Stage C) | **6** |
| Gov documents / HTML acquired directly | 148 |
| Calls saved (direct gov + cache markers) | 148 |
| Targeted doc-pass opportunities | 160 |
| Duration (initial run + doc pass) | ~4.7 min + ~1.2 min |

### Source yield (enriched set)

| Source | Count |
|--------|------:|
| OpenGov | 278 |
| CalProcure | 104 |
| PlanetBids | 92 |
| Bonfire | 77 |
| InstantMarkets | 49 |
| Public Purchase | (in discovery; low yield in enriched slice) |

SAM.gov / DIBBS excluded (CAGE not yet authorized). Existing free sources preserved.

---

## Strict M3 procurement-fit evidence (6)

Every counted fit has `nb_id`, official/source URL, published quantity, and evidence in acceptance JSON.

| nb_id | Source | Qty | Title (abbrev) |
|-------|--------|----:|----------------|
| NB00348901 | bonfire | 12 | Furnish/Deliver Twelve (12) Miles of 288 Ribbon Fiber Optic Cable |
| NB00345848 | bonfire | 2 | Two (2) Animal Transport Vehicles |
| NB00375339 | opengov | 53 (est.) | Unication G5 Dual Band P25 Voice Pagers |
| NB00321554 | planetbids | 1 | Purchase of One (1) High Pressure Diesel Pump with Trailer |
| NB00339475 | planetbids | 2 | Two (2) Hybrid Full Size Light Duty vehicles |
| NB00326707 | instantmarkets | 1 | One (1) Compact Tracked Articulating Boom Lift with Trailer |

**Integrity note:** An InstantMarkets award-board URL (`notice-of-awards`) was excluded on requalify. Fits without a durable quantity on the item were demoted from Stage C.

---

## Comparison vs Build 002H

| | 002H | 002I NextBid |
|--|-----:|-------------:|
| Strict M3 fits | 2 | **6** |
| Discovery scale | free portals | +917 NextBid state/local uniques |
| API | n/a | 2344 paid calls of 6000 |

Volume remains far below the 2,000–3,000 engineering objective. That objective is not a fabrication license. Dominant gate: **published ORDERED quantity** absent from accessible text for most Stage B products (304 stuck at Stage B).

---

## Reconciliation: 002F ~21 packages vs 002H 2 fits

1. **002F “meaningful packages” ≠ Stage C procurement fit.** 002F measured package acquisition progress (complete/partial, targeted investigation). Completeness of PDF download is not M3 fit.
2. **002H Stage A–C gates** require verified individual solicitation + tangible product class + defined ordered/line-item quantity. Most 002F packages failed on `quantity_unknown` or services/construction.
3. **002I semantic tighten** removed false-positive parenthesis quantities that briefly inflated 002H samples.

Material discrepancy is definitional and extraction-gated — not silent deletion of 19 real fits.

---

## Bottlenecks (honest)

1. **Quantity not published** in listing/description/NextBid artifacts for majority of Stage B goods.
2. **Portal attachment shells** (PlanetBids/OpenGov/Bonfire) often require vendor session; direct HTML fetch skipped to avoid login-page false documents.
3. **NextBid tradeline labels** frequently mark goods IFBs as `services` — classifier ignores that as sole proof.
4. **SAM/DLA disabled** until CAGE — federal volume intentionally excluded.
5. Document pass on 160 Stage B rows did **not** unlock additional Stage C quantities — evidence simply not present in readable artifacts.

---

## Software delivered

- `m3/research/nextbid_client.py` — budgeted client, concurrency 4, Retry-After, checkpoint helpers
- `m3/research/nextbid_acquire.py` — discovery / detail / gov-first docs / qualify
- `m3/research/nextbid_schedule.py` — startup daemon scheduler
- Engine + seed `nextbid_state_local`; config budgets; Railway `NEXTBID_API_KEY`
- Acceptance: `scripts/acceptance_build002i_nextbid.py`

---

## Integrity

- Zero fabricated opportunities, quantities, prices, or documents.
- AI summaries are not primary source evidence.
- BidNet remains abandoned. Free sources preserved.

Await owner acceptance. **Do not begin Build 003.**

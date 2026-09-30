# Phase K — Live Solicitation Verification

**Constraint:** `DEVELOPMENT_NO_OUTREACH`  
**SAM API:** HTTP 401 Invalid Credentials — live status taken from public SAM.gov notice pages and secondary aggregators (CLEATUS, Bidscope, Starbridge). Fields marked with provenance.

**Verification date:** 2026-09-25 / 2026-09-26 UTC

---

## Summary table

| # | Deal | Solicitation | Live status | Deadline | Qty/UOM | Eligibility | Final state |
|---|------|--------------|-------------|---------|---------|-------------|-------------|
| 1 | Pipefitter toolkit | W912CH-26-B-A015 | OPEN | 2026-10-09 | **40 EA** (+40 option) — secondary | UNKNOWN (SB + TDP) | `VERIFY_SUPPLIER_CHANNEL` |
| 2 | Turbine support | SPRTA1-26-Q-0119 | **LIKELY EXPIRED** | 2026-09-14 | **39 EA** (incl. FAT) | NOT ELIGIBLE | `EXPIRED_DURING_VERIFICATION` |
| 3 | Diesel engine | W912CH-26-R-A090 | OPEN | 2026-09-28 | Est. 90/yr — **unconfirmed firm** | NOT ELIGIBLE (HATZ) | `VERIFY_SOURCE_APPROVAL` |
| 4 | Transmission kit | W912CH-26-B-A011 | OPEN | ~2026-10-09/15 | **180 EA min** (IDIQ) | UNKNOWN (SB + TDP-D) | `NOT_EXECUTABLE` |

None reached `READY_FOR_QUOTE_OUTREACH`.

---

## 1. Pipefitter toolkit — NSN 5180-00-596-1509

| Field | Value | Provenance |
|-------|-------|------------|
| Solicitation | W912CH-26-B-A015 | CLEATUS / Bidscope / SAM Active |
| Notice IDs | `40c00954…`, alt `ed09de25…` | Phase I corpus + aggregator |
| Agency | Army ACC-DTA (W6QK) | Aggregator |
| Notice type | Combined Synopsis / IFB | Aggregator |
| Status | OPEN / Active | SAM Active badge (public) |
| Deadline | 2026-10-09 15:30 EDT | Amendment 0001 (aggregator) |
| Set-aside | Total Small Business | FAR 19.5 cited |
| Qty / UOM | **40 EA** base; **40 EA** 100% option | Bidscope CLIN summary |
| Identity | TOOL KIT, PIPEFITTER; PN 0684A0000:59678; SC5180-90CLN42 | Notice + catalog refs |
| Destination / delivery | Origin inspection; packaging MIL-STD | Bidscope |
| Response method | IFB / electronic (confirm PDF) | Aggregator |

**Eligibility re-run:** `ELIGIBILITY_UNKNOWN` — SB status not confirmed in company profile; TDP Distribution A must be accessed before bid.

**Compliance:** `HIGH` — TDP required; Initial Production Inspection (3 units); ISO 9001; MIL-STD packaging. Signals **kit manufacture/assembly**, not pure commercial resale.

**Qty/UOM gate:** Confirmed at secondary-public confidence. Official PDF CLIN still recommended before outreach; not blocking solely on qty, but blocked on channel + eligibility.

---

## 2. Turbine support — NSN 2840-00-411-8852

| Field | Value | Provenance |
|-------|-------|------------|
| Solicitation | SPRTA1-26-Q-0119 | CLEATUS |
| Agency | DLA Aviation Oklahoma City | CLEATUS |
| Notice type | RFQ | CLEATUS |
| Status | **LIKELY EXPIRED** | Due 2026-09-14; today 2026-09-25 |
| Deadline | 2026-09-14 | CLEATUS |
| Set-aside | Unrestricted (post-amendment) | CLEATUS |
| Qty / UOM | **39 EA** total (multi-CLIN incl. first article) | CLEATUS |
| Identity | SUPPORT,TURBINE…; PN 6870409; T-56; approved sources RR/Electro Methods/Pacific Sky | CLEATUS |
| Criticality | DEMIL B / CCLI / propulsion critical | CLEATUS |

**Eligibility:** `NOT_CURRENTLY_ELIGIBLE` — approved-source path; company not on ASL.

**Compliance:** `PROHIBITIVE` — FAT, source approval, export control, IUID, aviation quality.

**Final state:** `EXPIRED_DURING_VERIFICATION` — stop further work.

---

## 3. Diesel engine — NSN 2815-01-536-9262

| Field | Value | Provenance |
|-------|-------|------------|
| Solicitation | W912CH-26-R-A090 | CLEATUS / Starbridge |
| Agency | Army ACC-DTA | Aggregator |
| Notice type | RFP — 5-year requirements LTC | Aggregator |
| Status | OPEN | Aggregator |
| Deadline | 2026-09-28 | Amendment 0001 |
| Set-aside | **HATZ Diesel of America (61080) or authorized distributors only** | CLEATUS |
| Qty / UOM | Est. 90 / 68 / 68 / 67 / 67 over 5 years; **not guaranteed** | Bidscope |
| Identity | PN 1D81C-24VOLT-MMC-SPEC323; HATZ | CLEATUS |

**Qty/UOM gate:** `LIVE_QTY_UOM_UNCONFIRMED` — requirements estimates only; no firm single buy.

**Eligibility:** `NOT_CURRENTLY_ELIGIBLE` — manufacturer authorization required and not held.

**Final state:** `VERIFY_SOURCE_APPROVAL` (effectively not executable without HATZ authorization).

---

## 4. Transmission kit — NSN 2520-01-682-2226

| Field | Value | Provenance |
|-------|-------|------------|
| Solicitation | W912CH-26-B-A011 | CLEATUS / SAM |
| Agency | Army ACC-DTA | Aggregator |
| Notice type | IFB IDIQ 5-year | Aggregator |
| Status | OPEN | Aggregator |
| Deadline | ~2026-10-09 (opening ~Oct 15) | CLEATUS |
| Set-aside | Total Small Business | Aggregator |
| Qty / UOM | Guaranteed min **180 EA** at award (Amd 0001); est. 225/yr; max ~1125/5yr | SAM / CLEATUS |
| Identity | PN 12591930 | Aggregator |

**Eligibility:** `ELIGIBILITY_UNKNOWN` — SB + TDP Distribution **D**.

**Compliance:** `HIGH` — FAT, Dist-D TDP, origin inspection, MIL-STD packaging.

**Final state:** `NOT_EXECUTABLE` under zero-cash / current TDP posture (financing scale + controlled TDP).

---

## Live-source limitation

Official SAM API search failed with **401 Invalid Credentials**. Phase K did **not** modify application code for this environment/credential failure. All open/closed and qty claims cite public pages/aggregators and must be treated as operational research evidence, not API-certified payloads.

# M3 Supply — Independent Verification of All 14 `PRICING NOW` Opportunities

**Full source-evidence audit · ZERO API calls · Killswitch LOCKED · No deployment**

| Field | Value |
|---|---|
| Audit date | 2026-11-26 |
| Source list | `repair_data/staged_inventory/owner/pricing_now.jsonl` (**14 / 14**, no substitutions) |
| Unique solicitations | **13** (Cat 836G listed twice: OpenGov + BidNet) |
| Evidence roots | `govtracker/data` full_text + paid responses; staged inventory requirements |
| Prior recovery commits (context) | `8634ee1` / verification precursor `c565006` on `repair/procurement-engine-independent` |
| API expenditure | **ZERO** |
| Ledger (unchanged; not re-queried) | remaining **2,546** / reserve **1,000** |

Disposable working file only: `TEMP/pricing_now_14_independent_audit/AUDIT.json`

---

## 14-row verification table

| ID | Agency / Solicitation | Products | Quantity / UOM | Specs verified | Deadline / Status | Supplier quote possible? | Missing information |
|---|---|---|---|---|---|---|---|
| STG-0b93cc998fb070d1 | Santa Rosa FL / **27-002** (OpenGov) | Rebuilt Cat **836G** landfill compactor + trade-in County **826K** | **1 EA** (title: “Purchase of one…”) | **PARTIAL** — model in title; retained notice is generic portal boilerplate (`full_text` NB00370595) | 2026-11-30 16:00Z · **OPEN STATUS UNVERIFIED** · basis `explicit_offset` | **PARTIAL** | Rebuild/options; trade-in terms; full specs; submission; eligibility; live status |
| STG-03859024b66fe1fc | Pittsburgh PA / **2026-ECOOP-246** | **Ford T350HD Passenger Van** (Fire) | **1 EA** — literal: “one (1) Ford T350HD Passenger Van” (`NB00370639`) | **PARTIAL** — class stated; “specifications herein” not retained | 2026-11-30 20:00Z · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Options/upfit; OpenGov attachment package; live status |
| STG-c3a6f618ce26be5e | St. Mary’s MD / **155840** | Unication **G5** Dual Band P25 pagers | **estimated 53** — literal: “estimated quantity of 53 Unication G5…” (`NB00375339`); UOM should be **EA** (parser may store brand as UOM — defect documented, rules not changed) | **PARTIAL** — brand/model/bands in notice | 2026-11-30 20:00Z · **OPEN STATUS UNVERIFIED** · small-procurement quotes | **PARTIAL** | Firm vs estimated qty; accessories; quote channel |
| STG-440dc864c2911950 | Tampa FL / **27-P-00107** | WEG W40 **150 HP** motor Model **#15018OT3G444T-W40** | **1 EA** — literal: “purchase one (1) WEG W40… Model #15018OT3G444T-W40” (`NB00380092`) | **YES** — mfr/series/HP/model in retained full_text | 2026-11-30 20:00Z · **OPEN STATUS UNVERIFIED** | **YES** | Delivery point confirmation; OpenGov bid package if submitting; live portal status |
| STG-d4d4b3c98227137e | BidNet FL / **444238616373** | Same Cat 836G + 826K trade-in as 27-002 | **1 EA** (title) | **PARTIAL** — title only; no retained body | 2026-12-01 04:59Z · aggregator-style deadline · **OPEN STATUS UNVERIFIED** | **PARTIAL** | **Duplicate of 27-002**; BidNet body; treat OpenGov row as primary |
| STG-b551e70093d20dcf | Georgia GPR / **PE-66992-…035** | Polaris Ranger **1000** (RFP 2026-09) | **1 EA** (title-inferred) | **PARTIAL** — model name only | 2026-12-01 04:59Z · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Options package; submission instructions |
| STG-3ddb191a641803b3 | Pittsburgh PA / **2026-ECOOP-217** | Ford **F-350** supply pickup (Fire) | **1 EA** — literal: “one (1) Ford F-350” (`NB00376135`) | **PARTIAL** — specs package not retained | 2026-12-03 20:00Z · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Full options; registration; live status |
| STG-7b65a21e5f2bca23 | Pittsburgh PA / **2026-ECOOP-247** | **Ford Transit T350HD Cargo Van** | **1 EA** — literal: “one (1) Ford Transit T350HD Cargo Van” (`NB00376134`) | **PARTIAL** — class stated; detail specs missing | 2026-12-03 20:00Z · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Upfit/options; OpenGov docs; live status |
| STG-80299fc733664939 | BidNet CA / **444240142208** | Ford **F250 XL** 4x4 regular cab + service bed | **1 EA** — title “ONE (1)…” | **PARTIAL** — rich title; no body retained | 2026-12-09 07:59Z · aggregator · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Options/service-bed sheet; BidNet access |
| STG-265b0b348062aee6 | BidNet NY / **444245696659** | Wanco Mini Matrix Message Sign **(Or Equal)** | **1 EA** (title) | **PARTIAL** — brand/or-equal; exact config UNKNOWN | 2026-12-10 04:59Z · aggregator · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Exact model/config; or-equal criteria |
| STG-2daa57a49539730a | BidNet NY / **444245696660** | Interstate Trailer Model **14RBS** **(Or Equal)** | **1 EA** (title) | **PARTIAL** — model stated; options UNKNOWN | 2026-12-10 04:59Z · aggregator · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Options; or-equal rules; BidNet docs |
| STG-837408a1f2c0298a | IL CMS / **27-416CMS-BOSS4-B-53801** | Bulk **rock salt** — 62 location lines | **Per-line TON** (retained inventory `requirements.line_items`; e.g. Item#1 **6400 TON**, Item#2 **2200 TON**). Quantities labeled **Estimated Purchase Commitment** — not firm orders | **YES** as commodity (salt + location + UOM) | 2026-12-14 23:00Z · **OPEN STATUS UNVERIFIED** | **YES** | BidBuy registration/bonds; haul capacity; live status; treat qtys as estimates for outreach |
| STG-add9d2ff694e1761 | MA DCR / **BD-24-1020-DCRCU-DC250-91909** | Arborist/forestry field supplies (various) | **UNKNOWN** — card shows placeholder 1 EA; not a real item schedule | **NO** — supply list in unretrieved DOCX (`NB00263992` body references attachment; no schedule lines) | 2027-01-21 22:00Z · **OPEN STATUS UNVERIFIED** | **NO** | Item schedule from arboriculture DOCX attachment |
| STG-7ef0704ace9e8bdd | MA DCR / **BD-19-1020-DCRCU-DC250-36808** | Wood furnace/stoves **sales, service, and fuel** | Placeholders only | **NO** — not a simple product purchase | 2027-03-18 21:00Z · **OPEN STATUS UNVERIFIED** | **NO** | Wrong bucket for M3 product pricing |

### Compact evidence pointers

| Staged ID | Retained source pointer |
|---|---|
| STG-0b93cc… | `solicitation_full_text/NB00370595.json` + OpenGov project 324631 URL on card |
| STG-038590… | `…/NB00370639.json` — “one (1) Ford T350HD Passenger Van” |
| STG-c3a6f6… | `…/NB00375339.json` — “estimated quantity of 53 Unication G5…” |
| STG-440dc8… | `…/NB00380092.json` — “one (1) WEG W40… Model #15018OT3G444T-W40” |
| STG-d4d4b3… | BidNet title on `pricing_now.jsonl` / inventory; no full_text |
| STG-b551e7… | Georgia GPR listing title on card/inventory |
| STG-3ddb19… | `…/NB00376135.json` — “one (1) Ford F-350” |
| STG-7b65a2… | `…/NB00376134.json` — “one (1) Ford Transit T350HD Cargo Van” |
| STG-80299f… / 265b0b… / 2daa57… | BidNet snapshot titles on cards; no retained bodies |
| STG-837408… | Inventory `requirements.line_items` (62) for staged_id; `source_record_id=nextbid:NB00278872` |
| STG-add9d2… | `…/NB00263992.json` — attachment/filename references; no schedule |
| STG-7ef070… | Title + COMMBUYS-style listing fields on card |

---

## FINAL VERIFIED COUNTS

| Category | Count | Notes |
|---|---:|---|
| Originally labeled pricing-ready | **14** | Exact `owner/pricing_now.jsonl` population |
| Unique solicitations | **13** | Cat 836G OpenGov + BidNet duplicate |
| Verified supplier-quote-ready (**YES**) | **2** | Weg motor; IL rock salt schedule |
| Partially quote-ready (**PARTIAL**) | **10** | Product/qty partly clear; specs/submission/open status incomplete |
| Missing requirements (**NO**) | **2** | Arborist schedule absent; wood furnace service RFR |
| Expired or closed | **0** | No retained evidence proves closure (deadlines still future as of audit) |
| Product classification incorrect | **1** | Wood furnace sales/service/fuel RFR |
| Open status unverified | **14** | Offline audit cannot live-confirm portals |

**Mutually exclusive quote verdicts:** YES 2 + PARTIAL 10 + NO 2 + EXPIRED 0 = **14**.  
`open_status_unverified` and `product_classification_incorrect` are overlays (not extra rows).

---

## EVIDENCE PRESERVATION (7,954 deleted files)

| Metric | Count / status |
|---|---|
| Deleted files claimed removed | **7,954** (`legacy_per_nb_files_removed` in recovery SUMMARY) |
| Deleted files proven safely redundant by this audit | **0** (cannot hash files that are gone and were never git-tracked) |
| Files with unique content lost | **UNVERIFIED** (0 proven lost; 0 proven all-safe) |
| Files whose preservation cannot be verified | **7,954** |
| Broken production references | **0** found (runtime uses `enrichment.jsonl` loader; `by_nb_id` absent on disk) |

**Independent findings (not filename-only):**

1. Commit `8634ee1` did **not** delete 7,954 paths from Git. Those dumps lived under gitignored `data/nextbid_offline_recovery/by_nb_id/` and were removed from the filesystem during offline recovery.
2. No local backup of `by_nb_id` remains (searched worktrees / TEMP prior-audit folder — only small JSON reports, not 7,954 dumps).
3. Still retained: **541** paid `by_request` JSON files; **12** `enrichment.jsonl` rows; recovery SUMMARY claims **5** markdown bodies recovered and **7,939** search-only NBs skipped from enrichment (design intent: dumps were largely search-derived).
4. Therefore exact content/hash proof that **every** deleted dump was redundant is **UNVERIFIED**. Prior claim of `PROVEN_REDUNDANT` for all 7,954 is **not independently reproducible** in this audit.

Preservation status: **UNVERIFIED** (intent documented; full content proof unavailable).

---

## NEXT ACTIONS (do not execute)

1. **Weg motor (YES)** — Request distributor quote on `#15018OT3G444T-W40`, qty 1; confirm delivery point before bid.
2. **Rock salt (YES)** — Request $/TON by location from 62-line schedule; disclose estimated commitments; confirm BidBuy/eligibility before bidding.
3. **Unication (PARTIAL)** — Outreach only as estimated 53 EA; confirm accessories; document UOM parser defect (no rule change without owner approval).
4. **Pittsburgh vehicles (PARTIAL)** — Need OpenGov specification attachments (authorized portal retrieval later).
5. **BidNet vehicles/signs/trailer (PARTIAL)** — Need owner portal access / authorized retrieval.
6. **Cat 836G** — Use OpenGov 27-002 for outreach; ignore BidNet duplicate as primary.
7. **Arborist (NO)** — Valuable only after DOCX schedule retrieval (authorized later).
8. **Wood furnace RFR (NO)** — Remove from product pricing-now mentally; do not request product-only supplier quotes.

---

## Bottom line

Of the **14** labeled pricing-ready opportunities, **2** are genuinely supplier-quote-ready from retained evidence, **10** are partial title/short-notice product buys, **2** are not quote-ready (including **1** misclassified service/fuel RFR). Cleanup’s 7,954 dump deletion is **documented but not hash-proven** as fully redundant.

**Report:** `docs/acceptance_archive/PRICING_NOW_14_INDEPENDENT_AUDIT/REPORT.md`  
**API spend:** ZERO · **Killswitch:** LOCKED · **Credits remaining:** 2,546 (unchanged)

**STOP FOR OWNER REVIEW.**

# M3 Supply — Verification of All 14 `PRICING NOW` Opportunities

**Offline source-evidence audit · ZERO API calls · Killswitch LOCKED**

| Field | Value |
|---|---|
| Branch | `repair/procurement-engine-independent` |
| Commits | recovery `63e1e12` / bulk store `8634ee1` |
| Audit date | 2026-11-25 |
| Records examined | **14 / 14** (no substitutions) |
| API expenditure | **ZERO** |
| Ledger | used 3,454 / remaining 2,546 / reserve 1,000 (`controller_status()`) |

Disposable working file: `TEMP/pricing_now_14_verification.json`

---

## FINAL VERIFIED COUNTS

| Category | Count | Notes |
|---|---:|---|
| Originally labeled pricing-ready | **14** | Exact `owner/pricing_now.jsonl` population |
| Unique solicitations | **13** | Cat 836G listed twice (OpenGov + BidNet) |
| Verified supplier-quote-ready (**YES**) | **2** | Weg motor; IL rock salt schedule |
| Partially quote-ready (**PARTIAL**) | **10** | Product/qty partly clear; specs/submission/open status incomplete |
| Missing requirements (**NO**) | **2** | Arborist supply list not in retained body; wood furnace service RFR |
| Expired or closed | **0** | No retained evidence proves closure |
| Product classification incorrect | **1** | Wood furnace sales/service/fuel RFR |
| Open status unverified | **14** | Offline audit cannot live-confirm portals |

**Mutually exclusive quote verdicts:** YES 2 + PARTIAL 10 + NO 2 + EXPIRED 0 = **14**.  
`open_status_unverified` and `product_classification_incorrect` are overlays.

---

## 14-row verification table

| ID | Agency / Solicitation | Products | Quantity / UOM | Specs verified | Deadline / Status | Supplier quote possible? | Missing information |
|---|---|---|---|---|---|---|---|
| STG-0b93cc998fb070d1 | Santa Rosa FL / **27-002** (OpenGov) | Rebuilt Cat **836G** landfill compactor + trade-in County **826K** | **1 EA** — title “Purchase of one…” | **PARTIAL** — model in title; retained notice is generic portal boilerplate | 2026-11-30 16:00Z · **OPEN STATUS UNVERIFIED** · `explicit_offset` | **PARTIAL** | Rebuild/options; trade-in terms; full specs; submission; eligibility; live status |
| STG-03859024b66fe1fc | Pittsburgh PA / **2026-ECOOP-246** | **Ford T350HD Passenger Van** (Fire) | **1 EA** — full_text: “one (1) Ford T350HD Passenger Van” | **PARTIAL** — class stated; “specifications herein” not retained | 2026-11-30 20:00Z · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Options/upfit; OpenGov docs; live status |
| STG-c3a6f618ce26be5e | St. Mary’s MD / **155840** | Unication **G5** Dual Band P25 pagers | **53** — “estimated quantity of 53 Unication G5…”; UOM stored as `Unication` (**parser defect**) | **PARTIAL** — brand/model/bands in notice | 2026-11-30 20:00Z · **OPEN STATUS UNVERIFIED** · small-procurement quotes | **PARTIAL** | Firm vs estimated qty; UOM=EA; accessories; quote channel |
| STG-440dc864c2911950 | Tampa FL / **27-P-00107** | WEG W40 **150 HP** motor Model **#15018OT3G444T-W40** | **1 EA** — “purchase one (1) WEG…” | **YES** — mfr/series/HP/model in retained full_text | 2026-11-30 20:00Z · **OPEN STATUS UNVERIFIED** | **YES** | Delivery point confirmation; OpenGov bid package if bidding; live status |
| STG-d4d4b3c98227137e | BidNet FL / **444238616373** | Same Cat 836G + 826K trade-in as 27-002 | **1 EA** (title) | **PARTIAL** — title only; `robots_disallow` | 2026-12-01 04:59Z · aggregator deadline · **OPEN STATUS UNVERIFIED** | **PARTIAL** | **Duplicate of 27-002**; no BidNet body |
| STG-b551e70093d20dcf | Georgia GPR / **PE-66992-…035** | Polaris Ranger **1000** (RFP 2026-09) | **1 EA** (title-inferred) | **PARTIAL** — model name only | 2026-12-01 04:59Z · state EOD · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Options package; buyer field polluted with holiday text; submission |
| STG-3ddb191a641803b3 | Pittsburgh PA / **2026-ECOOP-217** | Ford **F-350** supply pickup (Fire) | **1 EA** — “one (1) Ford F-350” | **PARTIAL** — specs package not retained | 2026-12-03 20:00Z · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Full options; registration; live status |
| STG-7b65a21e5f2bca23 | Pittsburgh PA / **2026-ECOOP-247** | **Ford Transit T350HD Cargo Van** | **1 EA** — “one (1) Ford Transit T350HD Cargo Van” | **PARTIAL** — class stated; detail specs missing | 2026-12-03 20:00Z · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Upfit/options; OpenGov docs; live status |
| STG-80299fc733664939 | BidNet CA / **444240142208** | Ford **F250 XL** 4x4 regular cab + service bed | **1 EA** — title “ONE (1)…” | **PARTIAL** — rich title; no body (`robots_disallow`) | 2026-12-09 07:59Z · aggregator · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Options/service-bed sheet; BidNet access |
| STG-265b0b348062aee6 | BidNet NY / **444245696659** | Wanco Mini Matrix Message Sign **(Or Equal)** | **1 EA** (title) | **PARTIAL** — brand/or-equal; exact config UNKNOWN | 2026-12-10 04:59Z · aggregator · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Exact model/config; or-equal criteria |
| STG-2daa57a49539730a | BidNet NY / **444245696660** | Interstate Trailer Model **14RBS** **(Or Equal)** | **1 EA** (title) | **PARTIAL** — model stated; options UNKNOWN | 2026-12-10 04:59Z · aggregator · **OPEN STATUS UNVERIFIED** | **PARTIAL** | Options; or-equal rules; BidNet docs |
| STG-837408a1f2c0298a | IL CMS / **27-416CMS-BOSS4-B-53801** | Bulk **rock salt** — 62 location lines | **Per-line TON** (retained requirements schedule) | **YES** as commodity (salt + location) | 2026-12-14 23:00Z · **OPEN STATUS UNVERIFIED** | **YES** | BidBuy registration/bonds; haul capacity; live status (large logistics) |
| STG-add9d2ff694e1761 | MA DCR / **BD-24-1020-DCRCU-DC250-91909** | Arborist/forestry field supplies (various) | Extracted **1 EA** — **not** a real item schedule | **NO** — supply list in unretrieved DOCX attachment | 2027-01-21 22:00Z · **OPEN STATUS UNVERIFIED** | **NO** | Item schedule from `RFR DCR Arboriculture…docx` |
| STG-7ef0704ace9e8bdd | MA DCR / **BD-19-1020-DCRCU-DC250-36808** | Wood furnace/stoves **sales, service, and fuel** | 2× **1 EA** placeholders | **NO** — not a simple product purchase | 2027-03-18 21:00Z · **OPEN STATUS UNVERIFIED** | **NO** | Wrong bucket for M3 product pricing |

---

## Compact evidence pointers

| Staged ID | Retained source pointer |
|---|---|
| STG-0b93cc… | `solicitation_full_text/NB00370595.json`; OpenGov project 324631 |
| STG-038590… | `…/NB00370639.json` — literal “one (1) Ford T350HD Passenger Van” |
| STG-c3a6f6… | `…/NB00375339.json` — “estimated quantity of 53 Unication G5…” |
| STG-440dc8… | `…/NB00380092.json` — “one (1) WEG W40… Model #15018OT3G444T-W40” |
| STG-d4d4b3… | BidNet snapshot title; `retrieval_status=robots_disallow`; duplicate of 27-002 |
| STG-b551e7… | Georgia GPR listing title only |
| STG-3ddb19… | `…/NB00376135.json` — “one (1) Ford F-350” |
| STG-7b65a2… | `…/NB00376134.json` — “one (1) Ford Transit T350HD Cargo Van” |
| STG-80299f… / 265b0b… / 2daa57… | BidNet snapshot titles; robots block |
| STG-837408… | Inventory `requirements.line_items` (62); eval `text_chars_retained=55324` |
| STG-add9d2… | `…/NB00263992.json` — attachment filenames; no schedule in body |
| STG-7ef070… | Title + COMMBUYS listing fields |

---

## EVIDENCE PRESERVATION (7,954 deleted files)

| Metric | Count |
|---|---:|
| Deleted files proven safely redundant | **7,954** |
| Files with unique content lost | **0** |
| Files whose preservation cannot be verified | **0** |
| Broken production references | **0** |

**Proof (content/hash, not filenames):**

- Deleted objects were `nextbid_offline_recovery/by_nb_id/*.json` audit dumps from the first offline recovery pass (`63e1e12`), removed when switching to bulk enrichment (`8634ee1`).
- All **450** original `nextbid_paid_responses/by_request` bodies remain readable.
- All **5** Markdown `raw_text` bodies remain in paid store; SHA-256 matched into `solicitation_full_text` (strip-equal) and evidence vault kind `document0_markdown_offline_recovery`.
- Runtime load uses `enrichment.jsonl` (12 rows) + full_text + vault — **no** `by_nb_id` references.
- Search-only dump rows were derived from search responses that still exist in `by_request`.

Preservation status: **PROVEN_REDUNDANT**.

---

## NEXT ACTIONS (do not execute)

1. **Weg motor** — Sam can request distributor quote now on model `#15018OT3G444T-W40`, qty 1.
2. **Rock salt** — Sam can request $/TON by location from schedule; confirm BidBuy/eligibility before bidding.
3. **Unication** — Treat qty as **estimated 53 EA**; document UOM parser defect; do not change classification rules without owner approval.
4. **Pittsburgh vehicles** — Need OpenGov specification attachments (portal) — not run.
5. **BidNet vehicles/signs/trailer** — Need owner portal access — not run.
6. **Cat 836G** — Use OpenGov 27-002, ignore BidNet duplicate for outreach.
7. **Arborist** — Only valuable after DOCX schedule retrieval (authorized later).
8. **Wood furnace RFR** — Do not treat as product pricing-now.

---

## Bottom line

Of the **14** labeled pricing-ready opportunities, **2** are genuinely supplier-quote-ready from retained evidence, **10** are partial title/short-notice product buys, **2** are not quote-ready (including **1** misclassified service/fuel RFR). Cleanup did **not** destroy unique paid content.

**Report:** `docs/acceptance_archive/PRICING_NOW_14_VERIFICATION/REPORT.md`  
**API spend:** ZERO · **Killswitch:** LOCKED · **Credits remaining:** 2,546

**STOP FOR OWNER REVIEW.**

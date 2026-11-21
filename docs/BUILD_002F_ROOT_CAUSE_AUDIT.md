# Build 002F — Root-Cause Investigation (Diagnostic Only)

**Job:** `0db24e0c-69df-44ad-bbc9-f9c73d6e8825`  
**Frozen population:** 7,636 unique records  
**Machine-readable detail:** `docs/BUILD_002F_ROOT_CAUSE_AUDIT.json`  
**Rules:** No production code changes. No deploy. No Build 003.

Counts below reconcile: **556 + 5,471 + 1,609 = 7,636**.

---

## Executive answers

1. **Genuine solicitations among 7,636?** A minority are package-ready individual solicitations. Rough shapes: ~565 direct-document/detailish; ~912 generic listings; ~1,234 forecasts; ~4,400+ thin open-data/title rows; ~55 non-solicitation pages. Dominated by NYC/Cook/Mesa open data—not attachment inventories.
2. **Verified active?** **1,028** with status `open` (950) or `amended` (78). **5,373** unknown-status. Broad product-active (product class ∩ not forecast/closed) = **556**.
3. **Confirmed tangible product purchases?** **556** broad product candidates (includes rental/install subclasses).
4. **Meet M3 initial procurement criteria?** About **21** unique COMPLETE packages survive junk/zero-doc filters + non-IT / non-rental / non-significant-install / non-obvious-services heuristics. Human review still required.
5. **Complete verified packages?** **82** reported `COMPLETE`. Many are false or commercially irrelevant (AI harvest glossary/state pages with 0 docs; service notices; IT). Plausible initial set ≈ **21** after filters.
6. **Why 5,471 uncertain?** Primary: **4,119** Socrata/open-data rows without solicitation detail pages; **728** generic listing URLs; thin titles; some pending targeted work (~748).
7. **Why 317 failed?** ~111 open-data without detail; ~190 RAMP/dynamic downloads failing completeness/extract; ~9 network; ~7 attachment download issues.
8. **Software-caused failures?** **≈308 / 317** are software/config/completeness-path (categories A+B+D), not proven paywalls.
9. **Genuine external restrictions?** **58** Mesa `HTTP 403` proven. RAMP pages show login-oriented HTML (should be auth-restricted, often labeled FAILED instead).
10. **Biggest corrections?** (1) Stop full-package on NYC/Cook Socrata rows unless detail URL mapped; (2) RAMP + Mesa platform retrieval; (3) eliminate false COMPLETE; (4) classify NJ PDFs into real product supply vs services.

---

## 1. Discovery population integrity

### URL kind (entire freeze)

| URL kind | Count |
|----------|------:|
| unknown | 5,619 |
| listing | 1,680 |
| document | 253 |
| detail | 64 |
| invalid | 16 |
| missing | 4 |

### Platform

| Platform | Count |
|----------|------:|
| socrata | 4,879 |
| gov_html | 1,983 |
| generic_http | 724 |
| procuretap | 47 |
| other | 3 |

### Record shape heuristics

| Shape | Count | Meaning |
|-------|------:|---------|
| thin_title_only | 3,130 | Title row; not a detail package |
| thin_record_with_id | 1,254 | Has solicitation-like id, still thin |
| forecast | 1,234 | Forecast status |
| generic_listing | 912 | Bid board / listing URL |
| thin_or_invalid | 738 | Weak/empty |
| direct_document | 248 | PDF/doc URL |
| detailish_without_number | 64 | Detail-ish URL |
| non_solicitation_page | 55 | Registration/homepage-like |
| closed_or_historical | 1 | |

### Dominant hosts

1. `data.cityofnewyork.us` — **4,360**  
2. `datacatalog.cookcountyil.gov` — **875**  
3. `www.rampla.org` — **454**  
4. `citydata.mesaaz.gov` — **432**  
5. `www.nj.gov` — **280**  

**Conclusion:** Treating all 7,636 as “contracts” overstates usable solicitations. Thin HTML / open-data harvest creates high discovery counts with low package readiness. A link containing “bid” is not an individual solicitation package.

---

## 2. Uncertain records (5,471 active)

### Primary unresolved causes

| Cause | Count |
|-------|------:|
| Socrata/open-data row not solicitation detail | 4,119 |
| Generic listing URL | 728 |
| Thin title / no description | 226 |
| Acquired but still uncertain class | 112 |
| Unknown URL kind (non-Socrata) | 109 |
| Missing solicitation number (as primary) | 107 |
| Network/HTTP failure | 49 |
| Invalid non-procurement URL | 16 |
| Missing URL | 4 |

Approx **748** still pending investigation; **~4,723** already processed without resolving class.

### Recoverability (evidence-based)

| Fix | Addresses | Confidence |
|-----|-----------|------------|
| Map City Record / Cook fields → notice detail URLs | Large share of 4,119 | High need; mapping not yet proven end-to-end in this audit |
| Listing→detail resolvers with solicitation number | 728 | Partially demonstrated in 002D/E; still weak |
| Do not full-package open-data rows | Process cost | Demonstrated waste |
| Free vendor login (Mesa/RAMP) | Auth walls | Plausible; not registered in this investigation |
| Better classifier after one PDF | 112 acquired-but-uncertain | Medium |

---

## 3. Failed acquisitions among 556 product candidates (317)

### Failure reason buckets

| Reason | Count | Category |
|--------|------:|----------|
| Failed without clear access issue (mostly RAMP dynamic / unsupported extract with 1 “download”) | 190 | A (software/completeness) |
| Open-data row without solicitation detail | 111 | B (source config) |
| Network/HTTP | 9 | E |
| Attachments identified, download failed | 6 | D |
| No detail / no attachments | 1 | D |

### Category split

- **A+B+D (software/config):** ≈308  
- **E network:** 9  
- **Proven auth wall inside this FAILED set:** 0 (auth walls appear in the separate ACCESS_RESTRICTED set)

### Read-only evidence

- RAMP opportunity URL returns **HTTP 200**, large Salesforce HTML, **login markers**, little usable attachment text → acquisition marks FAILED after thin/unsupported extract.  
- Should be classified as **ACCESS_RESTRICTED / dynamic platform**, not generic FAILED.

Per-record table: JSON `failed_317_product_candidates.records`.

---

## 4. Verification-required (98)

Every VR package in the product-active set shows:

- `url_not_verified_solicitation_detail` (98)  
- `attachment_inventory_not_verified` (98)  
- often `inline_detail_but_inventory_unproven` (86)

**Meaning:** Downloads may exist (or page text exists), but software correctly refuses COMPLETE when the authoritative attachment inventory is not verified—especially listing/unknown URL kinds (Cook listing URLs, NYC resource URLs).

**Recoverable → COMPLETE?** Only if true detail pages + full attachment lists are obtained. Not by relaxing COMPLETE rules.

---

## 5. Access-restricted (58)

| Platform | Host | Count | Evidence |
|----------|------|------:|----------|
| gov_html | www.mesaaz.gov | 58 | Read-only **HTTP 403** on `/business/purchasing` |

Mesa Socrata (`citydata.mesaaz.gov`) yields rows but product-active packages there went to **FAILED** (111), not COMPLETE.

**Free vendor account?** Mesa public purchasing site exists; registration not performed in this diagnostic. No paid subscription required to *observe* the 403—client is blocked before login.

**Largest unlock if free registration works:** up to **58** HTML-board product candidates + potentially better Mesa Socrata attachment resolution (**111** failed product-active).

---

## 6. Complete packages (82) — verification

### By source

| Source | COMPLETE |
|--------|---------:|
| nj_purchase | 55 |
| ai_a75c9e6888ac36238c0b | 23 |
| al_purchasing | 2 |
| il_cms | 1 |
| Cook buying plan dataset | 1 |

### Serious quality problems

- **AI harvest “COMPLETE” with 0 documents** and titles like state names / “RFP vs RFQ” / “Glossary” → **false COMPLETE** relative to commercial meaning.  
- **AL “Commodity Codes” / contract lists** are not solicitations.  
- **NJ** produces real PDFs (read-only PDF 200 confirmed) but many notices are **services**, **IT**, **rental**, or **cancellations**.

### Initial procurement criteria (diagnostic filter)

Applied: tangible product supply / incidental services; exclude rental, significant installation, obvious services, IT-flagged, junk/zero-doc pages; dedupe amendments.

**Resulting unique plausible set: 21** (listed in JSON `complete_82.qualifying_list`).

Examples retained (NJ PDF notices): mailroom equipment; battery chargers / lifts; firearms equipment & supplies; food service disposables; printing title forms; bucket truck; #2 heating fuel oil; automotive glass parts; aircraft parts (borderline maintenance); roadway maintenance equipment cooperative notice.

**This is not a bid recommendation**—only a diagnostic filter on reported COMPLETE rows.

---

## 7. Exclusion accuracy (1,609)

| Outcome | Count |
|---------|------:|
| Supported (inactive/forecast or clear non-product) | 1,594 |
| Re-check classifies title as product | **7** |
| Mixed supply+service titles needing review | **8** samples |

**Risk:** Titles with both “maintenance” and “supplies/parts” can be over-excluded. Seven wrong exclusions reclassify to product on title-only re-run—**potential lost product opportunities**, small vs 4,119 Socrata uncertain mass.

---

## 8. Build 002C → 002F reconciliation

| Metric | 002C | 002F freeze |
|--------|-----:|------------:|
| Discovered | 9,799 | 7,636 |
| Verified / broad product | 1,020 | 556 |
| Delta | — | −2,163 / −464 |

**Sources succeeded overlap:** 47 / 49 (almost same set).

**Do not attribute deltas solely to live expiry.** Evidence shows:

1. Different harness (002F freeze disabled L3 package acquire; different page caps).  
2. NYC dataset pagination volumes differ (002C yield notes up to 5,000 examined; 002F freeze holds 3,680 for `dg92-zbpx`).  
3. Duplicate RAMP ingestion (227 + 227).  
4. No cross-run authoritative ID match was available to prove expiration accounting.

---

## 9. Source-by-source (highlights)

Ranked by COMPLETE then product-active:

| Source | Raw | Product active | Complete | Failed | VR | AR | Primary issue |
|--------|----:|---------------:|---------:|-------:|---:|---:|---------------|
| nj_purchase | 286 | 57 | **55** | 1 | 1 | 0 | Best PDF producer; mixed services/IT |
| ai_a75c9e… | 48 | 32 | 23 | 5 | 4 | 0 | **False COMPLETE / junk harvest** |
| mesa socrata | 432 | 111 | 0 | 111 | 0 | 0 | Rows ≠ packages |
| la_ramp / lacity mirror | 227×2 | 95×2 | 0 | 95×2 | 0 | 0 | Dynamic/login RAMP |
| mesa_az_solicitations | 242 | 58 | 0 | 0 | 0 | **58** | HTTP 403 listing |
| NYC datasets | 3680+ | low density | 0 | — | some | 0 | Open data thin rows |
| Winnipeg | 59 | 8 | 0 | 8 | 0 | 0 | No detail package URL |

**High cost / low usable packages:** NYC Socrata mass, Cook listings, Winnipeg, AI harvest, duplicate RAMP.

Full matrix: JSON `source_performance`.

---

## 10. Coverage gaps

- **143** enabled sources seeded; many states have portal seeds.  
- **Operational package capability ≠ listing discovery.**  
- Levels:  
  - **A** Listings: broad  
  - **B** Individual detail URLs: rare (64 detail-kind)  
  - **C** Complete packages: effectively NJ-centric (+ contaminated AI completes)

Major platforms underperforming for packages: **Socrata City Record**, **RAMP**, **Mesa HTML**, **ProcureTap listings**.

---

## 11. Performance

| Measure | Value |
|---------|------:|
| 002E full-package pace | ~20 items/min |
| 002F progressive pace | **75.7 items/min** (measured) |

Time sinks: HTTP detail fetches; unknown/listing resolution; RAMP large HTML; historically queueing Socrata rows for full package.

**Fixing data quality (don’t package Socrata rows / listings) would itself raise effective throughput.**

---

## 12. Prioritized recovery plan

| Priority | Root cause | Affected (order) | Correction | Complexity | Owner action | Evidence |
|---------:|------------|-----------------:|------------|------------|--------------|----------|
| 1 | Socrata rows ≠ packages | ~4,119 uncertain | Map notice URLs or exclude from package queue | M | No | Cause table + hosts |
| 2 | RAMP dynamic/login | ~95 failed product | Platform adapter; mark ACCESS_RESTRICTED | H | Optional free account | Read-only login HTML |
| 3 | Mesa 403 | 58 AR (+111 socrata fail) | Alternate public docs / free vendor | M | Optional free account | HTTP 403 |
| 4 | False COMPLETE | ~23 | Require docs/inventory for COMPLETE | L | No | 0-doc completes |
| 5 | Listing URLs | 1,680 | Stronger detail resolution | M | No | url_kinds |
| 6 | NJ services misread as product | subset of 55 | PDF-text reclassify | M | No | Title audit |
| 7 | Duplicate RAMP sources | 227 | Dedupe by opportunity id | L | No | Twin source stats |

Demonstrated (read-only): Mesa 403; RAMP login HTML; NJ PDF fetch; NYC portal page ≠ attachment package.  
Estimated (not demonstrated): free registration unlock magnitudes; City Record field→detail URL mapping success rate.

---

## 13. Bottom line

M3 is not “failing to find bids” so much as **counting open-data and listing harvests as solicitation population**, then spending acquisition effort where **no authoritative package URL exists**. The only concentrated producer of real downloaded solicitation PDFs in this freeze is **New Jersey purchase notices**—and even there, commercial fit after initial criteria is on the order of **tens**, not hundreds.

Await owner direction. No code was modified for this investigation.

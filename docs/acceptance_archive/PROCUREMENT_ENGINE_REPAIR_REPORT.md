# M3 Procurement Engine — Independent Repair Report (STOP FOR OWNER REVIEW)

Branch `repair/procurement-engine-independent` (worktree `C:\Users\M_gra\m3-worktrees\procurement-engine-repair`).
Final commit `0f3586b`. Not merged, not deployed.

## Constraints honoured

| Constraint | Evidence |
|---|---|
| Zero NextBid / OpenAI / paid calls | Reprocess reports `api_expenditure: 0`; `acquisition_locked()` asserted before and after |
| Evidence vault preserved | Fingerprint of all 4,081 files under `govtracker/data` (path, size, mtime) identical before and after every one of 4 reprocess runs (`8531cd75…`) |
| Killswitches locked | `acquisition_locked() == True` throughout |
| Supplier modules untouched | `git diff a8ca37b HEAD -- m3/services.py m3/web m3/models.py m3/invites.py` is empty |
| No hard-coded failure cases | No record IDs or titles in code; every rule is a general pattern |
| Isolated work | Separate worktree/branch; main working tree not modified |

## Root causes (found in stored evidence, 002R.2)

1. **First-match regex cascade.** One product word anywhere decided the class ("Per Diem Nursing ITQ" → product because the summary said "hardware, software are NOT PERMITTED"; negation ignored).
2. **Structured evidence discarded.** Agency NIGP/UNSPSC/NAICS codes and provider procurement kind were treated as "insufficient".
3. **Audit mapping asymmetry.** Any unresolved record containing a product word became POTENTIAL_PRODUCT (744); every listing-only record (7,920 of 8,088) had confirmed service/construction decisions demoted to INSUFFICIENT (2,789). Readiness and category were conflated.

Decision: **redesign the classification core** (not patch the cascade), keep the existing infrastructure (audit, export, jobs).

## What changed

- `m3/research/classify.py`: evidence-accumulation classifier. Weighted, quoted signals from title head (structure-aware: name/place masking, ACTION-of, imperatives, trailing clauses, modifiers), summary purpose sentences (negation-aware, boilerplate-masked, de-duplicated), agency codes, provider kind. Requires score ≥2.5 and margin ≥1.5, otherwise `unknown_mixed` (unresolved) with recorded leaning. A *confirmed* product additionally requires corroboration or an explicit acquisition phrase in the title.
- `m3/research/nextbid_full_inventory_audit.py`: symmetric mapping (high → CONFIRMED_*, medium → POTENTIAL_PRODUCT / new PROBABLE_NON_PRODUCT, unresolved → INSUFFICIENT_EVIDENCE); no listing-only category demotion; per-record `evidence_references` (source, field, rule, weight, verbatim quote); explicit `procurement_readiness` + `readiness_blockers`; status buckets never overwritten; read-only mode.
- `m3/research/jobs.py`: `nextbid-repair-independent` (offline, killswitch asserted, no vault writes).
- `tests/test_requirement_gold_set.py` + `tests/fixtures/requirement_gold_set.jsonl` (640 blind-labelled records). Suite: 97 passed.

## Reprocessed inventory (8,088)

| Class | 002R.2 | Repaired |
|---|---:|---:|
| CONFIRMED_PRODUCT | 44 | 330 |
| MIXED_PRODUCT_SERVICE | 12 | 74 |
| POTENTIAL_PRODUCT | 744 | 165 |
| CONFIRMED_SERVICE | 23 | 1,536 |
| CONFIRMED_CONSTRUCTION | 4 | 542 |
| PROBABLE_NON_PRODUCT | – | 245 |
| VENDOR_QUALIFICATION | 57 | 88 |
| OTHER_INCOMPATIBLE | 19 | 131 |
| INSUFFICIENT_EVIDENCE | 2,789 | 578 |
| Status (federal/closed/cancelled/duplicate) | 4,396 | 4,399 |

Readiness: 0 READY_FOR_SOURCING_REVIEW (no record has retained full solicitation documents with verified quantities and submission instructions); 404 product records `PRODUCT_CONFIRMED_NEEDS_SOLICITATION_DETAIL`; 165 `PRODUCT_CATEGORY_UNVERIFIED`; 578 unresolved.

Export: `audit_exports/PROCUREMENT_ENGINE_REPAIR_EXPORT/` (per-record A original evidence, B human-readable, C decision with quotes, D trace; validation ok). Row index archived as `docs/acceptance_archive/PROCUREMENT_ENGINE_REPAIR_EXPORT_MASTER_INDEX.jsonl` (21 MB, not committed).

## Measured accuracy (blind labels; annotators never saw engine output)

Two independent annotators per record, agreement 97–99%; disagreements adjudicated from record text only. **Caveat:** annotators are AI models of the same family; this is not human ground truth.

### Held-out random samples (product-side = confirmed + potential + mixed)

| Set | Engine | Product precision | Product recall | Non-products called product | Clear non-products unresolved |
|---|---|---:|---:|---:|---:|
| R1 (240) | 002R.2 shipped | 37% | 35% | 29 | 154 |
| R1 | frozen 5d5d294 (clean) | 91% | 63% | 3 | 41 |
| R2 (200) | 002R.2 shipped | 56% | 44% | 16 | 131 |
| R2 | frozen aaa772e (clean) | 84% | 60% | 5 | — |
| R3 (200) | 002R.2 shipped | 51% | 59% | 20 | 135 |
| R3 | frozen 649e7d3 (clean) | 82% | 59% | 5 | 20 |
| R3 | final classifier (excl. 3 inspected) | 88% | 60% | 3 | 20 |

R1/R2 numbers for later versions are contaminated (fixes informed by their errors) and are not shown as estimates.

### Stratified sample of the reprocessed inventory buckets

| Bucket | Sample | Correct (product vs non-product) | Strict |
|---|---:|---:|---:|
| CONFIRMED_PRODUCT — fresh, never inspected, final engine | 49 | **84%** (≈71–92% 95% CI) | — |
| POTENTIAL_PRODUCT — fresh, final engine | 24 | 75% are products | — |
| CONFIRMED_SERVICE | 25 | 96% | 80% |
| CONFIRMED_CONSTRUCTION | 25 | 100% | 92% |
| PROBABLE_NON_PRODUCT | 24 | 83% | — |
| MIXED_PRODUCT_SERVICE | 24 | 75% | 58% |
| INSUFFICIENT_EVIDENCE | 25 | 4 products, 19 non-products, 2 undeterminable | — |

(Non-product buckets measured on commit f931ccc; the final calibration only changed product confidence and rental detection.)

Extrapolated: roughly 40–95 of the 330 CONFIRMED_PRODUCT records are not product purchases. **This engine is not 100% accurate and should not be described as such.**

## Auditable examples (quotes from export C files)

| Record | 002R.2 | Repaired | Quoted evidence |
|---|---|---|---|
| NB00279755 Per Diem Nursing ITQ | CONFIRMED_PRODUCT | CONFIRMED_SERVICE | title "Per Diem Nursing ITQ"; naics:621491 |
| NB00281812 Shuttle Bus Services | CONFIRMED_PRODUCT | CONFIRMED_SERVICE | title "Shuttle Bus Services"; naics:485999 |
| NB00260545 Purchase of Publicly-Assisted Affordable Housing | CONFIRMED_PRODUCT | OTHER_INCOMPATIBLE | title.action_of:purchase → real property |
| NB00359286 Asbestos Abatement… | CONFIRMED_PRODUCT | CONFIRMED_SERVICE | summary "Contractor shall provide cleaning, removal, transporting, and disposal…" |
| NB00343726 Milk and Dairy Products | INSUFFICIENT_EVIDENCE | CONFIRMED_PRODUCT | summary "seeking bids … for the provision of milk and dairy products" |

## Known limitations (remaining errors seen in blind samples)

- **Lexicon long tail:** titles with nouns the engine doesn't know (e.g. "Tuna, Canned & Pouched", "Retail Merchandising Units") fall back on summary boilerplate → product recall ≈60% on random samples.
- **Structures read as goods:** "Ground Storage Tank", "Salt Shed", "Equipment Foundation", "Chemical Feed Building".
- **Installed building systems** (fire alarm, HVAC, generators) are surfaced as POTENTIAL product-with-installation; annotators usually call these construction.
- **Service of goods / software renewals** ("Parts and Repair Services…", "Secret Server Cloud Renewal") sometimes confirmed as product.
- **Government sales/non-solicitations** without sale words (surplus vehicle "Accepting Bids for 2007 …"; research program notices).
- **Service vs construction** confusion is common, but it is non-product either way.

## Hygiene

- Scratch work (labels, sets, scoring scripts, fingerprints) lives in `C:\Users\M_gra\m3-worktrees\review_scratch` — outside both repositories; nothing temporary in `scripts/` or `docs/`.
- No duplicate modules; changes are in the canonical `m3/research/classify.py`, the existing audit module and `jobs.py`.
- App imports (`app` → FastAPI) with the worktree code; killswitch locked.

## Owner decisions requested

1. Accept/merge the branch into `main` (main currently has its own uncommitted 002J–002R.2 work; the branch base snapshot `a8ca37b` captures it).
2. Whether POTENTIAL_PRODUCT and PROBABLE_NON_PRODUCT should be shown in the supplier dashboard or held for review.
3. Whether to authorize acquiring full solicitation documents (currently locked) — required before any record can be READY_FOR_SOURCING_REVIEW.

# BUILD 002Q — Document Reconcile

Finished: 2026-11-22T20:59:20.573183+00:00

## 1. Why downloaded packages “did not match” freeze keys

| Fact | Count |
|---|---:|
| Freeze unique rows | 23091 |
| SolicitationPackage rows | 7090 |
| Packages with downloads | 1493 |
| Freeze↔package match (any method) | **14036** |
| …of which with downloads | 1716 |
| Still unmatched | 9055 |

**Match methods (repaired):**

| Method | Count |
|---|---:|
| `normalized_url` | 10508 |
| `external_key` | 3284 |
| `solicitation_number` | 244 |
| unmatched | 9055 |

**Root cause (exact):** Prior qualify recovery reported 0 package quantities because:

1. **Join was not the main failure for products** — 320/390 product rows already shared `external_key` with a package.
2. **Stored excerpts were junk/thin** — 13,221 linked rows had portal chrome or empty stubs (e.g. Mesa “Employee Data Portal” HTML). Usable text after repair: **815**. PDF re-extract found **0** additional PDFs among those junk blobs (blobs were HTML, not PDF).
3. **Alternate key schemes** for the minority: Georgia `gt:ga:…` vs hash, truncated query strings — fixed via solicitation number + normalized URL + opportunity id.

## 2–3. Association repair + full reconcile

- Multi-key association in `m3/research/package_association.py` (canonical; no temp scripts).
- All **23,091** freeze rows reconciled against all **7,090** packages.
- Qualify reprocess: **14,062** rows linked to package evidence; **55** quantities recovered from package classification hints.

## 4. Blockers for all 390 product opportunities

| Blocker | Count |
|---|---:|
| `missing_quantity` | **372** |
| Stage C (no product blocker) | **18** |

No arbitrary filter changes. Missing quantity remains the dominant product gate after document reconcile.

## 5. Why Stage C opportunities remain (mostly) ineligible

### Freeze Stage C after reconcile: **18** · Bid-eligible: **2**

| Title | Qty | Status | Failed gates |
|---|---:|---|---|
| RFB FVFD-2026-01 SCBA / Facepieces | 5 | open | *(none — bid-eligible)* |
| Tractor Truck | 1 | open | *(none — bid-eligible)* |
| 2026-28 Ford F550 Mini Rescue (×7) | 7 | open | submission_instructions |
| Five Custom Class A Pumpers | 5 | open | submission_instructions |
| Pumper Fire Truck | 1 | open | submission_instructions |
| Polaris Ranger 1000 | 1 | open | submission_instructions |
| Full-Port Glass Lined Plug Valve | 1 | open | submission_instructions |
| 27-015 Instructional Supplies | 1200 | open | submission_instructions |
| Live Worms (250/cup) ×2 rows | 250 | open | reseller, bond/insurance, submission, sufficient_docs |
| Hydrant Pressure Recorder (300) ×2 | 300 | open | reseller, bond/insurance, submission, sufficient_docs |
| Athletic Equipment Uniforms | 2 | unknown | active_solicitation, submission |
| Next Generation 911 Evolution | 1 | unknown | active_solicitation |
| NJ bucket truck / incident mgmt notices ×4 | various | unknown | active_solicitation, submission |

**Rule unchanged:** Stage C = product + firm ordered quantity + individual. Bid-eligible additionally requires evidenced active status, submission instructions, sufficient documents, reseller terms, and no blocking install/bond/license. Pricing/financing/supplier stock are not used.

### 002P retained Stage C: **64** · Bid-eligible: **0**

Gate failures on retained 002P rows (unknown status / thin local docs in this free freeze):

| Gate | Failures |
|---|---:|
| active_solicitation | 64 |
| submission_instructions | 64 |
| sufficient_documents | 61 |
| reseller_eligibility | 54 |
| no_blocking_install_insurance_bond_license | 54 |

## 6. Evidence recovered from existing documents

| Recovery | Count |
|---|---:|
| Package quantity hints applied | 55 |
| Quantities from package body text | 0 |
| Usable non-junk package text rows | 815 |
| PDF blob re-extracts | 0 (junk blobs were HTML) |

## 7. After full inventory reprocess

| Metric | Count |
|---|---:|
| Unique population | 23091 |
| Verified individual | 1224 |
| Product opportunities | 390 |
| Stage C | **18** |
| Bid-eligible | **2** |
| Ready-to-submit | **2** |

Constraints held: no acquire restart · NextBid locked · no paid search · app import OK.

Artifacts: `docs/acceptance_archive/BUILD_002Q_DOCUMENT_RECONCILE.json`, `data/acceptance/jobs_document_reconcile_last.json`.

Awaiting owner acceptance.

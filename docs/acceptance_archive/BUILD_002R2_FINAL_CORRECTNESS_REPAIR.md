# BUILD 002R.2 — Eliminate False Classifications & Unreliable Recovery Targeting

**Status:** COMPLETE offline. Killswitch LOCKED. **STOP — await owner review.**  
**API expenditure:** 0  
**Export:** `govtracker/audit_exports/BUILD_002R2_VERIFICATION_EXPORT`  
**Runtime:** 553.07s · **8088/8088**

---

## 1. Corrected classification totals

| Class | Count |
|---|---:|
| FEDERAL_PARKED | 4288 |
| INSUFFICIENT_EVIDENCE | 2789 |
| POTENTIAL_PRODUCT | 744 |
| VENDOR_QUALIFICATION | 57 |
| CLOSED_EXPIRED | 49 |
| DUPLICATE | 55 |
| CONFIRMED_PRODUCT | 44 |
| CONFIRMED_SERVICE | 23 |
| OTHER_INCOMPATIBLE | 19 |
| MIXED_PRODUCT_SERVICE | 12 |
| CANCELLED | 4 |
| CONFIRMED_CONSTRUCTION | 4 |

## 2. Confirmed false-product corrections

| ID | Was (002R.1) | Now |
|---|---|---|
| NB00339526 (Vehicle Code / surveys) | CONFIRMED_PRODUCT | INSUFFICIENT_EVIDENCE (not product; listing-only demotes service confirm) |
| NB00219946 (booth installation) | CONFIRMED_PRODUCT | CONFIRMED_SERVICE |
| NB00268024 (furniture pre-qual install) | CONFIRMED_PRODUCT | VENDOR_QUALIFICATION |
| NB00275434 (fuel equipment repair pre-qual) | CONFIRMED_PRODUCT | VENDOR_QUALIFICATION |

Root fix: primary procurement action before bare product nouns; Vehicle Code / CVEF noise filtered; pre-qual / installation / survey / repair actions cannot be overridden by FURNITURE/EQUIPMENT/Vehicle word hits.

## 3. False rejections

Regression suite all passed (boom, forklift, kV, hours, four false-product IDs).  
Aggregate vs 002R.1: CONFIRMED_SERVICE −247, CONFIRMED_CONSTRUCTION −107 (listing-summary can no longer confirm those without package evidence — unresolved kept visible as INSUFFICIENT).

## 4. Evidence-completeness breakdown (verifiable fields)

| Dimension (true count) | N |
|---|---:|
| complete_retained_provider_response | 8088 |
| listing_summary_only | 7920 |
| complete_available_listing_description | 2215 |
| submission_requirements_available | 199 |
| full_opportunity_detail_response | 168 |
| original_solicitation_document_content_retained | 93 |
| complete_specifications_available | 2 |

Legacy misleading `full_source_evidence` removed; replaced by `substantial_retained_package` only when detail + non-preview document content exist. A ≤500-char listing preview is never complete.

## 5. Reconciled quantities

| Metric | Value |
|---|---:|
| Records with quantity_verified | **119** |
| Complete source-backed line items on those records | **138** |
| All extracted complete lines (incl. non-product classes) | 211 |
| quantity_verified with zero lines | **0** |
| Stale VERIFIED status without quantity_verified | **0** |

## 6–7. Active SL / product candidates

- Active state/local in scope: **3689**
- Evidence-backed product recovery candidates: **798**
- Unacquired listing pages (coverage): **123** across 4 incomplete partitions

## 8–10. Paid recovery (NOT AUTHORIZED)

- Prior 523-call plan **rejected**
- Full ranked inventory: `PAID_RECOVERY_FULL_RANKED.jsonl` (**3689** rows — not a 25-sample)
- Top 50 call sequences in `PAID_RECOVERY_PLAN.json`
- Top-50 estimated credits to meaningful requirements: **34**
- Limits retained: 100/day, 50/job, 1000 reserve, killswitch LOCKED

## 11. Unacquired listing coverage

123 listing pages remaining (`GET /opportunities`, page_size=25) — lower priority than product detail.

## 12. Evidence-vault integrity

Durability checks **ok=true** (atomic persist, no truncate, history, merge_thicker, cache reuse).

## 13. Change accounting

- Row-level 002R/002R.1 MASTER_INDEX **unavailable on disk** (prior export trees were cleared) — reported explicitly, not invented.
- Aggregate deltas vs archived 002R.1 ACCEPTANCE_SUMMARY included in export.
- This run’s MASTER_INDEX archived to `docs/acceptance_archive/BUILD_002R2_VERIFICATION_EXPORT_MASTER_INDEX.jsonl` for future row-level diffs.

## 14. Unresolved defects / caveats

- Most inventory remains listing-summary-only (7920) — cannot claim complete solicitation packages.
- CONFIRMED_PRODUCT is intentionally strict (44); many real goods may sit in POTENTIAL until purchase/furnish language or package text is retained.
- NB00339526 is unresolved (INSUFFICIENT) rather than confirmed professional_services because listing-only cannot prove complete requirements.
- Independent human semantic audit still required — tests passing ≠ semantic verification.

## 15. Runtime / zero-spend

553.07s · api_expenditure **0** · acquisition_locked **true** · validation_ok **true**

**STOP. No acquisition. Awaiting owner review.**

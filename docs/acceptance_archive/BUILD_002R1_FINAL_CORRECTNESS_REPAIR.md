# BUILD 002R.1 — Final Correctness Repair & Acquisition Recovery Preparation

**Status:** COMPLETE offline. Killswitch LOCKED. **STOP — await owner authorization.**  
**API expenditure:** 0 (zero paid NextBid / OpenAI / SAM / DIBBS).  
**Export:** `govtracker/audit_exports/BUILD_002R1_VERIFICATION_EXPORT`  
**Runtime:** 407.38s for full 8,088 reprocess + export.

---

## 1. Confirmed root causes fixed

| Defect | Root cause | Fix |
|---|---|---|
| NB00326707 triplicate/duplicate boom-lift lines | Title echoed in body → two identical WORD_PAREN matches → wrongly promoted to MULTI_LINE_ITEM | Deduplicate by `(quantity, normalized product)` + span; single unique requirement → `ORDERED_QUANTITY` |
| False quantities (35 kV, 24 hours) | Unit/context accepted as ordered qty | Retained REJECT_QTY_UNITS / NON_QUANTITY_CONTEXT; revalidated offline |
| Over-confident classifications / rejections | Category labels & rule names treated as proof; listing summaries confirmed too aggressively | Require source quotations; demote confirmed labels without quotes; listing-only cannot confirm service/construction; category-alone → unresolved |
| Evidence vault fragility | Direct OneDrive writes / overwrite without history | Atomic write + `.pending` / `%LOCALAPPDATA%` fallback; historical_versions on hash change; `merge_thicker`; never truncate body |

---

## 2. Regression test results

**Unit/integration (pytest):** 29 passed (`test_procurement_intelligence_002r`, `test_extract_and_fit`, `test_nextbid_permanent_repair_002q`).

**Live inventory spot checks (export packages):**

| ID | Result |
|---|---|
| NB00326707 | PASS — qty=1, `ORDERED_QUANTITY`, 1 line (boom lift + trailer package) |
| NB00320329 | PASS — `MULTI_LINE_ITEM`, 4 distinct lines (2 reach / 1 sit-down / 1 pallet / 4 chargers) |
| NB00341368 | PASS — no quantity from 35 kV |
| NB00291906 | PASS — no quantity from 24-hour support; confirmed service |

Identical duplicate line-item records across inventory after repair: **0**.

---

## 3. Updated classification counts (8,088)

| Class | Count |
|---|---:|
| FEDERAL_PARKED | 4288 |
| INSUFFICIENT_EVIDENCE | 2399 |
| CONFIRMED_PRODUCT | 382 |
| POTENTIAL_PRODUCT | 377 |
| CONFIRMED_SERVICE | 270 |
| CONFIRMED_CONSTRUCTION | 111 |
| MIXED_PRODUCT_SERVICE | 102 |
| DUPLICATE | 55 |
| CLOSED_EXPIRED | 52 |
| VENDOR_QUALIFICATION | 27 |
| OTHER_INCOMPATIBLE | 21 |
| CANCELLED | 4 |

Vs prior 002R export: CONFIRMED_PRODUCT −388, CONFIRMED_SERVICE −1028, CONFIRMED_CONSTRUCTION −107, INSUFFICIENT_EVIDENCE +1161 — expected after evidence-quotation gates (not count inflation).

---

## 4. Correctly verified product line-item count

- Records with verified quantities: **165**
- Extracted verified product line items (sum of lines): **119**
- Multi-line schedule records: **21**
- Identical-duplicate line records: **0**

---

## 5. Quantity-extraction errors corrected

- Boom-lift title/body echo no longer creates duplicate MULTI_LINE rows.
- Voltage (kV) and support-hours no longer become product quantities.
- Distinct multi-product schedules preserved (forklift + chargers).

---

## 6. Confirmed inappropriate rejections corrected

- Prior inventory rejections overturned this run: **373**
- Service/construction confirmations without source quotations blocked → unresolved.
- Category-label-only construction/service paths no longer auto-confirm.

---

## 7. Records requiring more original evidence

| Completeness | Count |
|---|---:|
| full_source_evidence | 1185 |
| partial_source_evidence | 6903 |
| missing_original_evidence | 0 |

INSUFFICIENT_EVIDENCE primary: **2399** (visible for investigation; not silent-rejected).

---

## 8. Evidence-vault durability test results

All checks **ok=true**:

- atomic_persist
- body_not_truncated
- historical_versions_retained
- merge_thicker_keeps_longer_text
- reuse_cached_paid_response
- store_root_exists
- content_hash_stable

Artifacts: `EVIDENCE_VAULT_DURABILITY.json` in export folder.

---

## 9. Prioritized paid-recovery plan (NOT EXECUTED)

Credits baseline: used **3453** / remaining **2547** / reserve **1000** / max spendable **1547**.  
Page size max supported by client: **25**. Daily & per-job limits retained. Killswitch stays LOCKED.

| Bucket | Population (uncapped) | Meaning | Endpoint / action |
|---|---:|---|---|
| A | 123 listing pages (4 incomplete partitions) | Unacquired active SL listings | `GET /opportunities` page_size=25 |
| B | 399 | Sufficient retained evidence — **no calls** | Offline only |
| C | 2438 | Need provider detail | `GET /opportunities/{nb_id}` |
| D | 423 | Need document listing | `GET /opportunities/{nb_id}/documents` |
| E | 0 staged | Specific solicitation documents | Selective download after D |
| F | (reprocess path) | Docs stored but processed wrong | Zero-spend reprocess |

**Authorized spend proposal if owner approves (capped, not executed):** C≤200 + D≤200 + A≤123 = **523 credits** (within 1547). Prioritize product/potential/mixed detail (C) over listing expansion (A). Full plan: `PAID_RECOVERY_PLAN.json`.

Do **not** auto-schedule. Listing pages do not equal full solicitation detail.

---

## 10. Runtime and zero-spend verification

- Inventory accounted: **8088 / 8088**
- Validation ok: **true**
- Elapsed: **407.38s**
- `api_expenditure`: **0**
- `acquisition_locked`: **true** throughout
- ZIP archives: **33** parts

**Disclaimer:** Automated tests and export consistency checks passed. Semantic accuracy has **not** been independently human-verified solely because tests passed — use the export for independent review.

---

**STOP. Awaiting owner authorization before any paid acquisition.**

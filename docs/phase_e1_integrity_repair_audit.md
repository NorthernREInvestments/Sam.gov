# Phase E.1 — Integrity Repair Audit

**Date:** 2026-09-22  
**Prior baseline:** 1711 passed / 0 failed (Phase E)  
**Scope:** Correctness only — no architecture redesign, no Phase F, no live opportunity testing.

---

## 1. UNKNOWN-readiness bug

| | |
|--|--|
| **Original bug** | `evaluate_owner_approval_gate` blocked only when fields were explicitly `False`. `None` / UNKNOWN economics, financing, or supplier quote could pass. |
| **Root cause** | Negative checks (`if x is False`) instead of positive confirmation (`if x is not True`). Profile also treated public pricing evidence as `supplier_quote_present`. |
| **Affected files** | `execution_requirements/readiness.py`, `execution_requirements/profile.py` |
| **Repair** | Tri-state-safe gate: required fields pass **only** when `is_explicitly_true(...)`. Resolvers return `None` for UNKNOWN. Profile passes full `row` into the gate; public-price→quote path removed. |
| **Regression test** | `test_fr001_*`, `test_fr002_*`, `test_unknown_never_passes_is_explicitly_true` |
| **Remaining limitation** | Fixture flag `owner_readiness_fixture_confirm_all` exists for curated positive goldens; live deals must still supply explicit confirms. |

## 2. Stale BID_PREPARATION / UI override

| | |
|--|--|
| **Original bug** | UI showed READY when `gateReady \|\| allPass \|\| APPROVAL_STATES.has(BID_PREPARATION)`. |
| **Root cause** | Multiple authorities for READY in `static/m3-mobile.js`. |
| **Affected files** | `static/m3-mobile.js`, dashboard “Awaiting approval” filter |
| **Repair** | READY banner and card badges use **only** `owner_approval_gate.ready_for_owner_approval === true` (via `isOwnerReady` / `owner_ready_banner_allowed`). |
| **Regression test** | `test_fr004_*`, `test_ui_js_no_longer_overrides_gate_with_bid_prep` |
| **Remaining limitation** | `APPROVAL_STATES` still used for soft portfolio buckets, not READY banners. |

## 3. Dashboard vs deep-dive enrichment inconsistency

| | |
|--|--|
| **Original bug** | Deal Room attached execution compliance before workflow; dashboard cards projected workflow from stored state only. |
| **Root cause** | Dual paths — `attach_execution_compliance` only on deal-room. |
| **Affected files** | `m3_mobile_read_model.py`, `operator_workflow/summary.py`, `execution_requirements/enrichment.py` |
| **Repair** | Canonical `enrich_deal_for_operator`: profile → blockers → supplier/econ/finance → owner gate → workflow → summary. Used by cards, deal room, and `build_operator_dashboard_payload(enrich=True)`. |
| **Regression test** | `test_fr005_*`, PR consistency asserts |
| **Remaining limitation** | Enriching every dashboard row is heavier; no caching yet. |

## 4. Public price vs supplier-validation conflation

| | |
|--|--|
| **Original bug** | Public/market price or commercial seller treated as quote present / supplier path. |
| **Root cause** | `_supplier_quote_present` inspected `Pricing_Evidence` items. |
| **Affected files** | `execution_requirements/supplier_state.py`, `profile.py`, `readiness.py` |
| **Repair** | Supplier ladder: `NO_SUPPLIER_SIGNAL` → `COMMERCIAL_SOURCE_FOUND` → … → `SUPPLIER_VALIDATED` → `QUOTE_EXECUTABLE`. Public price alone never validates or executes. |
| **Regression test** | `test_fr003_*`, `test_fr006_*`, BP_001 |
| **Remaining limitation** | No live supplier quote API; formal quote must be on the row. |

## 5. Misleading workflow labels

| Old | New |
|-----|-----|
| SUPPLIER_VALIDATION → Supplier Contacted | Supplier Validation |
| FINANCE_REVIEW → Quote Received | Financing Review |
| BID_PREPARATION → Ready For Approval | Bid Preparation |
| DISCOVERED → New | Discovered |
| DEEP_RESEARCH → Researching | Deep Research |

**Regression test:** `test_ui_js_no_longer_overrides_gate_with_bid_prep`

## 6. All-negative golden corpus weakness

| | |
|--|--|
| **Original bug** | All CASE_001–018 expected `ready_for_owner_approval=false`. |
| **Repair** | CASE_019/020/021 positive READY goldens; PR-001–003 unit tests. |
| **Regression test** | `test_positive_golden_corpus_cases`, `test_pr00*` |

## 7. Validation depth limitation

| | |
|--|--|
| **Original bug** | Harness labeled as end-to-end while only exercising execution-slice path. |
| **Repair** | Honest depth tags + report breakdown: execution-path / broader-pipeline / adversarial / positive-ready. Broader mode uses `enrich_deal_for_operator` + richer expected truth (BP_001–005). |
| **Regression test** | `test_broader_pipeline_cases_depth` |
| **Remaining limitation** | Still no live source retrieval, real WAWF/DIBBS submission, or real quote integrations. |

---

## Owner / VA role semantics

UI controls renamed to **View as Owner** / **View as Operator**. Copy states this is display preference only — **not** authentication or RBAC. Backend permissions are out of scope for E.1.

---

## Canonical owner-ready authority

`execution_requirements.readiness.evaluate_owner_approval_gate`  
→ `owner_approval_gate.ready_for_owner_approval`

Consumers: enrichment, profile, mobile APIs, UI banners. Legacy `readiness_summary`, lifecycle READY, BID_PREPARATION, checklist allPass are **informational only**.

---

## Provenance

Gate returns `provenance` for satisfied required fields and `supplier_execution_state` with quote/cost flags. Sufficient for later “Why ready?” UI — no new UI in E.1.

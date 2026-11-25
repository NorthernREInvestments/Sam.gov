"""End-to-end NextBid procurement pipeline (retrieve → persist → extract → verify → owner card).

Offline-first: plans the cheapest justified metered call and processes retained evidence.
Does **not** execute metered NextBid requests unless an explicit owner-authorized runner
is invoked separately (killswitch remains locked by default).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.classify import assess_product_presence, classify_requirement
from m3.research.extract_fields import extract_quantity_fields, normalize_quantity_unit
from m3.research.full_text_store import load_full_text, store_full_text
from m3.research.nextbid_acquire import nextbid_file_text
from m3.research.nextbid_controller import acquisition_locked, controller_status
from m3.research.nextbid_inventory import load_inventory
from m3.research.nextbid_offline_recovery import load_enrichment_index, load_recovery_package
from m3.research.nextbid_response_store import (
    content_hash,
    load_by_request_id,
    persist_paid_response,
    request_identity,
    verify_persisted_response,
)
from m3.research.procurement_requirements import extract_requirements

# Selected controlled-test opportunity (Phase 1): state/local bulk propane product supply
# with a known unread document0.md path. Courtroom furniture (NB00321439) rejected:
# retained listing text is primarily remove/build/install furniture (construction/install).
PIPELINE_TEST_NB_ID = "NB00375309"
PIPELINE_TEST_ARTIFACT = "nb2/published/opengov/document0/NB00375309/NB00375309.document0.md"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_test_opportunity() -> dict[str, Any]:
    """Document why NB00375309 is the pipeline test case (and why furniture was not)."""
    inv = load_inventory().get(PIPELINE_TEST_NB_ID) or {}
    pkg = load_recovery_package(PIPELINE_TEST_NB_ID) or {}
    ft = load_full_text(PIPELINE_TEST_NB_ID) or {}
    rejected = {
        "nb_id": "NB00321439",
        "title": "Oliver Hill New Courtroom Furniture",
        "rejected_because": (
            "Retained listing describes disassembly, building and installation of courtroom "
            "furniture across 5 courtrooms — primarily construction/install, not a simple "
            "physical-product purchase."
        ),
        "artifact_was_available": "nb2/published/opengov/document0/NB00321439/NB00321439.document0.md",
    }
    missing = [
        "full_notice_body_document0_md_not_retrieved",
        "firm_ordered_or_estimated_propane_quantity_UNKNOWN",
        "unit_price_basis_UNKNOWN",
        "delivery_schedule_terms_beyond_listing_UNKNOWN",
        "technical_specs_from_Bulk_Propane_Fuel_Supply.pdf_not_retrieved_as_bytes",
        "submission_method_detail_UNKNOWN",
        "eligibility_set_aside_UNKNOWN",
        "live_open_status_UNVERIFIED",
    ]
    return {
        "nb_id": PIPELINE_TEST_NB_ID,
        "title": pkg.get("title") or inv.get("title"),
        "agency": pkg.get("agency") or inv.get("agency") or "pembrokepines",
        "solicitation_number": pkg.get("solicitation_number") or inv.get("solicitation_number") or "CS-2026-QQ-02",
        "source": inv.get("source") or "opengov",
        "jurisdiction": "state_local",
        "official_url": pkg.get("source_url") or inv.get("official_url"),
        "deadline_retained": inv.get("response_deadline") or inv.get("deadline_at"),
        "lifecycle_retained": inv.get("lifecycle_status") or inv.get("solicitation_status"),
        "listing_description": pkg.get("description") or ft.get("full_text"),
        "listing_description_chars": len(str(pkg.get("description") or ft.get("full_text") or "")),
        "product_rationale": (
            "City of Pembroke Pines seeking quotes for on-demand delivery of bulk liquid propane "
            "to an existing 5,000-gallon stationary tank — tangible goods (LP fuel) purchase."
        ),
        "known_artifact": {
            "endpoint": "read_file",
            "path": PIPELINE_TEST_ARTIFACT,
            "bytes_declared": 8364,
            "kind": "document0",
            "already_retrieved_locally": False,
            "max_credit_cost": 1,
        },
        "already_paid_endpoints": pkg.get("endpoints_present") or [],
        "source_documents_metadata_count": pkg.get("source_documents_count") or len(pkg.get("source_documents") or []),
        "source_documents_sample": [
            {"name": d.get("name"), "stored": d.get("stored"), "sha256": d.get("sha256")}
            for d in (pkg.get("source_documents") or [])[:5]
        ],
        "missing_fields": missing,
        "rejected_alternative": rejected,
        "selected_at": _utc(),
    }


def _prefer_document0_path(paths: list[str]) -> str | None:
    if not paths:
        return None
    for p in paths:
        if p.endswith(".document0.md"):
            return p
    return paths[0]


def plan_cheapest_retrieval(nb_id: str) -> dict[str, Any]:
    """Decision order: local evidence → durable paid store → indexes → known artifact.

    Never proposes re-purchase of an unchanged artifact already retained locally.
    """
    from m3.research.solicitation_document_manifest import file_artifact_retention

    pkg = load_recovery_package(nb_id) or {}
    ft = load_full_text(nb_id) or {}
    inv = load_inventory().get(nb_id) or {}
    steps_checked: list[dict[str, Any]] = []

    def check(name: str, present: bool, detail: str) -> None:
        steps_checked.append({"step": name, "present": present, "detail": detail})

    local_doc = str(ft.get("full_text") or "")
    check(
        "1_local_full_text",
        len(local_doc) >= 800 and not ft.get("is_listing_preview_only"),
        f"chars={len(local_doc)} preview_flag={ft.get('is_listing_preview_only')} source={ft.get('source')}",
    )
    listing = str(pkg.get("description") or inv.get("title") or "")
    check("2_listing_description", len(listing) >= 200, f"chars={len(listing)}")
    eps = set(pkg.get("endpoints_present") or [])
    check("3_retained_get_opportunity", "get_opportunity" in eps, f"endpoints={sorted(eps)}")
    check(
        "4_retained_document_index",
        "list_documents" in eps or bool(pkg.get("source_documents")),
        f"source_documents={pkg.get('source_documents_count')}",
    )
    md_paths = [
        str(a.get("path"))
        for a in (pkg.get("nextbid_documents") or [])
        if str(a.get("path") or "").endswith(".md")
    ]
    # Also accept file_index / PIPELINE default path
    if not md_paths and PIPELINE_TEST_NB_ID == nb_id:
        md_paths = [PIPELINE_TEST_ARTIFACT]
    path = _prefer_document0_path(md_paths)
    retention = file_artifact_retention(nb_id, path) if path else {
        "already_retained": False,
        "durable_response_present": False,
        "durable_text_chars": 0,
        "persistence_ok": False,
        "http_path": None,
        "content_hash": None,
        "original_bytes_sha256": None,
    }
    recovered_md = any(
        ".md" in str(t.get("path") or "") for t in (pkg.get("recovered_file_texts") or [])
    )
    md_already = bool(
        retention.get("already_retained")
        or recovered_md
        or (
            len(local_doc) >= 800
            and not ft.get("is_listing_preview_only")
            and path
            and path.endswith(".md")
        )
    )
    check(
        "5_known_document0_md",
        bool(md_paths),
        (
            f"paths={md_paths[:2]} already_local={md_already} "
            f"durable={retention.get('durable_response_present')} "
            f"durable_chars={retention.get('durable_text_chars')} "
            f"sha={retention.get('original_bytes_sha256')}"
        ),
    )

    proposed = None
    stop_reason = None
    if md_already and path:
        stop_reason = "artifact_already_retained_locally"
        steps_checked.append(
            {
                "step": "6_duplicate_purchase_guard",
                "present": True,
                "detail": (
                    f"Blocked re-buy of {path}: durable_store="
                    f"{retention.get('durable_response_present')} "
                    f"persistence_ok={retention.get('persistence_ok')} "
                    f"full_text_chars={len(local_doc)}"
                ),
            }
        )
    elif md_paths and path and not md_already:
        proposed = {
            "endpoint": "read_file",
            "nb_id": nb_id,
            "path": path,
            "http_path": f"/opportunities/{nb_id}/files/{path}",
            "reason": (
                "Known document0 markdown artifact indexed by prior list_documents/list_files; "
                "not present in durable paid-response store or full_text."
            ),
            "missing_requirement_targeted": "notice_body_quantities_specs_submission",
            "max_credit_cost": 1,
            "skips": ["get_opportunity", "list_documents", "list_files"],
            "skip_rationale": "Indexes already retained; re-listing would re-spend without new discovery.",
            "stopping_condition": "After this single read_file, stop. Do not auto-fetch PDFs.",
            "preflight_checks": {
                "exact_artifact_path": path,
                "durable_response_present": retention.get("durable_response_present"),
                "content_hash": retention.get("content_hash"),
            },
        }
    elif "get_opportunity" not in eps:
        proposed = {
            "endpoint": "get_opportunity",
            "nb_id": nb_id,
            "path": None,
            "http_path": f"/opportunities/{nb_id}",
            "reason": "No opportunity detail retained and no known file artifact.",
            "missing_requirement_targeted": "description_and_document_hints",
            "max_credit_cost": 1,
            "stopping_condition": "Re-evaluate after detail; do not chain calls automatically.",
        }
    else:
        stop_reason = "no_justified_metered_path_without_known_artifact"

    return {
        "nb_id": nb_id,
        "steps_checked": steps_checked,
        "proposed_metered_call": proposed,
        "stop_without_call": stop_reason,
        "already_retained_artifact": {
            "path": path,
            "retained": md_already,
            "retention": retention if path else None,
        },
        "killswitch_locked": acquisition_locked(),
        "ledger": (controller_status().get("budget_state") or {}),
    }


def extract_from_retained_text(
    *,
    title: str,
    description: str | None,
    document_text: str | None,
    agency: str | None = None,
    solicitation_number: str | None = None,
    official_url: str | None = None,
    deadline_raw: str | None = None,
) -> dict[str, Any]:
    """Extract requirements with source-backed fields; UNKNOWN when absent."""
    blob = "\n".join(x for x in (description, document_text) if x)
    clf = classify_requirement(title, description=description, document_text=document_text)
    presence = assess_product_presence(
        title, description=description, document_text=document_text
    )
    reqs = extract_requirements(title, description=description, document_text=document_text)
    qty = extract_quantity_fields(blob, title=title)

    # Independent validation snippets
    validations: list[dict[str, Any]] = []
    for li in reqs.get("line_items") or []:
        uom = li.get("unit_of_measure")
        if uom and normalize_quantity_unit(str(uom)) is None:
            validations.append({
                "check": "uom_not_grounded",
                "pass": False,
                "detail": f"rejected_uom={uom}",
            })
            li["unit_of_measure"] = None
            li["complete"] = bool(li.get("description") and li.get("quantity") is not None and li.get("unit_of_measure"))
        elif uom:
            validations.append({"check": "uom_grounded", "pass": True, "detail": uom})
        q = li.get("quantity")
        if q is not None:
            # quantity must appear near product language in source when possible
            q_str = str(int(q)) if float(q) == int(q) else str(q)
            validations.append({
                "check": "quantity_in_source",
                "pass": q_str in blob or q_str in title,
                "detail": f"qty={q_str}",
            })

    brand_or_equal = (reqs.get("solicitation_requirements") or {}).get("brand_name_or_equal")
    brand_only = (reqs.get("solicitation_requirements") or {}).get("brand_name_only")
    if not brand_or_equal and not brand_only:
        brand_status = "UNKNOWN_silence_is_not_permission"
    elif brand_only:
        brand_status = "BRAND_NAME_ONLY"
    else:
        brand_status = "BRAND_OR_EQUAL"

    sreq = reqs.get("solicitation_requirements") or {}
    submission = sreq.get("submission") or None

    return {
        "solicitation": {
            "agency": agency or "UNKNOWN",
            "solicitation_number": solicitation_number or "UNKNOWN",
            "title": title,
            "official_url": official_url or "UNKNOWN",
            "deadline_retained": deadline_raw or "UNKNOWN",
            "deadline_timezone": "UNKNOWN" if not deadline_raw else "see_source_timestamp",
            "addenda": "UNKNOWN",
            "submission_method": submission[0] if isinstance(submission, list) and submission else (
                submission if isinstance(submission, str) else "UNKNOWN"
            ),
        },
        "classification": {
            "requirement_class": clf.get("requirement_class"),
            "product_presence": presence.get("product_presence"),
            "evidence_completeness": presence.get("evidence_completeness"),
        },
        "requirements": reqs,
        "quantity_extract": {
            "quantity": qty.get("quantity"),
            "quantity_unit": qty.get("quantity_unit"),
            "quantity_semantic": qty.get("quantity_semantic"),
            "quantity_kind": qty.get("quantity_kind"),
        },
        "brand_restriction_status": brand_status,
        "validations": validations,
        "unknowns": _collect_unknowns(reqs, submission, deadline_raw),
    }


def _collect_unknowns(reqs: dict, submission: Any, deadline_raw: str | None) -> list[str]:
    unk = []
    lines = reqs.get("line_items") or []
    if not lines:
        unk.append("no_line_items_extracted")
    for i, li in enumerate(lines):
        if li.get("quantity") is None:
            unk.append(f"line_{i}_quantity_UNKNOWN")
        if not li.get("unit_of_measure"):
            unk.append(f"line_{i}_uom_UNKNOWN")
        if not (li.get("model") or li.get("mpn") or li.get("nsn") or li.get("manufacturer")):
            unk.append(f"line_{i}_identity_partial_or_UNKNOWN")
    sreq = reqs.get("solicitation_requirements") or {}
    if not sreq.get("delivery"):
        unk.append("delivery_UNKNOWN")
    if not sreq.get("specifications"):
        unk.append("specifications_UNKNOWN")
    if not submission:
        unk.append("submission_UNKNOWN")
    if not deadline_raw:
        unk.append("deadline_UNKNOWN")
    unk.append("live_open_status_UNVERIFIED")
    return unk


def build_owner_pipeline_card(nb_id: str, extraction: dict[str, Any], *, source_retained: bool) -> dict[str, Any]:
    """Owner-facing status fields for existing workflow (no replacement dashboard)."""
    presence = (extraction.get("classification") or {}).get("product_presence")
    reqs = extraction.get("requirements") or {}
    rs = reqs.get("requirements_state")
    lines = reqs.get("line_items") or []
    has_qty = any(li.get("quantity") is not None for li in lines)
    has_uom = any(li.get("unit_of_measure") for li in lines)
    quote = "NO"
    if presence in {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT"} and has_qty and (has_uom or len(lines) >= 1) and source_retained:
        text_enough = True
        if rs == "COMPLETE" and has_uom:
            quote = "YES"
        else:
            quote = "PARTIAL"
    elif presence in {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT"}:
        quote = "PARTIAL"
    product_label = (
        "PRODUCT CONFIRMED"
        if presence in {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT"} and source_retained
        else ("POTENTIAL PRODUCT" if presence in {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT", "POSSIBLE_PRODUCT"} else "NOT PRODUCT / UNKNOWN")
    )
    req_label = {
        "COMPLETE": "REQUIREMENTS COMPLETE",
        "PARTIAL": "REQUIREMENTS PARTIAL",
        "NOT_FOUND": "REQUIREMENTS MISSING",
        "NO_TEXT": "REQUIREMENTS MISSING",
    }.get(str(rs), "REQUIREMENTS MISSING")
    plan = plan_cheapest_retrieval(nb_id)
    proposed = plan.get("proposed_metered_call")
    return {
        "nb_id": nb_id,
        "product_status": product_label,
        "requirements_status": req_label,
        "source_status": "SOURCE RETAINED" if source_retained else "SOURCE NOT RETAINED",
        "supplier_quote": f"SUPPLIER QUOTE {quote}",
        "bid_status": "BID STATUS UNVERIFIED",
        "exact_missing_information": extraction.get("unknowns") or [],
        "minimum_next_retrieval_action": proposed,
        "known_retrieval_credit_cost": (proposed or {}).get("max_credit_cost"),
        "line_item_count": len(lines),
        "note": "Supplier-quote readiness is independent of live open-status verification.",
    }


def process_offline_opportunity(nb_id: str) -> dict[str, Any]:
    """Run extract+validate+owner card on currently retained evidence only (zero API)."""
    inv = load_inventory().get(nb_id) or {}
    pkg = load_recovery_package(nb_id) or {}
    ft = load_full_text(nb_id) or {}
    desc = str(pkg.get("description") or "")
    doc = str(ft.get("full_text") or pkg.get("document_text") or "")
    # Prefer thicker
    if len(str(pkg.get("document_text") or "")) > len(doc):
        doc = str(pkg.get("document_text") or "")
    source_retained = len(doc) >= 400 or len(desc) >= 200
    extraction = extract_from_retained_text(
        title=str(pkg.get("title") or inv.get("title") or ""),
        description=desc or None,
        document_text=doc if len(doc) > len(desc) else (doc or None),
        agency=pkg.get("agency") or inv.get("agency"),
        solicitation_number=pkg.get("solicitation_number") or inv.get("solicitation_number"),
        official_url=pkg.get("source_url") or inv.get("official_url"),
        deadline_raw=inv.get("response_deadline") or inv.get("deadline_at") or pkg.get("response_deadline"),
    )
    card = build_owner_pipeline_card(nb_id, extraction, source_retained=source_retained)
    plan = plan_cheapest_retrieval(nb_id)
    return {
        "nb_id": nb_id,
        "mode": "offline_retained_only",
        "api_calls": 0,
        "source_retained": source_retained,
        "description_chars": len(desc),
        "document_text_chars": len(doc),
        "extraction": extraction,
        "owner_card": card,
        "retrieval_plan": plan,
        "processed_at": _utc(),
    }


def ingest_simulated_or_authorized_read_file(
    *,
    nb_id: str,
    path: str,
    raw_text: str,
    job_id: str = "pipeline-offline-fixture",
    status_code: int = 200,
) -> dict[str, Any]:
    """Persist a read_file-shaped body (fixture or authorized live), reload, extract.

    This is the durable path used by preflight fixtures and by an owner-authorized live call
    after the response text is in hand. Does not contact NextBid.
    """
    body = {
        "ok": False,
        "error": "non_json",
        "status_code": status_code,
        "raw_text": raw_text,
        "content_type": "text/markdown",
    }
    original = raw_text.encode("utf-8", errors="replace")
    meta = persist_paid_response(
        method="GET",
        path=f"/opportunities/{nb_id}/files/{path}",
        params=None,
        endpoint="read_file",
        nb_id=nb_id,
        status_code=status_code,
        body=body,
        job_id=job_id,
        raw_text=raw_text,
        content_type="text/markdown",
        original_bytes=original,
    )
    # Simulate application restart: clear nothing; reload from disk
    verify = verify_persisted_response(meta["request_id"], expected_content_hash=meta["content_hash"])
    reloaded = load_by_request_id(meta["request_id"]) or {}
    text = nextbid_file_text(reloaded.get("body"))
    if len(text) != len(raw_text):
        verify["ok"] = False
        verify.setdefault("errors", []).append("reload_text_length_mismatch")

    # Thicken full_text store (never thinner overwrite)
    store_full_text(nb_id, text, source="nextbid_pipeline_read_file", title=None)
    inv = load_inventory().get(nb_id) or {}
    pkg = load_recovery_package(nb_id) or {}
    extraction = extract_from_retained_text(
        title=str(pkg.get("title") or inv.get("title") or nb_id),
        description=pkg.get("description"),
        document_text=text,
        agency=pkg.get("agency") or inv.get("agency"),
        solicitation_number=pkg.get("solicitation_number") or inv.get("solicitation_number"),
        official_url=pkg.get("source_url") or inv.get("official_url"),
        deadline_raw=inv.get("response_deadline") or inv.get("deadline_at"),
    )
    card = build_owner_pipeline_card(nb_id, extraction, source_retained=True)
    return {
        "persistence": meta,
        "verify_reload": verify,
        "reloaded_raw_text_chars": len(text),
        "original_bytes_sha256": hashlib.sha256(original).hexdigest(),
        "extraction": extraction,
        "owner_card": card,
        "api_calls": 0,
    }


def build_one_call_approval_package() -> dict[str, Any]:
    """Package for owner approval — does not execute any metered call."""
    assert acquisition_locked(), "killswitch must remain locked"
    selected = select_test_opportunity()
    offline = process_offline_opportunity(PIPELINE_TEST_NB_ID)
    plan = offline["retrieval_plan"]
    st = controller_status()
    return {
        "title": "ONE-CALL APPROVAL PACKAGE — DO NOT EXECUTE WITHOUT OWNER AUTHORIZATION",
        "killswitch_locked": True,
        "opportunity_id": PIPELINE_TEST_NB_ID,
        "endpoint": "read_file",
        "artifact_path": PIPELINE_TEST_ARTIFACT,
        "http_path": f"/opportunities/{PIPELINE_TEST_NB_ID}/files/{PIPELINE_TEST_ARTIFACT}",
        "existing_source_evidence": {
            "listing_description_chars": selected["listing_description_chars"],
            "already_paid_endpoints": selected["already_paid_endpoints"],
            "source_documents_metadata": selected["source_documents_metadata_count"],
            "document0_md_retrieved": False,
        },
        "expected_missing_information": selected["missing_fields"],
        "max_credit_cost": 1,
        "ledger": st.get("budget_state"),
        "limits": st.get("limits"),
        "spendable_before_reserve": max(
            0,
            int((st.get("budget_state") or {}).get("account_remaining") or 0)
            - int((st.get("limits") or {}).get("monthly_reserve") or 1000),
        ),
        "preflight": {
            "persistence_tests": "see tests/test_nextbid_pipeline_persistence.py",
            "uom_brand_regression": "tests/test_uom_not_brand.py",
            "offline_pipeline_ran": True,
            "offline_owner_card": offline.get("owner_card"),
        },
        "proposed_call": plan.get("proposed_metered_call"),
        "execution_gate": "OWNER_EXPLICIT_AUTHORIZATION_REQUIRED",
        "prepared_at": _utc(),
    }


def reconcile_quote_ready_counts() -> dict[str, Any]:
    """Explain 2 (14-audit) vs 10 (population accounting) without changing classifications."""
    return {
        "fourteen_audit_YES": 2,
        "fourteen_audit_commit": "c565006",
        "fourteen_audit_scope": (
            "Strict manual verification of owner/pricing_now.jsonl only (14 cards). "
            "YES required clear product identity + grounded qty in retained full_text/requirements "
            "with supplier-outreach sufficiency (Weg motor model+qty; rock salt 62-line TON schedule)."
        ),
        "population_accounting_YES": 10,
        "population_accounting_commit": "76315c7",
        "population_accounting_scope": (
            "Automated thresholds across all 8,088 NextBid inventory rows: product/mixed + "
            "has_qty + (has_spec or >=5 complete lines) + text_chars>=200. Broader than the "
            "14-card manual bar; not independently source-audited line-by-line."
        ),
        "reconciliation": (
            "Not a contradiction: different universes and bars. The 2 YES are the "
            "owner-trustworthy quote-ready set from the pricing_now audit. The 10 YES are "
            "automated candidates from the full inventory pass and still require per-opportunity "
            "evidence review before outreach."
        ),
        "do_not_inflate": True,
    }

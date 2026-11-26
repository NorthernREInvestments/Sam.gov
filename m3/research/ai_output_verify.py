"""Deterministic verification of AI solicitation analysis against source passages.

AI output is never automatically trusted. Unsupported assertions are rejected.
Unresolved values become UNKNOWN. Significant install/construction/service blocks
product-only qualification.
"""

from __future__ import annotations

import re
from typing import Any

from m3.research.commercial_criteria import (
    INITIAL_EXCLUDE_CLASSES,
    evaluate_deadline_gate,
    evaluate_bid_eligibility,
    evaluate_procurement_pipeline,
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _snippet_supported(snippet: str, corpus: str, *, min_len: int = 12) -> bool:
    sn = _norm(snippet)
    if len(sn) < min_len:
        return False
    corp = _norm(corpus)
    if sn in corp:
        return True
    # Allow slight whitespace variance on a shorter core
    core = sn[:80]
    return len(core) >= min_len and core in corp


def verify_analysis_against_sources(
    analysis: dict[str, Any] | None,
    evidence_package: dict[str, Any],
    *,
    item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify model (or mock) structured output against assembled local sources."""
    corpus = str(evidence_package.get("assembled_text") or "")
    coverage = evidence_package.get("coverage") or {}
    analysis = analysis or {}
    rejected_assertions: list[str] = []
    verified_line_items: list[dict[str, Any]] = []
    unknown_fields: list[str] = []

    if not coverage.get("has_substantive_document_content"):
        return {
            "ok": False,
            "verified": False,
            "reason": "insufficient_substantive_source_content",
            "rejected_assertions": ["cannot_verify_without_substantive_local_documents"],
            "verified_line_items": [],
            "unknown_fields": ["all"],
            "blocks_product_only": False,
            "promotion": {
                "supplier_quote_ready": False,
                "currently_open_verified": False,
                "bid_submission_ready": False,
            },
            "complete_solicitation_reviewed": False,
        }

    req = str(analysis.get("requirement_class") or "unknown")
    blocks = False
    for hob in analysis.get("hidden_obligations") or []:
        if not isinstance(hob, dict):
            continue
        snip = str(hob.get("source_snippet") or "")
        if snip and not _snippet_supported(snip, corpus):
            rejected_assertions.append(f"unsupported_hidden_obligation:{hob.get('kind')}")
            continue
        if hob.get("blocks_product_only") or str(hob.get("kind") or "").lower() in {
            "installation",
            "construction",
            "mandatory_meeting",
        }:
            if re.search(r"(?i)\b(install|construction|site\s+work|mandatory\s+pre[\s-]?bid)\b", snip or str(hob.get("detail") or "")):
                blocks = True

    if req in INITIAL_EXCLUDE_CLASSES or req in {
        "services",
        "construction",
        "maintenance_repair_services",
        "professional_services",
        "product_with_significant_installation",
    }:
        blocks = True

    for li in analysis.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        desc = str(li.get("description") or "")
        snip = str(li.get("source_snippet") or desc)
        qty = li.get("quantity")
        if not desc:
            rejected_assertions.append("line_item_missing_description")
            continue
        if snip and not _snippet_supported(snip, corpus) and not _snippet_supported(desc, corpus):
            rejected_assertions.append(f"unsupported_line_item:{desc[:60]}")
            continue
        if qty in (None, "", "UNKNOWN", "unknown"):
            unknown_fields.append(f"quantity:{desc[:40]}")
            li = dict(li)
            li["quantity"] = None
            li["quantity_semantic"] = li.get("quantity_semantic") or "UNKNOWN"
        # Prevent duplicate boom: same desc+qty once
        key = _norm(f"{desc}|{qty}|{li.get('unit')}")
        if any(_norm(f"{x.get('description')}|{x.get('quantity')}|{x.get('unit')}") == key for x in verified_line_items):
            rejected_assertions.append(f"duplicate_line_item_removed:{desc[:60]}")
            continue
        verified_line_items.append(li)

    # Deadlines must be source-supported when claimed
    deadlines = []
    for dl in analysis.get("deadlines") or []:
        if not isinstance(dl, dict):
            continue
        snip = str(dl.get("source_snippet") or dl.get("value") or "")
        if snip and not _snippet_supported(snip, corpus) and not _snippet_supported(str(dl.get("value") or ""), corpus):
            rejected_assertions.append(f"unsupported_deadline:{dl.get('value')}")
            continue
        deadlines.append(dl)

    work = dict(item or {})
    work["nb_id"] = evidence_package.get("nb_id") or work.get("nb_id")
    work["title"] = evidence_package.get("title") or work.get("title")
    inv = evidence_package.get("inventory") or {}
    for k, v in inv.items():
        if v and not work.get(k):
            work[k] = v
    work["document_text"] = corpus
    work["verified_individual_solicitation"] = True
    work["record_type"] = work.get("record_type") or "INDIVIDUAL_SOLICITATION"
    if req and req != "unknown":
        work["requirement_class"] = req
    if verified_line_items:
        work["line_items"] = verified_line_items
        work["verified_line_items"] = verified_line_items
        work["quantity_semantic"] = "MULTI_LINE_ITEM" if len(verified_line_items) > 1 else work.get("quantity_semantic")
        if len(verified_line_items) == 1 and verified_line_items[0].get("quantity"):
            work["quantity"] = verified_line_items[0].get("quantity")
            work["quantity_unit"] = verified_line_items[0].get("unit")
            work["quantity_semantic"] = verified_line_items[0].get("quantity_semantic") or "ORDERED_QUANTITY"
    if analysis.get("submission_requirements"):
        work["submission_instructions"] = "; ".join(str(x) for x in analysis["submission_requirements"][:4])

    # Prefer inventory/authoritative deadline for gate (AI deadline is supplemental)
    pipe = evaluate_procurement_pipeline(work, document_text=corpus)
    if blocks:
        pipe = dict(pipe)
        pipe["excluded"] = True
        pipe["reasons"] = list(pipe.get("reasons") or []) + ["verified_service_or_installation_blocker"]
        pipe["product_opportunity"] = False
        pipe["stage"] = None
    bid = evaluate_bid_eligibility(work, pipe, document_text=corpus)
    dl = evaluate_deadline_gate(work)

    if not verified_line_items and analysis.get("is_product_purchase"):
        unknown_fields.append("line_items")

    # --- Authoritative readiness (AI cannot bypass) ---
    # Unknown/missing quantities on ANY verified line block FULL supplier-quote readiness.
    lines_missing_qty = [
        li
        for li in verified_line_items
        if li.get("quantity") in (None, "", "UNKNOWN", "unknown")
        or str(li.get("quantity_semantic") or "").upper() == "UNKNOWN"
    ]
    qty_complete = bool(verified_line_items) and not lines_missing_qty
    if lines_missing_qty:
        for li in lines_missing_qty:
            uf = f"quantity:{(li.get('description') or '')[:40]}"
            if uf not in unknown_fields:
                unknown_fields.append(uf)

    pkg_label = str(coverage.get("package_completeness") or "")
    docs_pending = int((coverage.get("document_inventory") or {}).get("download_pending") or 0)
    reported_docs = coverage.get("opportunity_document_count")
    package_blocks_full_quote = bool(
        docs_pending > 0
        or pkg_label in {"partial_document_package", "listing_only", "package_completeness_unverified"}
        or (reported_docs is not None and int(reported_docs) > 0 and docs_pending > 0)
    )

    readiness_blockers: list[str] = []
    if blocks:
        readiness_blockers.append("unsuitable_execution_obligations")
    if dl.get("expired"):
        readiness_blockers.append("deadline_expired")
    if not verified_line_items:
        readiness_blockers.append("no_verified_line_items")
    if not qty_complete:
        readiness_blockers.append("unknown_mandatory_line_quantities")
    if not coverage.get("has_substantive_document_content"):
        readiness_blockers.append("insufficient_substantive_source_content")
    if package_blocks_full_quote and pkg_label != "complete_relevant_package" and pkg_label != "complete_package_reviewed":
        # Body-only packages may still quote if quantities complete and no pending attachments
        if docs_pending > 0 or (reported_docs is not None and int(reported_docs) > 0 and not coverage.get("document_inventory", {}).get("downloaded")):
            readiness_blockers.append("required_attachments_not_downloaded")
    if not bid.get("supplier_quote_ready"):
        readiness_blockers.append("bid_eligibility_gate_failed")

    # Partial: some lines quoteable, but full solicitation not ready
    partial_supplier_quote = bool(
        verified_line_items
        and not blocks
        and not dl.get("expired")
        and coverage.get("has_substantive_document_content")
        and lines_missing_qty
        and any(li.get("quantity") not in (None, "", "UNKNOWN", "unknown") for li in verified_line_items)
    )

    supplier_quote_ready = bool(not readiness_blockers)
    currently_open_verified = bool(bid.get("currently_open_verified"))
    # Bid submission ready requires supplier-quote ready PLUS submission gates — never AI alone
    bid_submission_ready = bool(
        supplier_quote_ready
        and currently_open_verified
        and bid.get("ready_to_submit")
        and not unknown_fields
    )
    if supplier_quote_ready and not currently_open_verified:
        readiness_blockers.append("open_status_unverified")

    complete_reviewed = bool(
        coverage.get("complete_solicitation_reviewed_claim_allowed")
        and pkg_label in {"complete_package_reviewed", "complete_relevant_package"}
        and docs_pending == 0
        and coverage.get("has_substantive_document_content")
    )

    return {
        "ok": True,
        "verified": bool(verified_line_items) and not blocks and not dl.get("expired"),
        "rejected_assertions": rejected_assertions,
        "verified_line_items": verified_line_items,
        "unknown_fields": unknown_fields or (["UNKNOWN"] if not verified_line_items else []),
        "blocks_product_only": blocks,
        "deadlines_verified": deadlines,
        "deadline_gate": dl,
        "pipeline": pipe,
        "bid": bid,
        "promotion": {
            "supplier_quote_ready": supplier_quote_ready,
            "partial_supplier_quote": partial_supplier_quote and not supplier_quote_ready,
            "currently_open_verified": currently_open_verified,
            "bid_submission_ready": bid_submission_ready,
            "readiness_blockers": readiness_blockers,
            "ai_analysis_alone_insufficient": True,
            "ai_cannot_bypass_gates": True,
        },
        "complete_solicitation_reviewed": complete_reviewed,
        "item": work,
    }

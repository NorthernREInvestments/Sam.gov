"""Submission, supplier, post-award, and invoice checklists."""

from __future__ import annotations

from typing import Any

from execution_requirements.constants import (
    PAY_ACCEPTED,
    PAY_DELIVERED,
    PAY_INVOICED,
    PAY_PAID,
    PAY_SHIPPED,
    ST_CONFIRMED,
    ST_NOT_APPLICABLE,
    ST_UNKNOWN,
)


def _items_for(requirements: list[dict[str, Any]], categories: set[str]) -> list[dict[str, Any]]:
    out = []
    for r in requirements:
        if r.get("category") in categories:
            out.append(
                {
                    "requirement_id": r.get("requirement_id"),
                    "label": r.get("normalized_requirement"),
                    "status": r.get("status"),
                    "mandatory": bool(r.get("mandatory")),
                    "blocking": bool(r.get("blocking")),
                    "action": r.get("plain_english_action"),
                    "actor": r.get("assigned_actor"),
                    "evidence": r.get("evidence"),
                }
            )
    return out


def build_submission_checklist(requirements: list[dict[str, Any]]) -> dict[str, Any]:
    items = _items_for(requirements, {"SUBMISSION", "CERTIFICATION"})
    unresolved = [i for i in items if i["mandatory"] and i["status"] not in {ST_CONFIRMED, ST_NOT_APPLICABLE}]
    return {
        "kind": "SubmissionChecklist",
        "items": items,
        "unresolved_mandatory": unresolved,
        "complete": len(unresolved) == 0 and len(items) > 0,
        "blocks_owner_approval": len(unresolved) > 0,
    }


def build_supplier_confirmation_checklist(requirements: list[dict[str, Any]]) -> dict[str, Any]:
    items = _items_for(requirements, {"SUPPLIER_CONFIRMATION", "PACKAGING", "FOB", "COUNTRY_OF_ORIGIN", "WARRANTY"})
    # Prefer explicit SUPPLIER_CONFIRMATION rows
    supplier_rows = [i for i in items if True]
    unresolved = [
        i
        for i in supplier_rows
        if i["mandatory"] and i["status"] not in {ST_CONFIRMED, ST_NOT_APPLICABLE}
    ]
    return {
        "kind": "SupplierConfirmationChecklist",
        "items": supplier_rows,
        "unresolved_mandatory": unresolved,
        "complete": len(unresolved) == 0 and any(
            r.get("category") == "SUPPLIER_CONFIRMATION" for r in requirements
        ),
        "blocks_owner_approval": len(unresolved) > 0,
    }


def build_supplier_quote_packet(requirements: list[dict[str, Any]], *, deal_name: str = "Opportunity") -> dict[str, Any]:
    """Prepare supplier request content only — does NOT send email."""
    lines = [
        f"Supplier quote / confirmation request — {deal_name}",
        "",
        "Please confirm the following:",
    ]
    for r in requirements:
        if r.get("category") not in {"SUPPLIER_CONFIRMATION", "PRODUCT", "QUANTITY_UOM", "PACKAGING", "FOB", "DELIVERY", "COUNTRY_OF_ORIGIN", "WARRANTY"}:
            continue
        if r.get("status") in {ST_CONFIRMED, ST_NOT_APPLICABLE}:
            continue
        action = r.get("plain_english_action") or r.get("normalized_requirement")
        lines.append(f"- {action}")
    lines.extend(
        [
            "",
            "Please include: unit price, freight, lead time, quote validity, and any packaging costs.",
            "",
            "(Prepared by M3 — not transmitted. Operator must send manually if authorized.)",
        ]
    )
    return {
        "kind": "SupplierQuotePacket",
        "transmission": "CONTENT_ONLY_NO_SEND",
        "subject": f"Quote / confirmation request — {deal_name}",
        "body": "\n".join(lines),
        "item_count": max(0, len(lines) - 5),
    }


POST_AWARD_STEPS = (
    ("award_reviewed", "Award reviewed"),
    ("supplier_po_issued", "Supplier PO issued"),
    ("supplier_confirms_order", "Supplier confirms order"),
    ("packaging_confirmed", "Packaging confirmed"),
    ("shipment_created", "Shipment created"),
    ("tracking_captured", "Tracking captured"),
    ("delivery_completed", "Delivery completed"),
    ("acceptance_confirmed", "Acceptance confirmed"),
    ("invoice_submitted", "Invoice submitted"),
    ("payment_received", "Payment received"),
)


def build_post_award_checklist(
    requirements: list[dict[str, Any]],
    *,
    lifecycle: str | None = None,
    payment_state: str | None = None,
) -> dict[str, Any]:
    """Ordered post-award tasks; carry forward solicitation requirements."""
    lc = str(lifecycle or "").upper()
    pay = str(payment_state or "").upper()
    # Carry packaging/inspection/acceptance/invoice forward
    carry = [
        r
        for r in requirements
        if r.get("category") in {"PACKAGING", "MARKING", "DELIVERY", "FOB", "INSPECTION", "ACCEPTANCE", "INVOICE", "PAYMENT"}
        and r.get("mandatory")
    ]
    steps = []
    completed_hint = {
        "award_reviewed": "AWARD" in lc or lc in {"AWARDED", "ORDERING", "DELIVERED", "PAID"},
        "delivery_completed": pay == PAY_DELIVERED or "DELIVER" in lc,
        "acceptance_confirmed": pay == PAY_ACCEPTED,
        "invoice_submitted": pay == PAY_INVOICED,
        "payment_received": pay == PAY_PAID or "PAID" in lc,
        "shipment_created": pay == PAY_SHIPPED,
    }
    for key, label in POST_AWARD_STEPS:
        related = [c for c in carry if _step_matches(key, c)]
        done = bool(completed_hint.get(key))
        steps.append(
            {
                "step": key,
                "label": label,
                "status": "COMPLETE" if done else "PENDING",
                "carried_requirements": [
                    {"requirement_id": r.get("requirement_id"), "label": r.get("normalized_requirement"), "action": r.get("plain_english_action")}
                    for r in related
                ],
            }
        )
    return {
        "kind": "PostAwardChecklist",
        "steps": steps,
        "payment_states_distinct": list(
            (PAY_SHIPPED, PAY_DELIVERED, PAY_ACCEPTED, PAY_INVOICED, PAY_PAID)
        ),
        "note": "SHIPPED ≠ DELIVERED ≠ ACCEPTED ≠ INVOICED ≠ PAID — do not collapse.",
        "carried_requirement_count": len(carry),
    }


def _step_matches(step: str, req: dict[str, Any]) -> bool:
    cat = req.get("category")
    if step == "packaging_confirmed":
        return cat in {"PACKAGING", "MARKING"}
    if step in {"shipment_created", "tracking_captured", "delivery_completed"}:
        return cat in {"DELIVERY", "SHIPPING", "FOB"}
    if step == "acceptance_confirmed":
        return cat in {"ACCEPTANCE", "INSPECTION"}
    if step == "invoice_submitted":
        return cat in {"INVOICE"}
    if step == "payment_received":
        return cat in {"PAYMENT"}
    return False


def build_invoice_checklist(requirements: list[dict[str, Any]]) -> dict[str, Any]:
    items = _items_for(requirements, {"INVOICE", "PAYMENT", "ACCEPTANCE"})
    wawf = any(
        r.get("subtype") == "WAWF" or "WAWF" in str(r.get("normalized_requirement") or "").upper()
        for r in requirements
        if r.get("category") == "INVOICE"
    )
    unresolved = [i for i in items if i["mandatory"] and i["status"] not in {ST_CONFIRMED, ST_NOT_APPLICABLE, ST_UNKNOWN}]
    # UNKNOWN mandatory invoice system still blocks understanding
    unknown_mandatory = [
        i for i in items if i["mandatory"] and i["status"] == ST_UNKNOWN
    ]
    return {
        "kind": "InvoiceChecklist",
        "items": items,
        "wawf_required": wawf,
        "unresolved_mandatory": unresolved + unknown_mandatory,
        "states": {
            "SHIPPED": PAY_SHIPPED,
            "DELIVERED": PAY_DELIVERED,
            "ACCEPTED": PAY_ACCEPTED,
            "INVOICED": PAY_INVOICED,
            "PAID": PAY_PAID,
        },
        "note": "Do not invoice before acceptance when acceptance is required. UNKNOWN invoice system stays UNKNOWN.",
    }

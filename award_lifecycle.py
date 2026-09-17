"""Lightweight award lifecycle persistence + post-award next actions."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

STATUS_SUBMITTED = "SUBMITTED"
STATUS_AWARDED = "AWARDED"
STATUS_PERFORMING = "PERFORMING"
STATUS_INVOICED = "INVOICED"
STATUS_PAID = "PAID"
STATUS_LOST = "LOST"
STATUS_CLOSED = "CLOSED"

POST_AWARD_ACTIONS = (
    "CHECK AWARD",
    "REVIEW AWARD",
    "SEND AWARD/PO TO FINANCIER",
    "OBTAIN FINAL FUNDING APPROVAL",
    "PLACE SUPPLIER ORDER",
    "CONFIRM SUPPLIER PAYMENT",
    "CONFIRM SHIPMENT",
    "TRACK DELIVERY",
    "CONFIRM GOVERNMENT ACCEPTANCE",
    "SEND INVOICE",
    "CHECK GOVERNMENT PAYMENT",
    "PAY OFF FINANCIER",
    "CLOSE DEAL",
)


def get_or_create_award_lifecycle(session: Any, contract_id: int) -> Any:
    from models import AwardLifecycle

    row = session.query(AwardLifecycle).filter_by(contract_id=contract_id).first()
    if row is None:
        row = AwardLifecycle(contract_id=contract_id, lifecycle_status=STATUS_SUBMITTED)
        session.add(row)
        session.flush()
    return row


def award_lifecycle_to_dict(row: Any) -> dict[str, Any]:
    return {
        "contract_id": row.contract_id,
        "lifecycle_status": row.lifecycle_status,
        "submission_timestamp": row.submission_timestamp.isoformat() if row.submission_timestamp else None,
        "submission_method": row.submission_method,
        "submission_reference": row.submission_reference,
        "award_decision": row.award_decision,
        "award_amount": float(row.award_amount) if row.award_amount is not None else None,
        "award_po_reference": row.award_po_reference,
        "funding_final_approval": row.funding_final_approval,
        "supplier_order_status": row.supplier_order_status,
        "shipment_tracking": row.shipment_tracking,
        "delivery_status": row.delivery_status,
        "government_acceptance": row.government_acceptance,
        "invoice_number": row.invoice_number,
        "invoice_date": row.invoice_date.isoformat() if row.invoice_date else None,
        "invoice_amount": float(row.invoice_amount) if row.invoice_amount is not None else None,
        "government_payment_status": row.government_payment_status,
        "financier_payoff_status": row.financier_payoff_status,
        "company_proceeds_status": row.company_proceeds_status,
        "close_date": row.close_date.isoformat() if row.close_date else None,
        "LIVE_API_REQUESTS": 0,
    }


def update_award_milestone(session: Any, contract_id: int, **fields: Any) -> dict[str, Any]:
    row = get_or_create_award_lifecycle(session, contract_id)
    for k, v in fields.items():
        if hasattr(row, k):
            setattr(row, k, v)
    row.updated_at = now_utc()
    session.flush()
    return award_lifecycle_to_dict(row)


def post_award_next_action(lifecycle: dict[str, Any] | None) -> dict[str, Any]:
    """One obvious next action based on award lifecycle state."""
    lc = (lifecycle or {}).get("lifecycle_status") or STATUS_SUBMITTED
    chain = [
        (STATUS_SUBMITTED, "CHECK AWARD", "Confirm award decision posted."),
        (STATUS_AWARDED, "SEND AWARD/PO TO FINANCIER", "Provider needs PO to finalize funding."),
        (STATUS_AWARDED, "OBTAIN FINAL FUNDING APPROVAL", "Award confirmation separate from pre-bid viability."),
        (STATUS_PERFORMING, "PLACE SUPPLIER ORDER", "Execute supplier purchase after funding approval."),
        (STATUS_PERFORMING, "CONFIRM SUPPLIER PAYMENT", "Verify supplier paid per terms."),
        (STATUS_PERFORMING, "CONFIRM SHIPMENT", "Track fulfillment."),
        (STATUS_PERFORMING, "TRACK DELIVERY", "Monitor delivery to government."),
        (STATUS_PERFORMING, "CONFIRM GOVERNMENT ACCEPTANCE", "Acceptance may be required before invoice."),
        (STATUS_INVOICED, "CHECK GOVERNMENT PAYMENT", "Monitor payment status."),
        (STATUS_INVOICED, "PAY OFF FINANCIER", "Repay financier when government pays."),
        (STATUS_PAID, "CLOSE DEAL", "Record proceeds and close."),
    ]
    if lc == STATUS_LOST:
        return {"action": "ARCHIVE LOST DEAL", "why": "Award lost.", "kind": "AWARD"}
    if lc == STATUS_CLOSED:
        return {"action": "No post-award actions — deal closed.", "why": "Closed.", "kind": "AWARD", "blocking": False}

    if lc == STATUS_AWARDED and not lifecycle.get("funding_final_approval"):
        return {"action": "OBTAIN FINAL FUNDING APPROVAL", "why": "Award received — confirm funding.", "kind": "AWARD", "priority": 15}
    if lc == STATUS_PERFORMING and not lifecycle.get("supplier_order_status"):
        return {"action": "PLACE SUPPLIER ORDER", "why": "Perform contract — order product.", "kind": "AWARD", "priority": 20}
    if lc == STATUS_PERFORMING and lifecycle.get("supplier_order_status") and not lifecycle.get("shipment_tracking"):
        return {"action": "CONFIRM SHIPMENT", "why": "Order placed — confirm shipment.", "kind": "AWARD", "priority": 25}
    if lc == STATUS_INVOICED and lifecycle.get("government_payment_status") != "PAID":
        return {"action": "CHECK GOVERNMENT PAYMENT", "why": "Invoice sent — await payment.", "kind": "AWARD", "priority": 30}
    if lc == STATUS_PAID and lifecycle.get("financier_payoff_status") != "PAID":
        return {"action": "PAY OFF FINANCIER", "why": "Government paid — close financing.", "kind": "AWARD", "priority": 35}

    for status, action, why in chain:
        if status == lc:
            return {"action": action, "why": why, "kind": "AWARD", "priority": 40}
    return {"action": "REVIEW AWARD", "why": f"Lifecycle status: {lc}", "kind": "AWARD", "priority": 50}

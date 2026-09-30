"""Gap classification helpers."""

from __future__ import annotations

from validation_harness.models import DOMAIN_GAP_MAP, GAP_CATEGORIES


def classify_gap(domain: str, field_path: str, *, false_readiness: bool = False, false_rejection: bool = False) -> str:
    if false_readiness:
        return "FALSE_READINESS"
    if false_rejection:
        return "FALSE_REJECTION"
    mapped = DOMAIN_GAP_MAP.get(domain)
    if mapped in GAP_CATEGORIES:
        return mapped
    path = (field_path or "").lower()
    if "provenance" in path or "evidence" in path:
        return "PROVENANCE_ERROR"
    if "amendment" in path:
        return "AMENDMENT_ERROR"
    if "wawf" in path or "invoice" in path:
        return "INVOICE_ERROR"
    if "payment" in path:
        return "PAYMENT_ERROR"
    if "packag" in path or "mil" in path:
        return "PACKAGING_ERROR"
    if "fob" in path or "deliver" in path:
        return "DELIVERY_ERROR"
    if "inspect" in path:
        return "INSPECTION_ERROR"
    if "accept" in path:
        return "ACCEPTANCE_ERROR"
    if "quantity" in path or "uom" in path or "clin" in path:
        return "QUANTITY_UOM_ERROR"
    if "part" in path or "nsn" in path or "manufacturer" in path:
        return "IDENTITY_ERROR"
    if "supplier" in path:
        return "SUPPLIER_ERROR"
    if "financ" in path or "cash" in path or "guarantee" in path:
        return "FINANCING_ERROR"
    if "submit" in path or "deadline" in path:
        return "SUBMISSION_ERROR"
    if "ready" in path or "workflow" in path or "operator" in path:
        return "WORKFLOW_ERROR"
    return "OTHER"


def likely_module_for(category: str) -> str:
    return {
        "IDENTITY_ERROR": "execution_requirements.extract / m3_procurement_package",
        "QUANTITY_UOM_ERROR": "micro_purchase_lab_integrity / execution_requirements.extract",
        "PACKAGING_ERROR": "m3_dla_clause_extraction / execution_requirements",
        "DELIVERY_ERROR": "bid_submission_intelligence / execution_requirements",
        "INSPECTION_ERROR": "m3_dla_clause_extraction",
        "ACCEPTANCE_ERROR": "m3_dla_clause_extraction",
        "AMENDMENT_ERROR": "execution_requirements.conflicts",
        "SUBMISSION_ERROR": "bid_submission_intelligence / execution_requirements",
        "INVOICE_ERROR": "execution_requirements.checklists",
        "FINANCING_ERROR": "funding / cash_cycle / operator_workflow",
        "SUPPLIER_ERROR": "operator_workflow.resolver / commercial_verification",
        "FALSE_READINESS": "execution_requirements.readiness / operator_workflow",
        "FALSE_REJECTION": "operator_workflow.resolver / micro_purchase_lab_integrity",
        "WORKFLOW_ERROR": "operator_workflow.resolver",
        "PROVENANCE_ERROR": "execution_requirements.models",
        "ECONOMICS_ERROR": "micro_purchase_lab_integrity / deal economics",
    }.get(category, "UNKNOWN")

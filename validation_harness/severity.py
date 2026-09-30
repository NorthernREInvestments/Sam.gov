"""Severity assignment for validation gaps."""

from __future__ import annotations

from typing import Any

from validation_harness.models import (
    SEV_CRITICAL,
    SEV_HIGH,
    SEV_INFO,
    SEV_LOW,
    SEV_MEDIUM,
)

# Field path fragments → severity
_CRITICAL_PATHS = (
    "part_number",
    "nsn",
    "quantity",
    "uom",
    "approved_source",
    "deadline",
    "owner_readiness",
    "ready_for_owner_approval",
    "first_article",
    "source_inspection",
    "estimated_as_guaranteed",
    "false_readiness",
)

_HIGH_PATHS = (
    "fob",
    "packaging",
    "mil_std",
    "wawf",
    "warranty",
    "acceptance_point",
    "supplier_confirmation",
    "submission_method",
    "amendment",
)


def severity_for(field_path: str, category: str, *, false_readiness: bool = False) -> str:
    path = (field_path or "").lower()
    cat = (category or "").upper()
    if false_readiness or cat == "FALSE_READINESS":
        return SEV_CRITICAL
    if cat in {"FALSE_REJECTION"}:
        return SEV_HIGH
    if cat in {"IDENTITY_ERROR", "QUANTITY_UOM_ERROR", "AMENDMENT_ERROR", "WORKFLOW_ERROR"}:
        if any(k in path for k in _CRITICAL_PATHS):
            return SEV_CRITICAL
        return SEV_HIGH
    if any(k in path for k in _CRITICAL_PATHS):
        return SEV_CRITICAL
    if any(k in path for k in _HIGH_PATHS) or cat in {
        "PACKAGING_ERROR",
        "DELIVERY_ERROR",
        "INSPECTION_ERROR",
        "SUBMISSION_ERROR",
        "INVOICE_ERROR",
        "FINANCING_ERROR",
        "SUPPLIER_ERROR",
    }:
        return SEV_HIGH
    if cat in {"PROVENANCE_ERROR", "CLAUSE_ERROR"}:
        return SEV_MEDIUM
    if cat in {"HISTORY_ERROR", "STALE_DATA"}:
        return SEV_LOW
    return SEV_MEDIUM


def consequence_for(category: str, field_path: str) -> str:
    cat = (category or "").upper()
    mapping = {
        "IDENTITY_ERROR": "Wrong product could be quoted, bid, or fulfilled.",
        "QUANTITY_UOM_ERROR": "Incorrect supplier quote, bid price, funding, or fulfillment quantity.",
        "PACKAGING_ERROR": "Noncompliant packaging may cause rejection or delayed acceptance.",
        "DELIVERY_ERROR": "Supplier quote could be rejected or performance become delinquent.",
        "AMENDMENT_ERROR": "Stale quantity/deadline/spec drives wrong bid and fulfillment.",
        "FALSE_READINESS": "Owner may approve a deal that is not executable or financeable.",
        "FALSE_REJECTION": "Legitimate opportunity may be dropped prematurely.",
        "FINANCING_ERROR": "Cash exposure or owner personal risk may be misstated.",
        "INVOICE_ERROR": "Invoice may be rejected (e.g. missing WAWF/receiving report).",
        "SUBMISSION_ERROR": "Bid may be late, misdirected, or incomplete.",
        "INSPECTION_ERROR": "Shipment may fail inspection/acceptance gates.",
        "SUPPLIER_ERROR": "Cannot confirm cost, availability, or compliance.",
        "PROVENANCE_ERROR": "Critical requirement lacks evidence — do not treat as fact.",
        "WORKFLOW_ERROR": "Operator may act on wrong stage or skip required work.",
    }
    return mapping.get(cat, f"Mismatch on {field_path} may affect procurement decisions.")

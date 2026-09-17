"""Layered package / quote readiness — PRICEABLE ≠ formal-quote-ready ≠ bid-ready."""

from __future__ import annotations

import re
from typing import Any

from live_package_completeness import assess_live_package_completeness
from pursuit_qualification_constants import (
    MAT_BID,
    MAT_COMPLIANCE,
    MAT_DELIVERY,
    MAT_NONMATERIAL,
    MAT_PRICING,
    MAT_UNKNOWN,
    PKG_BID,
    PKG_FORMAL_QUOTE,
    PKG_INCOMPLETE,
    PKG_PRELIMINARY,
    REQ_COMPLIANT,
    REQ_INSUFFICIENT,
    REQ_PRICEABLE,
)


def classify_document_materiality(doc: dict[str, Any]) -> str:
    title = str(doc.get("document_title") or doc.get("filename") or "").lower()
    klass = str(doc.get("document_class") or doc.get("document_type") or "").upper()
    access = str(doc.get("access_status") or "").upper()
    if access in {"PUBLIC_FETCHED", "FETCHED", "KNOWN_RETRIEVED"}:
        return MAT_NONMATERIAL
    if klass == "SPECIFICATION" or "spec" in title:
        return MAT_COMPLIANCE
    if klass in {"PRICING_SHEET", "BOM", "PRODUCT_SCHEDULE"} or "pricing" in title:
        return MAT_PRICING
    if "delivery" in title or "shipping" in title:
        return MAT_DELIVERY
    if klass in {"BID_FORM", "REPRESENTATIONS_CERTIFICATIONS"} or "bid form" in title:
        return MAT_BID
    if access in {"AUTH_REQUIRED", "LOGIN_REQUIRED", "LISTED_NO_PUBLIC_URL"}:
        return MAT_UNKNOWN
    return MAT_NONMATERIAL


def assess_package_readiness_layers(
    *,
    documents: list[dict[str, Any]] | None = None,
    line_items: list[dict[str, Any]] | None = None,
    terms: dict[str, Any] | None = None,
    deadline: str | None = None,
    deadline_status: str | None = None,
    auth_barriers: list[Any] | None = None,
    title: str | None = None,
    has_authoritative_text: bool = False,
) -> dict[str, Any]:
    """Separate preliminary analysis completeness from formal-quote and bid completeness."""
    base = assess_live_package_completeness(
        documents=documents,
        line_items=line_items,
        terms=terms,
        deadline=deadline,
        deadline_status=deadline_status,
        auth_barriers=auth_barriers,
        title=title,
        has_authoritative_text=has_authoritative_text,
    )
    docs = [d for d in (documents or []) if isinstance(d, dict)]
    lines = [li for li in (line_items or []) if isinstance(li, dict)]
    material_docs = []
    for d in docs:
        mat = classify_document_materiality(d)
        access = str(d.get("access_status") or "").upper()
        if access in {"AUTH_REQUIRED", "LOGIN_REQUIRED", "LISTED_NO_PUBLIC_URL", "PUBLIC_LISTED_NOT_FETCHED"}:
            material_docs.append(
                {
                    "document": d.get("document_title") or d.get("filename"),
                    "materiality": mat,
                    "access_status": access,
                }
            )

    has_qty = bool(lines) and all(
        li.get("quantity") is not None for li in lines if not li.get("is_service_line")
    )
    has_desc = bool(lines) and any(len(str(li.get("description") or "")) >= 8 for li in lines)
    priceable = has_qty and has_desc
    compliance_material_missing = any(m["materiality"] == MAT_COMPLIANCE for m in material_docs)
    delivery_tbd = False
    if isinstance((terms or {}).get("delivery_location"), dict):
        delivery_tbd = str((terms or {})["delivery_location"].get("value") or "").upper().startswith("TBD")

    if priceable:
        req_layer = REQ_PRICEABLE if compliance_material_missing else REQ_COMPLIANT
    else:
        req_layer = REQ_INSUFFICIENT

    preliminary_ok = priceable and bool(deadline) and has_authoritative_text
    formal_quote_ok = preliminary_ok and not compliance_material_missing and not delivery_tbd
    bid_ok = formal_quote_ok and not any(m["materiality"] == MAT_BID for m in material_docs)

    if bid_ok:
        layered_status = PKG_BID
    elif formal_quote_ok:
        layered_status = PKG_FORMAL_QUOTE
    elif preliminary_ok:
        layered_status = PKG_PRELIMINARY
    else:
        layered_status = PKG_INCOMPLETE

    return {
        "kind": "PackageReadinessLayers",
        "base_completeness": base,
        "requirement_layer": req_layer,
        "priceable_requirements": priceable,
        "compliant_product_requirements": req_layer == REQ_COMPLIANT,
        "preliminary_analysis_complete": preliminary_ok,
        "formal_quote_complete": formal_quote_ok,
        "bid_complete": bid_ok,
        "layered_status": layered_status,
        "material_documents": material_docs,
        "compliance_material_unresolved": compliance_material_missing,
        "notes": [
            "PRICEABLE_REQUIREMENTS ≠ COMPLIANT_PRODUCT_REQUIREMENTS",
            "PACKAGE_COMPLETE_FOR_PRELIMINARY_ANALYSIS ≠ PACKAGE_COMPLETE_FOR_FORMAL_QUOTE",
        ],
    }

"""Live package completeness assessment for transactional product deal selection.

Categories are intentionally coarse — selection aid, not fake precision.
"""

from __future__ import annotations

from typing import Any

from executable_deal_constants import LIVE_IOWA

AVAILABLE = "AVAILABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"
MISSING = "MISSING"
AUTH_REQUIRED = "AUTH_REQUIRED"
UNCERTAIN = "UNCERTAIN"

COMPLETENESS_CATEGORIES = (
    "authoritative_solicitation_notice",
    "response_deadline",
    "quantities",
    "line_items_product_schedule",
    "technical_specifications",
    "manufacturer_model_part_or_salient",
    "brand_or_equal_rules",
    "delivery_destination",
    "delivery_requirements_timing",
    "pricing_bid_structure",
    "material_terms",
    "amendments",
    "q_and_a",
    "mandatory_forms_certifications",
    "material_product_compliance",
)

CRITICAL_FOR_VALIDATION = frozenset(
    {
        "authoritative_solicitation_notice",
        "response_deadline",
        "quantities",
        "line_items_product_schedule",
        "technical_specifications",
        "manufacturer_model_part_or_salient",
        "delivery_destination",
        "pricing_bid_structure",
    }
)


def _cat(status: str, *, note: str | None = None) -> dict[str, Any]:
    return {"status": status, "note": note}


def assess_live_package_completeness(
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
    """Assess the 15 material categories for live candidate selection."""
    docs = [d for d in (documents or []) if isinstance(d, dict)]
    lines = [li for li in (line_items or []) if isinstance(li, dict)]
    terms = terms or {}
    auth_barriers = list(auth_barriers or [])

    fetched = [
        d
        for d in docs
        if str(d.get("access_status") or "").upper() in {"PUBLIC_FETCHED", "FETCHED", "KNOWN_RETRIEVED"}
        or d.get("retrieval_status") == "KNOWN_RETRIEVED"
        or (d.get("text") or d.get("text_preview") or d.get("text_length"))
    ]
    auth_docs = [
        d
        for d in docs
        if str(d.get("access_status") or "").upper() in {"AUTH_REQUIRED", "LOGIN_REQUIRED"}
    ]

    has_qty = bool(lines) and all(
        li.get("quantity") is not None for li in lines if not li.get("is_service_line")
    )
    has_desc = bool(lines) and any(li.get("description") for li in lines)
    has_mfr_or_part = any(
        li.get("manufacturer")
        or li.get("part_number")
        or li.get("model")
        or li.get("salient_characteristics")
        for li in lines
    )
    if not has_mfr_or_part and lines:
        has_mfr_or_part = any(len(str(li.get("description") or "")) >= 20 for li in lines)

    # Spec attachment listed/gated
    gated_spec = bool(auth_barriers) or any(
        str(d.get("document_class") or "").upper() == "SPECIFICATION"
        and str(d.get("access_status") or "").upper()
        in {
            "AUTH_REQUIRED",
            "LOGIN_REQUIRED",
            "LISTED_NO_PUBLIC_URL",
            "PUBLIC_LISTED_NOT_FETCHED",
        }
        for d in docs
    )

    delivery = None
    if isinstance(terms.get("delivery_location"), dict):
        delivery = terms["delivery_location"].get("value")
    delivery = delivery or next(
        (li.get("delivery_location") for li in lines if li.get("delivery_location")), None
    )
    delivery_available = bool(delivery)
    delivery_tbd = bool(delivery) and str(delivery).upper().startswith("TBD")

    # Auth-gated attachment is critical only when line items cannot identify the product
    product_identified = has_desc and has_qty and has_mfr_or_part
    critical_auth = gated_spec and not product_identified
    # Soft auth note: attachment gated but product schedule already usable
    soft_auth = gated_spec and product_identified

    dl = deadline
    if not dl and isinstance(terms.get("bid_deadline"), dict):
        dl = terms["bid_deadline"].get("value")
    deadline_ok = bool(dl) and str(deadline_status or "").upper() not in {
        "EXPIRED",
        "DEADLINE_CONFLICT",
    }

    brand = terms.get("brand_name_or_equal")
    brand_val = brand.get("value") if isinstance(brand, dict) else brand

    delivery_timing = None
    if isinstance(terms.get("delivery_deadline"), dict):
        delivery_timing = terms["delivery_deadline"].get("value")
    else:
        delivery_timing = terms.get("delivery_deadline")

    certs = None
    if isinstance(terms.get("required_certifications"), dict):
        certs = terms["required_certifications"].get("value")
    if not certs:
        certs = any(li.get("required_certifications") for li in lines)

    if critical_auth:
        tech_status = AUTH_REQUIRED
    elif soft_auth:
        tech_status = UNCERTAIN
    elif has_desc and has_mfr_or_part:
        tech_status = AVAILABLE
    elif has_desc:
        tech_status = UNCERTAIN
    else:
        tech_status = MISSING

    categories: dict[str, dict[str, Any]] = {
        "authoritative_solicitation_notice": _cat(
            AVAILABLE
            if (fetched or has_authoritative_text)
            else (AUTH_REQUIRED if auth_docs and not fetched else MISSING)
        ),
        "response_deadline": _cat(
            AVAILABLE if deadline_ok else (MISSING if not dl else UNCERTAIN),
            note=str(dl) if dl else None,
        ),
        "quantities": _cat(AVAILABLE if has_qty else (MISSING if lines else UNCERTAIN)),
        "line_items_product_schedule": _cat(AVAILABLE if has_desc else MISSING),
        "technical_specifications": _cat(
            tech_status,
            note="auth_attachment_present_but_line_items_usable" if soft_auth else None,
        ),
        "manufacturer_model_part_or_salient": _cat(
            AVAILABLE if has_mfr_or_part else (UNCERTAIN if has_desc else MISSING)
        ),
        "brand_or_equal_rules": _cat(
            AVAILABLE if brand_val is not None else (NOT_APPLICABLE if has_desc else UNCERTAIN)
        ),
        "delivery_destination": _cat(
            AVAILABLE if delivery_available else MISSING,
            note="buyer_will_provide_later" if delivery_tbd else None,
        ),
        "delivery_requirements_timing": _cat(AVAILABLE if delivery_timing else NOT_APPLICABLE),
        "pricing_bid_structure": _cat(AVAILABLE if lines else UNCERTAIN),
        "material_terms": _cat(
            AVAILABLE if (terms.get("FOB_terms") or terms.get("payment_terms") or fetched) else UNCERTAIN
        ),
        "amendments": _cat(
            AVAILABLE
            if any(str(d.get("document_class") or "").upper() == "AMENDMENT" for d in docs)
            else NOT_APPLICABLE
        ),
        "q_and_a": _cat(
            AVAILABLE
            if any(str(d.get("document_class") or "").upper() in {"Q_AND_A", "QA"} for d in docs)
            else NOT_APPLICABLE
        ),
        "mandatory_forms_certifications": _cat(UNCERTAIN),
        "material_product_compliance": _cat(AVAILABLE if certs else UNCERTAIN),
    }

    score = selection_completeness_score(
        categories, critical_auth=critical_auth, soft_auth=soft_auth
    )
    package_status = (
        "PACKAGE_COMPLETE_FOR_DEAL_ANALYSIS"
        if score["eligible_for_complete_validation"]
        else "PACKAGE_INCOMPLETE"
    )
    return {
        "kind": "LivePackageCompleteness",
        "categories": categories,
        "critical_auth_gated_spec": critical_auth,
        "soft_auth_gated_attachment": soft_auth,
        "score": score,
        "package_status": package_status,
        "title": title,
        "line_item_count": len(lines),
        "document_count": len(docs),
        "fetched_count": len(fetched),
    }


def selection_completeness_score(
    categories: dict[str, dict[str, Any]],
    *,
    critical_auth: bool = False,
    soft_auth: bool = False,
) -> dict[str, Any]:
    """Practical selection score — not fake precision."""
    weights = {
        "authoritative_solicitation_notice": 15,
        "response_deadline": 15,
        "quantities": 15,
        "line_items_product_schedule": 12,
        "technical_specifications": 12,
        "manufacturer_model_part_or_salient": 10,
        "delivery_destination": 8,
        "pricing_bid_structure": 8,
        "brand_or_equal_rules": 2,
        "delivery_requirements_timing": 1,
        "material_terms": 1,
        "amendments": 0,
        "q_and_a": 0,
        "mandatory_forms_certifications": 0,
        "material_product_compliance": 1,
    }
    total = 0
    max_pts = sum(weights.values())
    blockers: list[str] = []
    for key, w in weights.items():
        st = (categories.get(key) or {}).get("status")
        if st == AVAILABLE:
            total += w
        elif st == NOT_APPLICABLE:
            total += w
        elif st == UNCERTAIN and key == "technical_specifications" and soft_auth:
            total += int(w * 0.5)
        elif st == AUTH_REQUIRED:
            blockers.append(key)
        elif st == MISSING and key in CRITICAL_FOR_VALIDATION:
            blockers.append(key)
    if critical_auth:
        blockers.append("critical_specification_auth_gated")
        total = min(total, 55)
    if soft_auth:
        blockers.append("optional_or_supplemental_spec_auth_gated")
    tech = (categories.get("technical_specifications") or {}).get("status")
    eligible = (
        not critical_auth
        and (categories.get("response_deadline") or {}).get("status") == AVAILABLE
        and (categories.get("quantities") or {}).get("status") == AVAILABLE
        and (categories.get("line_items_product_schedule") or {}).get("status") == AVAILABLE
        and (categories.get("authoritative_solicitation_notice") or {}).get("status") == AVAILABLE
        and (categories.get("delivery_destination") or {}).get("status") == AVAILABLE
        and tech in {AVAILABLE, NOT_APPLICABLE, UNCERTAIN}
        and (categories.get("manufacturer_model_part_or_salient") or {}).get("status")
        in {AVAILABLE, UNCERTAIN}
    )
    # Prefer non-soft-auth packages when scoring equal — eligibility still true for soft_auth
    return {
        "points": total,
        "max_points": max_pts,
        "ratio": round(total / max_pts, 3) if max_pts else 0,
        "blockers": blockers,
        "eligible_for_complete_validation": eligible,
        "soft_auth_gated_attachment": soft_auth,
        "note": "selection aid only — not a precision quality metric",
    }


def is_forbidden_primary(solicitation_id: str | None) -> bool:
    """Iowa blade incomplete-package case must not be primary validation proof."""
    return str(solicitation_id or "").strip() == LIVE_IOWA


def selection_uses_forbidden_outcome_fields(candidate: dict[str, Any]) -> bool:
    """Guard: award winner / outcome must not drive live selection."""
    forbidden = (
        "award_winner",
        "winning_vendor",
        "winning_bid_amount",
        "award_amount",
        "hidden_benchmark_outcome",
        "benchmark_answer_key",
    )
    return any(candidate.get(k) is not None for k in forbidden)

"""Commercial verification plan + supplier targets — prepare only, never contact."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from commercial_verification_constants import (
    CAT_AUTHORIZED_RESELLER,
    CAT_COUNTRY_OF_ORIGIN,
    CAT_DELIVERY,
    CAT_FINANCING_COST,
    CAT_FINANCING_ELIGIBILITY,
    CAT_FREIGHT,
    CAT_LEAD_TIME,
    CAT_MINIMUM_FICO,
    CAT_OEM_AUTHORIZATION,
    CAT_PERSONAL_CREDIT,
    CAT_PERSONAL_GUARANTEE,
    CAT_PRICE_VALIDITY,
    CAT_PRODUCT_AVAILABILITY,
    CAT_PRODUCT_COMPLIANCE_DOC,
    CAT_SUPPLIER_PRICE,
    FUTURE_ACTION,
    NEG_HARD_CEILING,
    NEG_TARGET,
    NEG_WALK_AWAY,
    PRI_DEAL_KILLER,
    PRI_HIGH,
    PRI_LOW,
    PRI_MATERIAL,
    PRI_OPTIONAL,
    V_NOT_NEEDED,
    V_PENDING,
)
from economic_integrity import min_actual_profit_usd


def _item_id(opportunity_id: str, category: str, question: str) -> str:
    h = hashlib.sha256(f"{opportunity_id}|{category}|{question}".encode()).hexdigest()[:12]
    return f"CVI-{h}"


def verification_item(
    *,
    opportunity_id: str,
    category: str,
    exact_question: str,
    why_needed: str,
    current_value: Any = None,
    current_confidence: str = "UNKNOWN",
    target_value: Any = None,
    maximum_acceptable: Any = None,
    minimum_acceptable: Any = None,
    evidence_needed: str | None = None,
    preferred_source: str | None = None,
    deadline: str | None = None,
    economic_deps: list[str] | None = None,
    compliance_deps: list[str] | None = None,
    funding_deps: list[str] | None = None,
    readiness_deps: list[str] | None = None,
    estimated_cost: float = 0.0,
    voi: str = "HIGH",
    priority: str = PRI_MATERIAL,
    status: str = V_PENDING,
) -> dict[str, Any]:
    return {
        "kind": "CommercialVerificationItem",
        "opportunity_id": opportunity_id,
        "item_id": _item_id(opportunity_id, category, exact_question),
        "category": category,
        "exact_question": exact_question,
        "why_needed": why_needed,
        "current_value": current_value,
        "current_confidence": current_confidence,
        "target_value": target_value,
        "maximum_acceptable_value": maximum_acceptable,
        "minimum_acceptable_value": minimum_acceptable,
        "evidence_needed": evidence_needed,
        "preferred_verification_source": preferred_source,
        "deadline": deadline,
        "economic_dependencies": economic_deps or [],
        "compliance_dependencies": compliance_deps or [],
        "funding_dependencies": funding_deps or [],
        "readiness_dependencies": readiness_deps or [],
        "estimated_verification_cost": estimated_cost,
        "voi": voi,
        "priority": priority,
        "status": status,
        "proposed_action": FUTURE_ACTION,
        "operator_authorization_state": "NOT_AUTHORIZED",
        "result": None,
        "evidence_provenance": None,
        "verified_at": None,
        "expires_at": None,
        "created_at": now_utc().isoformat(),
    }


def supplier_verification_target(
    *,
    product: str,
    configuration: str | None = None,
    quantity: float | None = None,
    unit: str = "EA",
    target_unit_cost: float | None = None,
    hard_ceiling_unit: float | None = None,
    hard_ceiling_landed: float | None = None,
    must_ship_by: str | None = None,
    quote_valid_through: str | None = None,
    destination: str | None = None,
    freight_requirement: str | None = None,
    authorization_requirement: str | None = None,
    compliance_docs: list[str] | None = None,
    acceptable_substitutions: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "SupplierVerificationTarget",
        "product": product,
        "configuration": configuration,
        "quantity": quantity,
        "unit": unit,
        "bands": {
            NEG_TARGET: target_unit_cost,
            NEG_HARD_CEILING: hard_ceiling_unit,
            "HARD_CEILING_LANDED": hard_ceiling_landed,
            NEG_WALK_AWAY: hard_ceiling_unit,
        },
        "must_ship_by": must_ship_by,
        "quote_valid_through": quote_valid_through,
        "destination": destination,
        "freight_requirement": freight_requirement,
        "authorization_requirement": authorization_requirement,
        "compliance_documents_needed": compliance_docs or [],
        "acceptable_substitutions": acceptable_substitutions,
        "proposed_action": FUTURE_ACTION,
        "script_summary": (
            f"Need supplier confirmation for: {quantity} × {product}. "
            f"Target: <= ${target_unit_cost}/unit. "
            f"Hard ceiling: <= ${hard_ceiling_landed or hard_ceiling_unit} landed. "
            f"Must ship by: {must_ship_by or 'UNKNOWN'}. "
            f"Quote valid through: {quote_valid_through or 'UNKNOWN'}."
        ),
        "contact_performed": False,
    }


def build_commercial_verification_plan(
    *,
    opportunity_id: str,
    acquisition_estimate: float | None = None,
    acquisition_confidence: str = "UNKNOWN",
    max_acquisition: float | None = None,
    target_acquisition: float | None = None,
    freight_estimate: float | None = None,
    freight_confidence: str = "UNKNOWN",
    financing_required: bool | None = None,
    financing_amount: float | None = None,
    max_financing_cost: float | None = None,
    financing_confidence: str = "UNKNOWN",
    stock_verified: bool = False,
    lead_time_verified: bool = False,
    delivery_by: str | None = None,
    authorization_required: bool = False,
    authorization_verified: bool = False,
    origin_required: bool = False,
    origin_verified: bool = False,
    product_description: str | None = None,
    quantity: float | None = None,
    unit_target: float | None = None,
    unit_ceiling: float | None = None,
    quote_valid_through: str | None = None,
    destination: str | None = None,
) -> dict[str, Any]:
    """Only request verification for facts not already authoritative/current."""
    items: list[dict[str, Any]] = []

    def add(**kwargs: Any) -> None:
        items.append(verification_item(opportunity_id=opportunity_id, **kwargs))

    # Supplier price — deal killer if unknown or estimate
    if acquisition_confidence in {"UNKNOWN", "COMMERCIAL_VERIFICATION_REQUIRED", "DEFENSIBLE_ESTIMATE", "STALE", "COMPARABLE_ESTIMATE", "RECENT_HISTORICAL"}:
        add(
            category=CAT_SUPPLIER_PRICE,
            exact_question="What is the binding unit/extended acquisition price for the exact configuration?",
            why_needed="Estimated acquisition cost is not a binding quote; economics and financing depend on it",
            current_value=acquisition_estimate,
            current_confidence=acquisition_confidence,
            target_value=target_acquisition or acquisition_estimate,
            maximum_acceptable=max_acquisition,
            evidence_needed="BINDING_QUOTE",
            preferred_source="supplier",
            economic_deps=["acquisition_cost", "profit", "financing_amount"],
            readiness_deps=["pricing", "execution"],
            priority=PRI_DEAL_KILLER,
            voi="CRITICAL",
        )
    else:
        items.append(
            verification_item(
                opportunity_id=opportunity_id,
                category=CAT_SUPPLIER_PRICE,
                exact_question="Supplier price already verified",
                why_needed="Authoritative acquisition evidence present",
                current_value=acquisition_estimate,
                current_confidence=acquisition_confidence,
                status=V_NOT_NEEDED,
                priority=PRI_OPTIONAL,
            )
        )

    if not stock_verified:
        add(
            category=CAT_PRODUCT_AVAILABILITY,
            exact_question="Is the exact quantity currently available or allocatable?",
            why_needed="Without stock, delivery and awardability fail",
            preferred_source="supplier",
            economic_deps=["availability"],
            priority=PRI_DEAL_KILLER,
            voi="CRITICAL",
        )

    if not lead_time_verified:
        add(
            category=CAT_LEAD_TIME,
            exact_question=f"Can product ship to meet delivery by {delivery_by or 'REQUIRED DATE'}?",
            why_needed="Lead time failure kills delivery compliance",
            preferred_source="supplier",
            deadline=delivery_by,
            economic_deps=["delivery_viability"],
            compliance_deps=["delivery"],
            priority=PRI_HIGH,
            voi="HIGH",
        )

    if freight_confidence in {"UNKNOWN", "COMMERCIAL_VERIFICATION_REQUIRED"} or freight_estimate is None:
        add(
            category=CAT_FREIGHT,
            exact_question="What is verified freight/logistics cost to destination?",
            why_needed="Unknown freight cannot be treated as $0",
            current_value=freight_estimate,
            current_confidence=freight_confidence,
            preferred_source="carrier_or_supplier",
            economic_deps=["freight", "profit"],
            priority=PRI_MATERIAL if (freight_estimate or 0) < 5000 else PRI_HIGH,
            voi="MEDIUM",
        )

    add(
        category=CAT_PRICE_VALIDITY,
        exact_question="Through what date does the supplier quote remain valid?",
        why_needed="Stale quotes cannot support final bid readiness",
        target_value=quote_valid_through,
        preferred_source="supplier",
        priority=PRI_MATERIAL,
        voi="MEDIUM",
    )

    if authorization_required and not authorization_verified:
        add(
            category=CAT_OEM_AUTHORIZATION,
            exact_question="Can supplier provide required OEM/authorized-reseller documentation?",
            why_needed="Authorization gap is a compliance hard blocker",
            preferred_source="supplier_or_oem",
            compliance_deps=["authorization"],
            priority=PRI_DEAL_KILLER,
            voi="CRITICAL",
        )

    if origin_required and not origin_verified:
        add(
            category=CAT_COUNTRY_OF_ORIGIN,
            exact_question="What is documented country of origin / domestic-content status?",
            why_needed="Origin documentation may be mandatory",
            preferred_source="supplier",
            compliance_deps=["origin"],
            priority=PRI_HIGH,
            voi="HIGH",
        )

    needs_funding = financing_required if financing_required is not None else (
        acquisition_estimate is not None and acquisition_estimate >= 10000
    )
    if needs_funding:
        add(
            category=CAT_FINANCING_ELIGIBILITY,
            exact_question=f"Can a financier fund ~${financing_amount or acquisition_estimate:,.0f} for this government product-resale transaction?",
            why_needed="Deal requiring external capital is not executable without funding feasibility",
            current_value=financing_amount,
            current_confidence=financing_confidence,
            maximum_acceptable=max_financing_cost,
            preferred_source="financier",
            funding_deps=["funding_feasibility"],
            economic_deps=["financing_cost", "profit"],
            readiness_deps=["execution"],
            priority=PRI_DEAL_KILLER,
            voi="CRITICAL",
        )
        add(
            category=CAT_FINANCING_COST,
            exact_question="What is expected total financing cost for this transaction?",
            why_needed="Financing cost can erase profit floor",
            maximum_acceptable=max_financing_cost,
            preferred_source="financier",
            funding_deps=["financing_cost"],
            priority=PRI_HIGH,
            voi="HIGH",
        )
        for cat, q in [
            (CAT_PERSONAL_GUARANTEE, "Is a personal guarantee required?"),
            (CAT_PERSONAL_CREDIT, "Is personal credit pulled, and does approval depend materially on personal credit?"),
            (CAT_MINIMUM_FICO, "Is there a minimum FICO requirement?"),
        ]:
            add(
                category=cat,
                exact_question=q,
                why_needed="PG ≠ personal-credit dependency; FICO may hard-block",
                preferred_source="financier",
                funding_deps=["compatibility"],
                priority=PRI_HIGH if cat == CAT_MINIMUM_FICO else PRI_MATERIAL,
                voi="HIGH",
            )

    if delivery_by:
        add(
            category=CAT_DELIVERY,
            exact_question=f"Will supplier commit delivery by {delivery_by}?",
            why_needed="Delivery commitment required for bid readiness",
            preferred_source="supplier",
            deadline=delivery_by,
            priority=PRI_HIGH,
            voi="HIGH",
        )

    # Sort: deal killers first
    rank = {PRI_DEAL_KILLER: 0, PRI_HIGH: 1, PRI_MATERIAL: 2, PRI_LOW: 3, PRI_OPTIONAL: 4}
    items.sort(key=lambda x: (rank.get(x["priority"], 9), x["category"]))

    supplier_target = None
    if product_description:
        supplier_target = supplier_verification_target(
            product=product_description,
            quantity=quantity,
            target_unit_cost=unit_target,
            hard_ceiling_unit=unit_ceiling,
            hard_ceiling_landed=max_acquisition,
            must_ship_by=delivery_by,
            quote_valid_through=quote_valid_through,
            destination=destination,
            authorization_requirement="required" if authorization_required else None,
        )

    active = [i for i in items if i["status"] != V_NOT_NEEDED]
    return {
        "kind": "CommercialVerificationPlan",
        "opportunity_id": opportunity_id,
        "items": items,
        "active_items": active,
        "deal_killer_count": sum(1 for i in active if i["priority"] == PRI_DEAL_KILLER),
        "supplier_target": supplier_target,
        "proposed_action": FUTURE_ACTION,
        "outreach_performed": False,
        "created_at": now_utc().isoformat(),
    }

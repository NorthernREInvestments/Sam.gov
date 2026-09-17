"""Deal-type qualification, pre-deep gate, and cheap second-stage classification."""

from __future__ import annotations

import re
from typing import Any

from deep_deal_constants import (
    CHEAP_INSUFFICIENT,
    CHEAP_LIKELY_PRODUCT,
    CHEAP_LIKELY_PRODUCT_PLUS,
    CHEAP_LIKELY_SERVICE,
    DEAL_BPA,
    DEAL_CATALOG,
    DEAL_CONSTRUCTION,
    DEAL_COOP_MASTER,
    DEAL_IDIQ,
    DEAL_LEASE,
    DEAL_ONE_TIME_PRODUCT,
    DEAL_PRODUCT_PLUS_INSTALL,
    DEAL_REQUIREMENTS,
    DEAL_SERVICE,
    DEAL_UNKNOWN,
    FIT_CORE_TRANSACTIONAL,
    FIT_LONG_TERM_CHANNEL,
    FIT_NEEDS_DOC_REVIEW,
    FIT_NOT_OUR_MODEL,
    FIT_POOR_LAUNCH,
    FIT_POTENTIAL_VEHICLE,
    FIT_PRODUCT_PLUS_SUB,
    QUEUE_CHEAP_CLASS,
    QUEUE_DEEP,
    QUEUE_IMMEDIATE,
    QUEUE_REJECTED,
    QUEUE_STRATEGIC,
    REV_NONE,
    REV_POTENTIAL,
    REV_UNKNOWN,
)

_COOP_SIGNALS = (
    r"\bsourcewell\b",
    r"\bcooperative\b",
    r"\bcoop\b",
    r"\bnaspo\b",
    r"\bvaluepoint\b",
    r"\bh\-?gac\b",
    r"\bomnia\b",
    r"\bbuyboard\b",
    r"\bmaster\s+agreement\b",
    r"\bparticipating\s+addendum\b",
    r"\bmembers?\s+may\s+purchase\b",
    r"\bauthorized\s+vendors?\b",
)

_IDIQ_SIGNALS = (
    r"\bidiq\b",
    r"\bindefinite\s+delivery\b",
    r"\bindefinite\s+quantity\b",
    r"\btask\s+order\b",
    r"\bdelivery\s+order\b",
)

_BPA_SIGNALS = (
    r"\bbpa\b",
    r"\bblanket\s+purchase\b",
    r"\bblanket\s+agreement\b",
)

_CATALOG_SIGNALS = (
    r"\bcatalog\b",
    r"\bdiscount\s+schedule\b",
    r"\bgsa\s+schedule\b",
    r"\bprice\s+list\b",
)

_CONSTRUCTION_SIGNALS = (
    r"\bhvac\s+upgrade\b",
    r"\bconstruction\b",
    r"\brenovation\b",
    r"\bremodel\b",
    r"\bdesign[\s-]build\b",
    r"\bturnkey\b",
    r"\bsite\s+work\b",
    r"\binstall(?:ation)?\s+and\s+commission\b",
)

_INSTALL_SIGNALS = (
    r"\bsupply\s+and\s+install\b",
    r"\bfurnish\s+and\s+install\b",
    r"\bprovide\s+and\s+install\b",
    r"\binstallation\s+required\b",
)

_SERVICE_SIGNALS = (
    r"\bprofessional\s+services?\b",
    r"\bconsulting\b",
    r"\bconsultant\b",
    r"\bstaffing\b",
    r"\bmaintenance\s+services?\b",
    r"\bjanitorial\b",
    r"\bbenefits?\s+consultant\b",
    r"\bpre[\s-]qualify\s+vendors?\b",
    r"\bmanagement\s+system\b",
    r"\bgarage\s+building\b",
)

_LEASE_SIGNALS = (r"\blease\b", r"\brental\b", r"\brent[\s-]to[\s-]own\b")

_ONE_TIME_SIGNALS = (
    r"\brfq\b",
    r"\bifb\b",
    r"\brfb\b",
    r"\bitb\b",
    r"\binformal\s+bid\b",
    r"\bsealed\s+bid\b",
    r"\bpurchase\s+of\b",
    r"\bprocurement\s+of\b",
    r"\bone[\s-]time\b",
    r"\bfirm[\s-]fixed[\s-]price\b",
    r"\bfor\s+snow/?ice\s+removal\b",
    r"\breplacement\b",
)

_PRODUCT_HINTS = (
    r"\bequipment\b",
    r"\bsupplies\b",
    r"\bhardware\b",
    r"\bvehicles?\b",
    r"\bparts?\b",
    r"\bmaterials?\b",
    r"\bcomputers?\b",
    r"\blaptops?\b",
    r"\bservers?\b",
    r"\bgenerators?\b",
    r"\bfurniture\b",
    r"\btools?\b",
    r"\bblades?\b",
    r"\bpumps?\b",
    r"\bvalves?\b",
    r"\bappliances?\b",
    r"\bmonitors?\b",
    r"\bplaner\b",
    r"\bseed\b",
    r"\bwheelchair\s+lift\b",
    r"\bbadges?\b",
    r"\btank\b",
    r"\bsigns?\b",
    r"\bchairs?\b",
    r"\btractors?\b",
    r"\bmachinery\b",
)

_QUANTITY_SIGNALS = (
    r"\b\d+\s*(?:ea|each|units?|pcs?|pieces?)\b",
    r"\bqty\b",
    r"\bquantity\b",
)


def _blob(row: dict[str, Any]) -> str:
    parts = [
        row.get("title"),
        row.get("description"),
        row.get("agency"),
        row.get("source_id"),
        row.get("product_classification"),
        row.get("what_they_are_buying"),
        " ".join(str(x) for x in (row.get("document_names") or [])),
        row.get("commodity_hint"),
        row.get("notice_type"),
    ]
    return " ".join(str(p or "") for p in parts).lower()


def _hits(patterns: tuple[str, ...], text: str) -> list[str]:
    return [p for p in patterns if re.search(p, text, re.I)]


def classify_deal_type(row: dict[str, Any], *, document_text: str | None = None) -> dict[str, Any]:
    """
    Deterministic deal-type classification.
    Cooperative master contracts are NOT treated as one-time PO revenue.
    """
    text = _blob(row)
    if document_text:
        text = f"{text} {document_text[:20000].lower()}"

    reasons: list[str] = []
    deal_type = DEAL_UNKNOWN
    revenue_certainty = REV_UNKNOWN
    purchase_commitment = "UNKNOWN"
    guaranteed_quantity = None
    estimated_quantity = None
    ordering_mechanism = "UNKNOWN"
    base_term = None
    option_terms = None

    coop = _hits(_COOP_SIGNALS, text)
    idiq = _hits(_IDIQ_SIGNALS, text)
    bpa = _hits(_BPA_SIGNALS, text)
    catalog = _hits(_CATALOG_SIGNALS, text)
    construction = _hits(_CONSTRUCTION_SIGNALS, text)
    install = _hits(_INSTALL_SIGNALS, text)
    service = _hits(_SERVICE_SIGNALS, text)
    lease = _hits(_LEASE_SIGNALS, text)
    one_time = _hits(_ONE_TIME_SIGNALS, text)

    source_id = str(row.get("source_id") or "").lower()
    agency = str(row.get("agency") or "").lower()

    if coop or "sourcewell" in source_id or "sourcewell" in agency or "coop_" in source_id:
        deal_type = DEAL_COOP_MASTER
        revenue_certainty = REV_NONE
        purchase_commitment = "NONE_UNTIL_MEMBER_ORDER"
        ordering_mechanism = "MEMBER_PURCHASE_ORDERS_AFTER_AWARD"
        reasons.append("cooperative_or_master_contract_signals")
    elif idiq:
        deal_type = DEAL_IDIQ
        revenue_certainty = REV_POTENTIAL
        purchase_commitment = "INDEFINITE"
        ordering_mechanism = "TASK_OR_DELIVERY_ORDERS"
        reasons.append("idiq_signals")
    elif bpa:
        deal_type = DEAL_BPA
        revenue_certainty = REV_POTENTIAL
        purchase_commitment = "BLANKET"
        ordering_mechanism = "CALLS_AGAINST_BPA"
        reasons.append("bpa_signals")
    elif catalog:
        deal_type = DEAL_CATALOG
        revenue_certainty = REV_POTENTIAL
        purchase_commitment = "CATALOG"
        ordering_mechanism = "CATALOG_ORDERS"
        reasons.append("catalog_signals")
    elif construction and not one_time:
        deal_type = DEAL_CONSTRUCTION
        revenue_certainty = REV_UNKNOWN
        reasons.append("construction_upgrade_signals")
    elif install and _hits(_PRODUCT_HINTS, text):
        deal_type = DEAL_PRODUCT_PLUS_INSTALL
        revenue_certainty = REV_UNKNOWN
        reasons.append("product_plus_installation_signals")
    elif service and not _hits(_PRODUCT_HINTS, text):
        deal_type = DEAL_SERVICE
        revenue_certainty = REV_UNKNOWN
        reasons.append("service_contract_signals")
    elif lease:
        deal_type = DEAL_LEASE
        revenue_certainty = REV_UNKNOWN
        reasons.append("lease_rental_signals")
    elif one_time or re.search(r"\brfq\b|\bifb\b|\brfb\b|\bitb\b", text, re.I):
        deal_type = DEAL_ONE_TIME_PRODUCT if _hits(_PRODUCT_HINTS, text) or row.get("product_classification") in {
            "CORE_PRODUCT",
            "PRODUCT_PLUS_SERVICE",
        } else DEAL_UNKNOWN
        if deal_type == DEAL_ONE_TIME_PRODUCT:
            revenue_certainty = REV_UNKNOWN
            purchase_commitment = "SINGLE_AWARD_IF_WON"
            ordering_mechanism = "DIRECT_AWARD_PO"
            reasons.append("one_time_product_purchase_signals")
        else:
            reasons.append("solicitation_signals_but_product_unclear")
    elif (
        _hits(_PRODUCT_HINTS, text)
        and not service
        and not construction
        and not install
        and not lease
        and not coop
        and not idiq
        and not bpa
        and "naspo" not in text
        and "valuepoint" not in text
    ):
        # Commodity-style title without vehicle/service language — likely one-time purchase (conservative)
        deal_type = DEAL_ONE_TIME_PRODUCT
        revenue_certainty = REV_UNKNOWN
        purchase_commitment = "LIKELY_SINGLE_AWARD"
        ordering_mechanism = "DIRECT_AWARD_PO"
        reasons.append("product_commodity_title_without_vehicle_or_service_signals")
    else:
        reasons.append("insufficient_deal_type_evidence")

    # HVAC upgrade title pattern → construction unless docs prove equipment-only
    if re.search(r"\bhvac\b.*\bupgrade\b|\bupgrade\b.*\bhvac\b", text, re.I):
        if deal_type not in {DEAL_COOP_MASTER, DEAL_IDIQ, DEAL_BPA}:
            deal_type = DEAL_CONSTRUCTION
            reasons.append("hvac_upgrade_treated_as_construction_until_docs_prove_otherwise")

    return {
        "deal_type": deal_type,
        "revenue_certainty": revenue_certainty,
        "purchase_commitment": purchase_commitment,
        "guaranteed_quantity": guaranteed_quantity,
        "estimated_quantity": estimated_quantity,
        "ordering_mechanism": ordering_mechanism,
        "base_term": base_term,
        "option_terms": option_terms,
        "deal_type_reasons": reasons,
        "signals": {
            "coop": len(coop),
            "idiq": len(idiq),
            "construction": len(construction),
            "install": len(install),
            "service": len(service),
            "one_time": len(one_time),
        },
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def evaluate_pre_deep_fit(row: dict[str, Any], deal_type_result: dict[str, Any] | None = None) -> dict[str, Any]:
    """IS THIS ACTUALLY OUR TYPE OF DEAL? — before expensive research."""
    dt = deal_type_result or classify_deal_type(row)
    deal_type = dt.get("deal_type") or DEAL_UNKNOWN
    product = row.get("product_classification") or row.get("document_classification") or "UNKNOWN"
    reasons: list[str] = []

    if deal_type == DEAL_SERVICE or product in {"SERVICE", "CLEARLY_IRRELEVANT"}:
        fit = FIT_NOT_OUR_MODEL
        reasons.append("service_or_non_product")
        admit_deep = False
        horizon = "NOT_APPLICABLE"
    elif deal_type == DEAL_COOP_MASTER:
        fit = FIT_LONG_TERM_CHANNEL
        reasons.append("cooperative_master_is_channel_not_immediate_po")
        admit_deep = False  # docs skim only; no full economics as if PO
        horizon = "LONG_TERM_CHANNEL"
    elif deal_type in {DEAL_IDIQ, DEAL_BPA, DEAL_CATALOG, DEAL_REQUIREMENTS}:
        fit = FIT_POTENTIAL_VEHICLE
        reasons.append("vehicle_without_committed_transaction")
        admit_deep = False
        horizon = "LONG_TERM_CHANNEL"
    elif deal_type == DEAL_CONSTRUCTION:
        fit = FIT_POOR_LAUNCH
        reasons.append("construction_or_upgrade_likely_self_performance")
        admit_deep = True  # allow document review to confirm/reject
        horizon = "SHORT_TERM_IF_PRODUCT_DOMINANT"
    elif deal_type == DEAL_PRODUCT_PLUS_INSTALL:
        fit = FIT_PRODUCT_PLUS_SUB
        reasons.append("product_plus_subcontractable_work")
        admit_deep = True
        horizon = "SHORT_TERM_CASH"
    elif deal_type == DEAL_ONE_TIME_PRODUCT and (
        product in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"}
        or row.get("cheap_classification") in {CHEAP_LIKELY_PRODUCT, CHEAP_LIKELY_PRODUCT_PLUS}
    ):
        if product == "PRODUCT_PLUS_SERVICE" or row.get("cheap_classification") == CHEAP_LIKELY_PRODUCT_PLUS:
            fit = FIT_PRODUCT_PLUS_SUB
        else:
            fit = FIT_CORE_TRANSACTIONAL
        reasons.append("one_time_product_resale_candidate")
        admit_deep = True
        horizon = "SHORT_TERM_CASH"
    elif deal_type == DEAL_ONE_TIME_PRODUCT:
        fit = FIT_NEEDS_DOC_REVIEW
        reasons.append("one_time_signals_product_class_uncertain")
        admit_deep = True
        horizon = "SHORT_TERM_CASH"
    elif deal_type == DEAL_LEASE:
        fit = FIT_POOR_LAUNCH
        reasons.append("lease_rental_economics_uncertain")
        admit_deep = False
        horizon = "NOT_APPLICABLE"
    else:
        fit = FIT_NEEDS_DOC_REVIEW
        reasons.append("deal_type_or_product_unclear")
        admit_deep = True
        horizon = "UNKNOWN"

    return {
        "pre_deep_fit": fit,
        "admit_to_deep_research": admit_deep,
        "opportunity_horizon": horizon,
        "pre_deep_reasons": reasons,
        "deal_type": deal_type,
        "revenue_certainty": dt.get("revenue_certainty"),
        "note": (
            "Cooperative ceilings / historical program sales are NOT guaranteed revenue"
            if deal_type == DEAL_COOP_MASTER
            else None
        ),
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def cheap_second_stage_classify(row: dict[str, Any]) -> dict[str, Any]:
    """Cheap triage for UNKNOWN listings — no OpenAI, no document download."""
    text = _blob(row)
    product_hits = _hits(_PRODUCT_HINTS, text)
    service_hits = _hits(_SERVICE_SIGNALS, text)
    install_hits = _hits(_INSTALL_SIGNALS, text)
    listing = row.get("product_classification") or "UNKNOWN"

    if listing in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE", "SERVICE", "CLEARLY_IRRELEVANT"}:
        return {
            "cheap_classification": {
                "CORE_PRODUCT": CHEAP_LIKELY_PRODUCT,
                "PRODUCT_PLUS_SERVICE": CHEAP_LIKELY_PRODUCT_PLUS,
                "SERVICE": CHEAP_LIKELY_SERVICE,
                "CLEARLY_IRRELEVANT": CHEAP_LIKELY_SERVICE,
            }.get(listing, CHEAP_INSUFFICIENT),
            "listing_classification": listing,
            "classification_changed": False,
            "classification_reason": "listing_already_classified",
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
        }

    if product_hits and install_hits:
        cheap = CHEAP_LIKELY_PRODUCT_PLUS
        reason = "product_and_installation_title_signals"
    elif product_hits and not service_hits:
        cheap = CHEAP_LIKELY_PRODUCT
        reason = "product_title_commodity_signals"
    elif service_hits and not product_hits:
        cheap = CHEAP_LIKELY_SERVICE
        reason = "service_title_signals"
    elif product_hits and service_hits:
        cheap = CHEAP_LIKELY_PRODUCT_PLUS
        reason = "mixed_product_service_signals"
    else:
        cheap = CHEAP_INSUFFICIENT
        reason = "insufficient_title_description_evidence"

    return {
        "cheap_classification": cheap,
        "listing_classification": listing,
        "classification_changed": cheap != CHEAP_INSUFFICIENT,
        "classification_reason": reason,
        "product_signal_count": len(product_hits),
        "service_signal_count": len(service_hits),
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def assign_operator_queue_bucket(
    *,
    pre_deep_fit: str | None,
    deal_type: str | None,
    cheap_classification: str | None = None,
    bid_state: str | None = None,
) -> str:
    """Separate immediate resale from strategic vehicles and unknowns."""
    if pre_deep_fit == FIT_NOT_OUR_MODEL or bid_state == "REJECTED":
        return QUEUE_REJECTED
    if deal_type == DEAL_COOP_MASTER or pre_deep_fit in {FIT_LONG_TERM_CHANNEL, FIT_POTENTIAL_VEHICLE}:
        return QUEUE_STRATEGIC
    if pre_deep_fit == FIT_CORE_TRANSACTIONAL:
        return QUEUE_IMMEDIATE
    if cheap_classification == CHEAP_INSUFFICIENT or pre_deep_fit == FIT_NEEDS_DOC_REVIEW:
        return QUEUE_CHEAP_CLASS
    if pre_deep_fit in {FIT_PRODUCT_PLUS_SUB, FIT_POOR_LAUNCH}:
        return QUEUE_DEEP
    return QUEUE_CHEAP_CLASS


def qualify_candidate(row: dict[str, Any], *, document_text: str | None = None) -> dict[str, Any]:
    """Full pre-research qualification package for one candidate."""
    deal = classify_deal_type(row, document_text=document_text)
    fit = evaluate_pre_deep_fit(row, deal)
    cheap = cheap_second_stage_classify(row)
    bucket = assign_operator_queue_bucket(
        pre_deep_fit=fit.get("pre_deep_fit"),
        deal_type=deal.get("deal_type"),
        cheap_classification=cheap.get("cheap_classification"),
    )
    return {
        **deal,
        **fit,
        **cheap,
        "operator_queue_bucket": bucket,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "USAspending": 0,
        "paid": 0,
    }

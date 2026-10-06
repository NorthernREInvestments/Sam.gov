"""Owner next-action + What Can Hurt Us + UI mapping."""

from __future__ import annotations

from typing import Any

from basket_full_funnel_reconcile.models import (
    BASKET_BLOCKED,
    BASKET_READY_PUBLIC_PRICE,
    BASKET_READY_QUOTE_DEPENDENT,
    ECON_EXECUTION_BLOCKED,
    FINANCING_BLOCKED,
    LIKELY_PROFITABLE,
    POSSIBLE_PROFIT,
    PROVEN_PROFITABLE,
    QUOTE_REQUIRED,
    RESEARCH_EXHAUSTED,
    UNPROFITABLE,
)


OWNER_STAGE_LABELS = [
    ("DISCOVERED", "Discovered"),
    ("PACKAGE_VERIFIED", "Package"),
    ("ELIGIBILITY_CLEARED", "Eligibility"),
    ("COMMERCIAL_IDENTITY_READY", "Products"),
    ("REVENUE_EVIDENCE_READY", "Government value"),
    ("ACQUISITION_COST_READY", "Supplier cost"),
    ("BASKET_READY", "Basket"),
    ("FREIGHT_READY", "Shipping"),
    ("FINANCING_READY", "Financing"),
    ("ECONOMICS_READY", "Profit"),
    ("EXECUTION_CHECKED", "Execution risks"),
    ("BID_READY", "Ready status"),
]


def derive_canonical_stage(result: dict[str, Any]) -> str:
    econ = result.get("economics") or {}
    basket = result.get("basket") or {}
    terminal = econ.get("economic_terminal")
    if terminal == UNPROFITABLE:
        return "REJECTED"
    if terminal == ECON_EXECUTION_BLOCKED:
        return "BLOCKED"
    if basket.get("basket_class") == BASKET_READY_QUOTE_DEPENDENT:
        return "QUOTE_RESERVE"
    if terminal in {PROVEN_PROFITABLE, LIKELY_PROFITABLE} and econ.get("financing_status") not in {FINANCING_BLOCKED}:
        if float(econ.get("net_expected_profit") or 0) >= 5000:
            return "LENDER_READY"
        return "ECONOMICS_READY"
    if basket.get("basket_class") in {BASKET_READY_PUBLIC_PRICE, BASKET_READY_QUOTE_DEPENDENT}:
        return "BASKET_READY"
    if int((result.get("lines") or {}).get("PRICED_LINES") or 0) > 0:
        return "ACQUISITION_COST_READY"
    if int((result.get("lines") or {}).get("IDENTIFIED_LINES") or 0) > 0:
        return "COMMERCIAL_IDENTITY_READY"
    return "LINES_EXTRACTED"


def owner_next_action(result: dict[str, Any]) -> dict[str, str]:
    econ = result.get("economics") or {}
    basket = result.get("basket") or {}
    terminal = econ.get("economic_terminal")
    lines = result.get("lines") or {}
    quote_n = int(lines.get("QUOTE_REQUIRED_LINES") or 0)

    if terminal == PROVEN_PROFITABLE and derive_canonical_stage(result) in {"LENDER_READY", "ECONOMICS_READY"}:
        return {"code": "READY_TO_PREPARE_BID", "label": "Ready to prepare bid"}
    if terminal == LIKELY_PROFITABLE:
        if econ.get("financing_status") == FINANCING_BLOCKED:
            return {"code": "CALL_LENDER", "label": "Call lender"}
        return {"code": "REVIEW_DELIVERY_RISK", "label": "Review delivery risk"}
    if basket.get("basket_class") == BASKET_READY_QUOTE_DEPENDENT or quote_n > 0:
        return {"code": "REQUEST_SUPPLIER_QUOTE", "label": "Request supplier quote"}
    if terminal in {RESEARCH_EXHAUSTED, ECON_EXECUTION_BLOCKED}:
        if int(lines.get("UNRESOLVED_LINES") or 0) > 0:
            return {"code": "WAITING_FOR_PUBLIC_PRICE", "label": "Waiting for public price"}
        return {"code": "REJECT", "label": "Reject"}
    if terminal == UNPROFITABLE:
        return {"code": "REJECT", "label": "Reject"}
    if basket.get("basket_class") == BASKET_BLOCKED:
        return {"code": "RESEARCHING_AUTOMATICALLY", "label": "Researching automatically"}
    return {"code": "RESEARCHING_AUTOMATICALLY", "label": "Researching automatically"}


def what_can_hurt_us(result: dict[str, Any], corpus_row: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Plain-English risk section for near-ready/profitable opportunities."""
    risks: list[dict[str, str]] = []
    econ = result.get("economics") or {}
    basket = result.get("basket") or {}
    elig = (corpus_row or {}).get("eligibility_status") or "UNKNOWN"

    def add(category: str, detail: str) -> None:
        risks.append({"category": category, "detail": detail})

    if elig not in {"BID_ELIGIBLE", "CLEARED", "UNKNOWN"}:
        add("eligibility", f"Eligibility status is {elig} — may need registration or clearance before bid.")
    else:
        add("eligibility", "Confirm mandatory pre-bid requirements are cleared for this buyer.")

    add("bond", "Check if bid/performance bond is required; bonding capacity can block submission.")
    add("insurance", "Verify COI limits (GL, auto, umbrella) match solicitation.")
    add("OEM authorization", "Some OEMs require authorized reseller status for compliant supply.")
    add("delivery", "Confirm delivery date and destination are achievable with current stock lead times.")
    if econ.get("freight_status") != "PUBLIC_FREIGHT_CONFIRMED":
        add("freight", "Freight is estimated/quote-required — actual shipping can erase margin.")
    else:
        add("freight", "Confirm FOB terms and that seller quote matches destination.")
    add("liquidated damages", "Late delivery may trigger liquidated damages — price schedule risk.")
    add("payment timing", "Net terms vs supplier prepay can create a cash gap even if deal is profitable.")
    if float(econ.get("owner_cash_required") or 0) > 0:
        add("financing", f"Modeled owner/company cash required: ${econ.get('owner_cash_required')} — violates $0 prepay target until restructured.")
    else:
        add("financing", "Keep owner/company prepay at $0; confirm lender/supplier stack before bid.")
    add("required forms", "Ack amendments, reps/certs, and buyer-specific forms before submission.")
    add("amendments", "Re-check for new amendments close to deadline.")
    add("cybersecurity", "If CMMC/NIST/IT clauses apply, product-only resale may still need attestations.")
    add("warranty", "Government may require manufacturer warranty pass-through.")
    add("inspection", "Source inspection / acceptance criteria can delay payment.")
    add("country of origin", "TAA/BAA/Buy American may disqualify some public sellers.")
    add("submission method", "Portal vs email vs sealed bid — miss the method and the bid is void.")

    if basket.get("basket_class") == BASKET_READY_QUOTE_DEPENDENT:
        add("quote coverage", "Basket depends on supplier quotes — public price alone is incomplete.")
    if econ.get("economic_terminal") in {POSSIBLE_PROFIT, LIKELY_PROFITABLE, PROVEN_PROFITABLE}:
        add("margin sensitivity", "Small freight or price moves can flip thin-margin deals.")

    return risks


def owner_funnel_view(result: dict[str, Any]) -> dict[str, Any]:
    stage = derive_canonical_stage(result)
    action = owner_next_action(result)
    return {
        "canonical_stage": stage,
        "stages": [{"code": c, "label": l, "reached": CANONICAL_REACHED(stage, c)} for c, l in OWNER_STAGE_LABELS],
        "next_action": action,
        "what_can_hurt_us": what_can_hurt_us(result, result.get("corpus")),
    }


def CANONICAL_REACHED(current: str, target: str) -> bool:
    from basket_full_funnel_reconcile.models import CANONICAL_STAGES

    # Map owner labels to nearest canonical
    order = [c for c, _ in OWNER_STAGE_LABELS]
    if current in {"REJECTED", "BLOCKED", "QUOTE_RESERVE"}:
        return False
    try:
        return order.index(current) >= order.index(target) if current in order and target in order else (
            CANONICAL_STAGES.index(current) >= CANONICAL_STAGES.index(target)
            if current in CANONICAL_STAGES and target in CANONICAL_STAGES
            else False
        )
    except ValueError:
        return False

"""Funding Path Intelligence — deal funding foundation (deterministic, no paid APIs).

Extends existing funding_engine / commercial financing gates.
Hard constraints remain authoritative: no PG, no personal credit, $0 cash.
"""

from __future__ import annotations
from application_clock import now_utc, today_local

from datetime import datetime, timezone
from typing import Any

from funding_path_constants import (
    ASSIGN_UNKNOWN,
    CONF_MANUAL_REVIEW,
    CONF_MULTIPLE_PRELIMINARY,
    CONF_NEEDS_LENDER_VERIFICATION,
    CONF_NO_KNOWN_PATH,
    CONF_PRELIMINARY_MATCH,
    CONF_REJECTED,
    CONF_SECURED,
    CONF_THEORETICAL_ONLY,
    CONF_UNASSESSED,
    CONF_VERIFIED_PRE_BID,
    CONF_POST_AWARD_REQUIRED,
    EV_UNKNOWN,
    FUNDING_PATH_TYPES,
    HARD_POLICY,
    MATCH_CONDITIONAL,
    MATCH_MATCH,
    MATCH_NEEDS_VERIFICATION,
    MATCH_NOT_APPLICABLE,
    MATCH_REJECT,
    PATH_AR_FINANCING,
    PATH_DISTRIBUTOR_CREDIT,
    PATH_GOV_ADVANCE,
    PATH_GOV_INSTALLMENT,
    PATH_GOV_INTERIM,
    PATH_HYBRID_PO_PLUS_FACTORING,
    PATH_HYBRID_SUPPLIER_PLUS_PO,
    PATH_INVOICE_FACTORING,
    PATH_PO_FINANCING,
    PATH_SUPPLIER_TERMS,
    PG_CONDITIONAL,
    PG_NOT_REQUIRED,
    PG_REQUIRED,
    PG_UNKNOWN,
    READY_HARD_FAIL,
    READY_PLAUSIBLE,
    READY_POST_AWARD,
    READY_PRE_BID,
    READY_UNKNOWN,
    STAGE_POST_AWARD,
    STAGE_PRE_BID,
    STRUCT_NEEDS_VERIFICATION,
    STRUCT_NOT_APPLICABLE,
    STRUCT_POTENTIALLY,
    VERIFIED_EVIDENCE,
)
from funding_source_kb import (
    criteria_needing_reverification,
    empty_funding_source_profile,
    mark_stale_criteria,
)
from funding_underwriting import (
    CREDIT_NONE,
    DEFAULT_OPERATOR_PERSONAL_FICO,
    PATH_REJECT,
    PATH_WORKABLE_OPERATOR_APPROVAL,
    classify_path_underwriting,
    map_path_class_to_match_status,
)

# Re-export path types for callers
__all__ = [
    "build_funding_requirement",
    "match_funding_source",
    "assess_deal_funding_confidence",
    "evaluate_funding_readiness",
    "generate_hybrid_funding_paths",
    "apply_financing_cost_to_profit",
    "assess_government_contract_financing",
    "build_operator_call_sheet",
    "record_contact_outcome",
    "build_future_openai_decision_packet",
    "run_funding_path_workflow",
    "FUNDING_PATH_TYPES",
]


def _f(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _evidence_status(profile: dict[str, Any], key: str) -> str:
    ev = (profile.get("criteria_evidence") or {}).get(key)
    if isinstance(ev, dict):
        return (ev.get("verification_status") or EV_UNKNOWN).upper()
    return EV_UNKNOWN


def _is_verified(profile: dict[str, Any], key: str) -> bool:
    return _evidence_status(profile, key) in VERIFIED_EVIDENCE


def build_funding_requirement(
    *,
    opportunity: dict[str, Any] | None = None,
    economics: dict[str, Any] | None = None,
    deadline: dict[str, Any] | None = None,
    supplier: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deal-level FundingNeed — preserve UNKNOWN; do not invent numbers."""
    opp = opportunity or {}
    eco = economics or {}
    dl = deadline or {}
    sup = supplier or {}
    x = extras or {}

    bid = _f(eco.get("estimated_bid_value") or eco.get("proposed_bid") or opp.get("estimated_value"))
    supplier_cost = _f(eco.get("estimated_supplier_cost") or eco.get("supplier_cost"))
    freight = _f(eco.get("estimated_freight") or eco.get("freight"))
    other = _f(eco.get("estimated_other_performance_cost") or eco.get("other_cost"))

    total_cost = None
    known_costs = [c for c in (supplier_cost, freight, other) if c is not None]
    if known_costs and (supplier_cost is not None or freight is not None or other is not None):
        # Only sum when at least one component known; mark confidence accordingly
        total_cost = sum(known_costs)
        if any(c is None for c in (supplier_cost, freight, other)):
            total_cost_status = "PARTIAL"
        else:
            total_cost_status = "CALCULATED"
    else:
        total_cost_status = "UNKNOWN"

    gross_profit = None
    gross_margin = None
    if bid is not None and total_cost is not None:
        gross_profit = bid - total_cost
        if bid > 0:
            gross_margin = round(100.0 * gross_profit / bid, 2)

    cash_before_delivery = _f(x.get("cash_required_before_delivery"))
    cash_before_gov_pay = _f(x.get("cash_required_before_government_payment"))
    funding_amount = cash_before_delivery
    if funding_amount is None:
        funding_amount = cash_before_gov_pay
    if funding_amount is None and supplier_cost is not None:
        # Theoretical need = supplier cost when deposit/timing unknown
        funding_amount = supplier_cost

    customer = (
        opp.get("government_customer_type")
        or opp.get("buyer_type")
        or x.get("government_customer_type")
        or "UNKNOWN"
    )

    return {
        "government_customer_type": str(customer).lower() if customer != "UNKNOWN" else "UNKNOWN",
        "solicitation_number": opp.get("solicitation_number") or opp.get("notice_id"),
        "agency": opp.get("agency") or opp.get("buyer"),
        "product_category": opp.get("product_category") or opp.get("product_classification"),
        "product_description": opp.get("title") or opp.get("product_description"),
        "estimated_bid_value": bid,
        "estimated_supplier_cost": supplier_cost,
        "estimated_freight": freight,
        "estimated_other_performance_cost": other,
        "estimated_total_cost_to_perform": total_cost,
        "estimated_total_cost_status": total_cost_status,
        "estimated_gross_profit": gross_profit,
        "estimated_gross_margin_pct": gross_margin,
        "cash_required_before_delivery": cash_before_delivery,
        "cash_required_before_government_payment": cash_before_gov_pay,
        "supplier_deposit_required": sup.get("deposit_required"),
        "supplier_payment_timing": sup.get("payment_timing") or sup.get("payment_before_ship"),
        "supplier_terms_verified": bool(sup.get("terms_verified")),
        "expected_delivery_days": x.get("expected_delivery_days"),
        "expected_government_payment_days": x.get("expected_government_payment_days"),
        "estimated_cash_cycle_days": x.get("estimated_cash_cycle_days"),
        "direct_ship_possible": sup.get("direct_ship_available") or x.get("direct_ship_possible"),
        "supplier_can_ship_direct_to_government": (
            sup.get("direct_ship_to_government_available") or x.get("supplier_can_ship_direct_to_government")
        ),
        "award_required_before_financing": x.get("award_required_before_financing"),
        "purchase_order_expected": x.get("purchase_order_expected"),
        "contract_expected": x.get("contract_expected"),
        "assignment_of_claims_possible": x.get("assignment_of_claims_possible"),
        "assignment_status": x.get("assignment_status") or ASSIGN_UNKNOWN,
        "deadline_viability": dl.get("deadline_viability") or opp.get("deadline_viability"),
        "deadline_runway_days": dl.get("deadline_runway_days") or opp.get("deadline_runway_days"),
        "funding_needed": funding_amount is not None and funding_amount > 0,
        "funding_amount_required": funding_amount,
        "funding_confidence": "UNKNOWN" if funding_amount is None else "ESTIMATED",
        "funding_stage": x.get("funding_stage") or STAGE_PRE_BID,
        "award_received": bool(x.get("award_received")),
        "award_date": x.get("award_date"),
        "purchase_order_received": bool(x.get("purchase_order_received")),
        "executed_contract_received": bool(x.get("executed_contract_received")),
        "company_is_new_entity": x.get("company_is_new_entity"),
        "is_first_government_contract": x.get("is_first_government_contract"),
        "time_in_business_years": x.get("time_in_business_years"),
        "annual_revenue": x.get("annual_revenue"),
        "deal_qualified": x.get("deal_qualified", True),
        "hard_policy": dict(HARD_POLICY),
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "USAspending": 0,
        "paid": 0,
    }


def match_funding_source(
    requirement: dict[str, Any],
    source: dict[str, Any],
    *,
    deal_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Deterministic source match vs operating policy + verified source criteria.
    Personal guarantee alone does NOT hard-reject (operator risk decision).
    Personal credit: reject only when verified FICO floor / material dependence
    disqualifies operator. UNKNOWN → NEEDS_VERIFICATION.
    """
    req = requirement or {}
    src = mark_stale_criteria(source or empty_funding_source_profile())
    facts = deal_facts or {}
    hard_fail: list[str] = []
    unknown: list[str] = []
    positive: list[str] = []
    evidence_used: list[dict[str, Any]] = []
    next_actions: list[str] = []
    operator_notes: list[str] = []

    amount = _f(req.get("funding_amount_required") or req.get("estimated_supplier_cost"))
    margin = _f(req.get("estimated_gross_margin_pct"))
    customer = str(req.get("government_customer_type") or "UNKNOWN").lower()
    category = str(req.get("product_category") or "").lower()
    operator_fico = _f(
        facts.get("operator_personal_fico")
        or req.get("operator_personal_fico")
        or HARD_POLICY.get("operator_personal_fico_approx")
        or DEFAULT_OPERATOR_PERSONAL_FICO
    )

    def use_ev(key: str) -> None:
        ev = (src.get("criteria_evidence") or {}).get(key)
        if isinstance(ev, dict):
            evidence_used.append({"criterion": key, **{k: ev.get(k) for k in ("value", "verification_status", "verified_at")}})

    # --- Underwriting hierarchy (credit ≠ PG) ---
    underwriting = classify_path_underwriting(
        src,
        operator_personal_fico=operator_fico,
        uncovered_operator_cash=_f(facts.get("uncovered_operator_cash")),
        funding_timing_status=facts.get("funding_timing_status") or req.get("funding_timing_status"),
        actual_expected_profit=_f(facts.get("actual_expected_profit") or req.get("actual_expected_profit")),
        transaction_amount=amount,
    )
    for r in underwriting.get("reject_reasons") or []:
        hard_fail.append(r)
    for v in underwriting.get("verification_reasons") or []:
        unknown.append(v)
        next_actions.append(f"Verify: {v}")

    pg = src.get("personal_guarantee")
    pg_st = _evidence_status(src, "personal_guarantee")
    if pg == PG_REQUIRED and pg_st in VERIFIED_EVIDENCE:
        # NOT a hard_fail — operator risk review
        positive.append("pg_required_operator_review")
        operator_notes.append("Personal guarantee required — OPERATOR_PG_REVIEW_REQUIRED; not auto-reject")
        use_ev("personal_guarantee")
        next_actions.append("Operator risk review of personal guarantee (not automatic rejection)")
    elif pg in (PG_UNKNOWN, None, PG_CONDITIONAL) or pg_st not in VERIFIED_EVIDENCE:
        if pg == PG_CONDITIONAL or pg_st not in VERIFIED_EVIDENCE:
            unknown.append("personal_guarantee")
            next_actions.append("Verify personal guarantee requirement and type with underwriting")

    cash_req = src.get("borrower_cash_contribution_required")
    cash_pct = _f(src.get("minimum_cash_contribution_pct"))
    cash_st = _evidence_status(src, "borrower_cash_contribution_required")
    if cash_st in VERIFIED_EVIDENCE:
        use_ev("borrower_cash_contribution_required")
        if cash_req is True or (cash_pct is not None and cash_pct > 0):
            if _f(facts.get("uncovered_operator_cash")) is None or (_f(facts.get("uncovered_operator_cash")) or 0) > 0:
                if "verified_personal_cash_contribution_required_uncovered" not in hard_fail:
                    hard_fail.append("personal_cash_contribution_required")
    else:
        unknown.append("borrower_cash_contribution")
        next_actions.append("Verify borrower cash/equity contribution is $0")

    # Personal credit: only reject on verified disqualifying FICO / material dependence
    credit_checked = src.get("personal_credit_checked")
    fico = src.get("minimum_personal_fico")
    credit_st = _evidence_status(src, "personal_credit_checked")
    fico_st = _evidence_status(src, "minimum_personal_fico")
    if credit_st in VERIFIED_EVIDENCE:
        use_ev("personal_credit_checked")
        if credit_checked is False or credit_checked == CREDIT_NONE or credit_checked == "NONE":
            positive.append("no_personal_credit_pull")
        elif fico is not None and _f(fico) is not None and fico_st in VERIFIED_EVIDENCE:
            use_ev("minimum_personal_fico")
            if operator_fico is not None and operator_fico < _f(fico):
                if "verified_minimum_fico_above_operator_score" not in hard_fail:
                    hard_fail.append("personal_credit_dependency")
        elif credit_checked is True and fico is None:
            unknown.append("personal_credit_dependency")
            next_actions.append("Verify whether personal credit/FICO materially affects approval")
        else:
            unknown.append("personal_credit_dependency")
    else:
        unknown.append("personal_credit_dependency")
        next_actions.append("Verify whether personal credit/FICO is underwritten")

    biz_credit = src.get("business_credit_required")
    if _is_verified(src, "business_credit_required") and biz_credit is True:
        if facts.get("business_credit_available") is not True:
            hard_fail.append("established_business_credit_required")
            use_ev("business_credit_required")

    mn = _f(src.get("minimum_transaction") or src.get("minimum_funding_amount"))
    mx = _f(src.get("maximum_transaction") or src.get("maximum_funding_amount"))
    if amount is not None and mn is not None and _is_verified(src, "minimum_transaction"):
        use_ev("minimum_transaction")
        if amount < mn:
            hard_fail.append("below_minimum_transaction")
    elif mn is None or not _is_verified(src, "minimum_transaction"):
        unknown.append("minimum_transaction")

    if amount is not None and mx is not None and _is_verified(src, "maximum_transaction"):
        use_ev("maximum_transaction")
        if amount > mx:
            hard_fail.append("above_maximum_transaction")
    elif mx is None or not _is_verified(src, "maximum_transaction"):
        unknown.append("maximum_transaction")

    min_m = _f(src.get("minimum_margin_pct"))
    if margin is not None and min_m is not None and _is_verified(src, "minimum_margin_pct"):
        use_ev("minimum_margin_pct")
        if margin < min_m:
            hard_fail.append("below_minimum_margin")
    elif min_m is None or not _is_verified(src, "minimum_margin_pct"):
        unknown.append("minimum_margin_pct")

    supported = src.get("government_customer_types_supported")
    if isinstance(supported, list) and _is_verified(src, "government_customer_types_supported"):
        use_ev("government_customer_types_supported")
        if customer != "unknown" and customer not in [str(s).lower() for s in supported]:
            hard_fail.append("unsupported_government_customer")
        elif customer != "unknown":
            positive.append("customer_type_supported")
    else:
        unknown.append("government_customer_types_supported")

    cats = src.get("product_categories_supported")
    excl = src.get("excluded_categories") or []
    if isinstance(excl, list) and category and any(category in str(e).lower() for e in excl):
        if _is_verified(src, "excluded_categories") or excl:
            hard_fail.append("excluded_product_category")
    if isinstance(cats, list) and cats and category:
        if _is_verified(src, "product_categories_supported"):
            use_ev("product_categories_supported")
            if not any(category in str(c).lower() or str(c).lower() in category for c in cats):
                if src.get("product_categories_exclusive") is True:
                    hard_fail.append("unsupported_product_category")
            else:
                positive.append("product_category_supported")

    if req.get("is_first_government_contract") is True or facts.get("is_first_government_contract") is True:
        fgc = src.get("first_government_contract_allowed")
        if fgc is False and _is_verified(src, "first_government_contract_allowed"):
            hard_fail.append("first_government_contract_prohibited")
            use_ev("first_government_contract_allowed")
        elif fgc is None or not _is_verified(src, "first_government_contract_allowed"):
            unknown.append("first_government_contract_allowed")

    if req.get("company_is_new_entity") is True or facts.get("company_is_new_entity") is True:
        ne = src.get("new_entity_allowed") if src.get("new_entity_allowed") is not None else src.get("startup_allowed")
        key = "new_entity_allowed" if "new_entity_allowed" in (src.get("criteria_evidence") or {}) else "startup_allowed"
        if ne is False and _is_verified(src, key):
            hard_fail.append("new_entity_prohibited")
            use_ev(key)
        elif ne is None or not _is_verified(src, key):
            unknown.append("new_entity_allowed")

    tib = _f(src.get("minimum_time_in_business"))
    company_tib = _f(req.get("time_in_business_years") or facts.get("time_in_business_years"))
    if tib is not None and _is_verified(src, "minimum_time_in_business"):
        if company_tib is not None and company_tib < tib:
            hard_fail.append("minimum_time_in_business_not_met")
            use_ev("minimum_time_in_business")

    min_rev = _f(src.get("minimum_annual_revenue") or src.get("minimum_historical_revenue"))
    company_rev = _f(req.get("annual_revenue") or facts.get("annual_revenue"))
    if min_rev is not None and _is_verified(src, "minimum_annual_revenue"):
        if company_rev is not None and company_rev < min_rev:
            hard_fail.append("minimum_revenue_not_met")
            use_ev("minimum_annual_revenue")

    fund_days = _f(src.get("typical_funding_days"))
    runway = _f(req.get("deadline_runway_days"))
    if fund_days is not None and runway is not None and _is_verified(src, "typical_funding_days"):
        if req.get("funding_stage") == STAGE_POST_AWARD and fund_days > runway:
            hard_fail.append("funding_cannot_occur_soon_enough")

    if amount is not None and amount > 0:
        if src.get("direct_supplier_payment_supported") is True and _is_verified(src, "direct_supplier_payment_supported"):
            positive.append("direct_supplier_payment")
            use_ev("direct_supplier_payment_supported")

    # Prefer underwriting path_class; hard_fail list still drives REJECT
    if hard_fail or underwriting.get("path_class") == PATH_REJECT:
        result = MATCH_REJECT
    elif underwriting.get("path_class") == PATH_WORKABLE_OPERATOR_APPROVAL and not hard_fail:
        # PG-required workable path is CONDITIONAL, not REJECT
        result = MATCH_CONDITIONAL
        positive.append("workable_with_operator_pg_approval")
    elif unknown:
        result = MATCH_NEEDS_VERIFICATION
    elif pg == PG_NOT_REQUIRED and cash_req is False and (
        credit_checked is False or credit_checked == CREDIT_NONE or credit_checked == "NONE"
    ):
        hard_ok = (
            _is_verified(src, "personal_guarantee")
            and _is_verified(src, "borrower_cash_contribution_required")
            and _is_verified(src, "personal_credit_checked")
        )
        if hard_ok:
            positive.append("hard_constraints_compatible")
            result = MATCH_MATCH if not unknown else MATCH_CONDITIONAL
        else:
            result = MATCH_NEEDS_VERIFICATION
    else:
        result = map_path_class_to_match_status(underwriting.get("path_class") or PATH_REJECT)
        if result == MATCH_REJECT and not hard_fail:
            result = MATCH_NEEDS_VERIFICATION if unknown else MATCH_CONDITIONAL

    score = 50
    score -= 40 * len(hard_fail)
    score -= 5 * len(unknown)
    score += 10 * len(positive)
    if result == MATCH_MATCH:
        score = max(score, 80)
    if underwriting.get("path_class") == PATH_WORKABLE_OPERATOR_APPROVAL:
        score = max(score, 55)

    return {
        "match_status": result,
        "match_score": score,
        "hard_fail_reasons": hard_fail,
        "unknown_criteria": sorted(set(unknown)),
        "positive_match_reasons": positive,
        "required_next_actions": next_actions,
        "operator_notes": operator_notes,
        "evidence_used": evidence_used,
        "source_name": src.get("source_name"),
        "funding_secured": False,
        "underwriting": underwriting,
        "path_class": underwriting.get("path_class"),
        "operator_pg_review_required": underwriting.get("operator_pg_review_required"),
        "hard_policy": dict(HARD_POLICY),
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "USAspending": 0,
        "paid": 0,
        "financier_outreach": 0,
    }


def assess_deal_funding_confidence(
    *,
    requirement: dict[str, Any] | None = None,
    match_results: list[dict[str, Any]] | None = None,
    award_confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Conservative deal-level funding confidence. PRELIMINARY ≠ SECURED."""
    req = requirement or {}
    matches = match_results or []
    stage = req.get("funding_stage") or STAGE_PRE_BID

    if req.get("deal_qualified") is False:
        return {
            "funding_confidence": CONF_REJECTED,
            "reason": "fundamental_deal_qualification_failed",
            "note": "Funding cannot override failed deal qualification",
            "funding_secured": False,
            "LIVE_API_REQUESTS": 0,
        }

    # Deadline TOO_LATE does not become attractive because funding exists
    if str(req.get("deadline_viability") or "").upper() == "TOO_LATE":
        return {
            "funding_confidence": CONF_MANUAL_REVIEW,
            "reason": "deadline_too_late",
            "note": "TOO_LATE remains non-actionable regardless of funding matches",
            "funding_secured": False,
            "LIVE_API_REQUESTS": 0,
        }

    rejects = [m for m in matches if m.get("match_status") == MATCH_REJECT]
    needs = [m for m in matches if m.get("match_status") == MATCH_NEEDS_VERIFICATION]
    ok = [m for m in matches if m.get("match_status") in {MATCH_MATCH, MATCH_CONDITIONAL}]

    if stage == STAGE_POST_AWARD:
        ac = award_confirmation or {}
        if ac.get("status") == "CONFIRMED":
            # Still not auto FUNDING_SECURED without explicit secured flag
            if ac.get("funding_disbursed") is True:
                return {
                    "funding_confidence": CONF_SECURED,
                    "funding_secured": True,
                    "stage": STAGE_POST_AWARD,
                    "LIVE_API_REQUESTS": 0,
                }
            return {
                "funding_confidence": CONF_CONDITIONALLY_APPROVED
                if ac.get("provider_approval_verified")
                else CONF_POST_AWARD_REQUIRED,
                "funding_secured": False,
                "stage": STAGE_POST_AWARD,
                "LIVE_API_REQUESTS": 0,
            }
        return {
            "funding_confidence": CONF_POST_AWARD_REQUIRED,
            "funding_secured": False,
            "stage": STAGE_POST_AWARD,
            "required_action": "transaction_specific_funding_verification_application",
            "LIVE_API_REQUESTS": 0,
        }

    if not matches:
        return {
            "funding_confidence": CONF_UNASSESSED if not req.get("funding_needed") else CONF_NO_KNOWN_PATH,
            "funding_secured": False,
            "stage": STAGE_PRE_BID,
            "LIVE_API_REQUESTS": 0,
        }

    if ok and not any(m.get("match_status") == MATCH_MATCH for m in ok):
        conf = CONF_PRELIMINARY_MATCH if len(ok) == 1 else CONF_MULTIPLE_PRELIMINARY
        return {
            "funding_confidence": conf,
            "funding_secured": False,
            "note": "Preliminary match is not funding secured",
            "match_count": len(ok),
            "stage": STAGE_PRE_BID,
            "LIVE_API_REQUESTS": 0,
        }

    if any(m.get("match_status") == MATCH_MATCH for m in ok):
        # Verified criteria match pre-bid — still not transaction funded
        return {
            "funding_confidence": CONF_VERIFIED_PRE_BID,
            "funding_secured": False,
            "note": "Verified pre-bid path ≠ post-award approval or funding secured",
            "match_count": len([m for m in ok if m.get("match_status") == MATCH_MATCH]),
            "stage": STAGE_PRE_BID,
            "LIVE_API_REQUESTS": 0,
        }

    if needs and not ok:
        return {
            "funding_confidence": CONF_NEEDS_LENDER_VERIFICATION,
            "funding_secured": False,
            "stage": STAGE_PRE_BID,
            "LIVE_API_REQUESTS": 0,
        }

    if rejects and not ok and not needs:
        return {
            "funding_confidence": CONF_REJECTED,
            "funding_secured": False,
            "stage": STAGE_PRE_BID,
            "LIVE_API_REQUESTS": 0,
        }

    if matches:
        return {
            "funding_confidence": CONF_THEORETICAL_ONLY,
            "funding_secured": False,
            "stage": STAGE_PRE_BID,
            "LIVE_API_REQUESTS": 0,
        }

    return {"funding_confidence": CONF_UNASSESSED, "funding_secured": False, "LIVE_API_REQUESTS": 0}


def evaluate_funding_readiness(
    *,
    requirement: dict[str, Any] | None = None,
    confidence: dict[str, Any] | None = None,
    match_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Preliminary funding readiness — does not invent DEAL_READY."""
    req = requirement or {}
    conf = confidence or assess_deal_funding_confidence(
        requirement=req, match_results=match_results
    )
    status = conf.get("funding_confidence")

    if req.get("deal_qualified") is False:
        return {
            "readiness": READY_HARD_FAIL,
            "reason": "deal_not_qualified",
            "funding_confidence": status,
            "LIVE_API_REQUESTS": 0,
        }

    if any(
        m.get("match_status") == MATCH_REJECT and m.get("hard_fail_reasons")
        for m in (match_results or [])
    ) and not any(
        m.get("match_status") in {MATCH_MATCH, MATCH_CONDITIONAL, MATCH_NEEDS_VERIFICATION}
        for m in (match_results or [])
    ):
        # All known paths hard-failed
        if match_results and all(m.get("match_status") == MATCH_REJECT for m in match_results):
            return {
                "readiness": READY_HARD_FAIL,
                "reason": "all_known_paths_rejected",
                "funding_confidence": CONF_REJECTED,
                "LIVE_API_REQUESTS": 0,
            }

    if req.get("funding_stage") == STAGE_POST_AWARD or req.get("award_received"):
        return {
            "readiness": READY_POST_AWARD,
            "reason": "post_award_transaction_verification_required",
            "funding_confidence": status,
            "LIVE_API_REQUESTS": 0,
        }

    if status == CONF_VERIFIED_PRE_BID:
        return {
            "readiness": READY_PRE_BID,
            "reason": "verified_compatible_path_pre_bid",
            "funding_confidence": status,
            "note": "Not DEAL_READY alone; commercial gates still apply",
            "LIVE_API_REQUESTS": 0,
        }

    if status in {CONF_PRELIMINARY_MATCH, CONF_MULTIPLE_PRELIMINARY, CONF_NEEDS_LENDER_VERIFICATION}:
        return {
            "readiness": READY_PLAUSIBLE,
            "reason": "path_plausible_needs_verification",
            "funding_confidence": status,
            "LIVE_API_REQUESTS": 0,
        }

    if status in {CONF_UNASSESSED, CONF_NO_KNOWN_PATH, CONF_THEORETICAL_ONLY}:
        return {
            "readiness": READY_UNKNOWN,
            "reason": "funding_path_unknown_or_unassessed",
            "funding_confidence": status,
            "note": "Do not reject excellent opportunities solely for incomplete lender research",
            "LIVE_API_REQUESTS": 0,
        }

    if status == CONF_REJECTED:
        return {
            "readiness": READY_HARD_FAIL,
            "reason": "funding_rejected",
            "funding_confidence": status,
            "LIVE_API_REQUESTS": 0,
        }

    return {
        "readiness": READY_UNKNOWN,
        "funding_confidence": status,
        "LIVE_API_REQUESTS": 0,
    }


def generate_hybrid_funding_paths(
    requirement: dict[str, Any],
    *,
    components_sets: list[list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """
    Deterministic creative/hybrid structures.
    Personal cash >0 FAILS hard constraints.
    Personal guarantee alone → OPERATOR_PG_REVIEW (not auto hard-fail).
    """
    req = requirement or {}
    need = _f(req.get("funding_amount_required")) or 0.0
    presets = components_sets or [
        [{"type": PATH_SUPPLIER_TERMS, "covers": need, "pg": False, "cash": 0}],
        [{"type": PATH_PO_FINANCING, "covers": need, "pg": False, "cash": 0}],
        [
            {"type": PATH_SUPPLIER_TERMS, "covers": need * 0.2 if need else 0, "label": "deposit_deferral", "pg": False, "cash": 0},
            {"type": PATH_PO_FINANCING, "covers": need * 0.8 if need else 0, "label": "po_balance", "pg": False, "cash": 0},
        ],
        [
            {"type": PATH_PO_FINANCING, "covers": need * 0.9 if need else 0, "pg": False, "cash": 0},
            {"type": PATH_INVOICE_FACTORING, "covers": need * 0.1 if need else 0, "pg": False, "cash": 0},
        ],
        [
            {"type": PATH_DISTRIBUTOR_CREDIT, "covers": need * 0.5 if need else 0, "pg": False, "cash": 0},
            {"type": PATH_AR_FINANCING, "covers": need * 0.5 if need else 0, "pg": False, "cash": 0},
        ],
        [
            {"type": PATH_GOV_INTERIM, "covers": need * 0.5 if need else None, "pg": False, "cash": 0, "availability": STRUCT_NEEDS_VERIFICATION},
            {"type": PATH_PO_FINANCING, "covers": need * 0.5 if need else 0, "pg": False, "cash": 0},
        ],
    ]

    out: list[dict[str, Any]] = []
    for comps in presets:
        borrower_cash = 0.0
        pg_status = PG_NOT_REQUIRED
        unknowns: list[str] = []
        covered = 0.0
        seq: list[str] = []
        fail_reasons: list[str] = []
        for c in comps:
            seq.append(c.get("label") or c.get("type") or "component")
            cov = _f(c.get("covers"))
            if cov is None:
                unknowns.append(f"coverage_unknown:{c.get('type')}")
            else:
                covered += cov
            cash = _f(c.get("cash")) or 0.0
            borrower_cash += cash
            if c.get("pg") is True or c.get("personal_guarantee") == PG_REQUIRED:
                pg_status = PG_REQUIRED
            if c.get("availability") == STRUCT_NEEDS_VERIFICATION:
                unknowns.append(f"availability:{c.get('type')}")
            if c.get("type") in {
                PATH_GOV_ADVANCE,
                PATH_GOV_INTERIM,
                PATH_GOV_INSTALLMENT,
            }:
                unknowns.append("government_financing_must_be_verified_in_solicitation")

        if borrower_cash > 0:
            fail_reasons.append("personal_cash_contribution_required")

        gap = None if need is None else max(0.0, need - covered)
        operator_pg_review = pg_status == PG_REQUIRED
        if fail_reasons:
            status = "FAIL_HARD_CONSTRAINT"
        elif operator_pg_review:
            status = "OPERATOR_PG_REVIEW_REQUIRED"
        elif unknowns or gap is None:
            status = "NEEDS_VERIFICATION"
        elif gap == 0:
            status = "COVERS_NEED"
        else:
            status = "PARTIAL_COVERAGE"
        path_type = comps[0].get("type") if len(comps) == 1 else (
            PATH_HYBRID_SUPPLIER_PLUS_PO if any(c.get("type") == PATH_SUPPLIER_TERMS for c in comps) and any(
                c.get("type") == PATH_PO_FINANCING for c in comps
            ) else PATH_HYBRID_PO_PLUS_FACTORING
        )

        out.append(
            {
                "path_type": path_type,
                "components": comps,
                "cash_flow_sequence": seq,
                "estimated_borrower_cash_required": borrower_cash,
                "personal_guarantee_status": pg_status,
                "personal_credit_dependency": False,
                "operator_pg_review_required": operator_pg_review,
                "autonomous_pg_acceptance": False,
                "unknowns": unknowns,
                "verification_actions": [
                    f"Verify component availability: {c.get('type')}" for c in comps
                ],
                "estimated_coverage_of_funding_need": covered,
                "remaining_funding_gap": gap,
                "funding_need": need,
                "status": status,
                "hard_fail_reasons": fail_reasons,
                "hard_policy_pass": not fail_reasons,
                "LIVE_API_REQUESTS": 0,
                "OpenAI": 0,
            }
        )
    return out


def apply_financing_cost_to_profit(
    *,
    profit_before_financing: float | None,
    financing_fee_estimate: float | None = None,
    financing_interest_estimate: float | None = None,
    factor_fee_estimate: float | None = None,
    other_funding_cost: float | None = None,
    bid_value: float | None = None,
    minimum_profit: float | None = None,
) -> dict[str, Any]:
    """Funding cost reduces estimated actual profit. UNKNOWN fees stay explicit."""
    min_p = minimum_profit if minimum_profit is not None else HARD_POLICY["minimum_actual_profit_usd"]
    parts = {
        "financing_fee_estimate": financing_fee_estimate,
        "financing_interest_estimate": financing_interest_estimate,
        "factor_fee_estimate": factor_fee_estimate,
        "other_funding_cost": other_funding_cost,
    }
    known = [v for v in parts.values() if v is not None]
    unknown_fees = [k for k, v in parts.items() if v is None]

    if not known and unknown_fees == list(parts.keys()):
        total_funding_cost = None
        cost_confidence = "UNKNOWN"
    elif unknown_fees:
        total_funding_cost = sum(known)
        cost_confidence = "PARTIAL"
    else:
        total_funding_cost = sum(known)
        cost_confidence = "ESTIMATED"

    profit_after = None
    margin_after = None
    meets_target = None
    if profit_before_financing is not None and total_funding_cost is not None:
        profit_after = profit_before_financing - total_funding_cost
        if bid_value and bid_value > 0:
            margin_after = round(100.0 * profit_after / bid_value, 2)
        meets_target = profit_after >= float(min_p)
    elif profit_before_financing is not None and total_funding_cost is None:
        meets_target = None  # cannot claim pass/fail

    return {
        **parts,
        "estimated_total_funding_cost": total_funding_cost,
        "profit_before_financing": profit_before_financing,
        "profit_after_financing": profit_after,
        "margin_after_financing": margin_after,
        "funding_cost_confidence": cost_confidence,
        "unknown_funding_cost_fields": unknown_fees,
        "minimum_actual_profit_target": min_p,
        "meets_minimum_profit_target": meets_target,
        "fails_minimum_profit_due_to_financing": (
            meets_target is False
            if profit_before_financing is not None and total_funding_cost is not None
            else None
        ),
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def assess_government_contract_financing(
    *,
    solicitation_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """FAR Part 32 may exist; specific solicitation financing is NOT assumed available."""
    ev = solicitation_evidence or {}
    present = ev.get("government_financing_present_in_solicitation")
    if present is True and ev.get("government_financing_verified") is True:
        return {
            "government_financing_present_in_solicitation": True,
            "government_financing_type": ev.get("government_financing_type"),
            "government_financing_clause": ev.get("government_financing_clause"),
            "government_financing_terms": ev.get("government_financing_terms"),
            "government_financing_verified": True,
            "availability": STRUCT_POTENTIALLY,
            "contracting_officer_verification_needed": bool(
                ev.get("contracting_officer_verification_needed", False)
            ),
            "LIVE_API_REQUESTS": 0,
        }
    if present is False and ev.get("government_financing_verified") is True:
        return {
            "government_financing_present_in_solicitation": False,
            "government_financing_verified": True,
            "availability": STRUCT_NOT_APPLICABLE,
            "contracting_officer_verification_needed": False,
            "LIVE_API_REQUESTS": 0,
        }
    return {
        "government_financing_present_in_solicitation": present,
        "government_financing_type": ev.get("government_financing_type"),
        "government_financing_clause": ev.get("government_financing_clause"),
        "government_financing_terms": ev.get("government_financing_terms"),
        "government_financing_verified": False,
        "availability": STRUCT_NEEDS_VERIFICATION,
        "contracting_officer_verification_needed": True,
        "co_question": (
            "Does this solicitation/contract authorize commercial advance, interim, "
            "installment, or other FAR Part 32 contract financing for the contractor?"
        ),
        "note": "FAR Part 32 existence does not make financing available on this deal",
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


# --- Call sheet / outcomes / AI schemas / workflow ---

_QUESTION_CATALOG: list[tuple[str, str]] = [
    ("finance_gov_po", "Do you finance product-resale government purchase orders/contracts?"),
    ("customer_types", "Federal, state and local?"),
    ("first_gov_contract", "Will you finance a company's first government contract?"),
    ("new_entity", "Will you finance a newly formed operating company?"),
    ("min_tib", "Do you require a minimum time in business?"),
    ("historical_revenue", "Do you require historical revenue?"),
    ("personal_credit", "Do you pull or underwrite personal credit?"),
    ("min_fico", "Is there a minimum personal FICO?"),
    ("personal_guarantee", "Is a personal guarantee required?"),
    ("cash_contribution", "Is any borrower cash/equity contribution required?"),
    ("min_margin", "What minimum gross margin do you require?"),
    ("min_max_size", "What are your minimum and maximum transaction/funding sizes?"),
    ("pay_supplier_direct", "Can you pay the supplier directly?"),
    ("direct_ship", "Can the supplier direct-ship to the government customer?"),
    ("assignment", "Do you require assignment of claims?"),
    ("lockbox", "Do you require a lockbox or controlled account?"),
    ("ucc", "Do you require a UCC filing?"),
    ("award_before", "Must an award/PO already exist before underwriting?"),
    ("pre_bid_screen", "Can you pre-screen a solicitation before award?"),
    ("approve_speed", "How quickly can you approve after award?"),
    ("fund_speed", "How quickly can you fund the supplier?"),
    ("documents", "What documentation do you require?"),
    ("fees", "What fees/rates apply?"),
    ("fees_when_funds", "Are fees charged only when a transaction funds?"),
    ("reject_causes", "What would cause you to reject THIS transaction?"),
]

_CRITERION_TO_QUESTIONS = {
    "personal_guarantee": ["personal_guarantee"],
    "borrower_cash_contribution": ["cash_contribution"],
    "borrower_cash_contribution_required": ["cash_contribution"],
    "personal_credit_dependency": ["personal_credit", "min_fico"],
    "personal_credit_checked": ["personal_credit", "min_fico"],
    "minimum_transaction": ["min_max_size"],
    "maximum_transaction": ["min_max_size"],
    "minimum_margin_pct": ["min_margin"],
    "first_government_contract_allowed": ["first_gov_contract"],
    "new_entity_allowed": ["new_entity"],
    "startup_allowed": ["new_entity"],
    "government_customer_types_supported": ["customer_types"],
    "direct_supplier_payment_supported": ["pay_supplier_direct"],
    "direct_ship_supported": ["direct_ship"],
}


def build_operator_call_sheet(
    *,
    requirement: dict[str, Any],
    source: dict[str, Any],
    match_result: dict[str, Any] | None = None,
    call_priority: int = 1,
) -> dict[str, Any]:
    """Deal-specific call guidance without OpenAI."""
    req = requirement or {}
    src = source or {}
    match = match_result or match_funding_source(req, src)
    unknowns = list(match.get("unknown_criteria") or [])
    # Skip questions for criteria still verified & current
    skip_keys: set[str] = set()
    for name, ev in (src.get("criteria_evidence") or {}).items():
        if isinstance(ev, dict) and ev.get("verification_status") in VERIFIED_EVIDENCE:
            skip_keys.update(_CRITERION_TO_QUESTIONS.get(name, []))
            # map criterion names to question keys loosely
            if name == "personal_guarantee":
                skip_keys.add("personal_guarantee")
            if name == "borrower_cash_contribution_required":
                skip_keys.add("cash_contribution")
            if name == "personal_credit_checked":
                skip_keys.update({"personal_credit", "min_fico"})

    # Stale → re-ask
    for name in criteria_needing_reverification(src):
        for qk in _CRITERION_TO_QUESTIONS.get(name, []):
            skip_keys.discard(qk)

    ask_keys: set[str] = {"finance_gov_po"}  # always confirm product-resale gov PO once if not verified
    if "finance_gov_po" in skip_keys:
        ask_keys.discard("finance_gov_po")

    for u in unknowns:
        for qk in _CRITERION_TO_QUESTIONS.get(u, []):
            if qk not in skip_keys:
                ask_keys.add(qk)
    # Deal-specific always useful
    if req.get("is_first_government_contract") and "first_gov_contract" not in skip_keys:
        ask_keys.add("first_gov_contract")
    if req.get("company_is_new_entity") and "new_entity" not in skip_keys:
        ask_keys.add("new_entity")

    questions = []
    for key, text in _QUESTION_CATALOG:
        if key in ask_keys and key not in skip_keys:
            questions.append({"key": key, "question": text})

    bid = req.get("estimated_bid_value")
    cost = req.get("estimated_supplier_cost")
    bid_s = f"${bid:,.0f}" if isinstance(bid, (int, float)) else "an amount still being estimated"
    cost_s = f"${cost:,.0f}" if isinstance(cost, (int, float)) else "still being confirmed"
    direct = req.get("supplier_can_ship_direct_to_government")
    direct_clause = (
        "The supplier can direct-ship to the government customer. "
        if direct is True
        else ("Direct-ship capability is still being confirmed. " if direct is None else "")
    )
    opening = (
        f"I have a government product-resale opportunity for approximately {bid_s}. "
        f"Estimated supplier cost is approximately {cost_s}. "
        f"{direct_clause}"
        "I am looking for transaction-based purchase-order or government-contract financing "
        "where underwriting can be based primarily on the award, government customer, supplier "
        "and transaction, without requiring a personal guarantee, personal-credit qualification, "
        "or borrower cash contribution."
    )

    known = []
    for name, ev in (src.get("criteria_evidence") or {}).items():
        if isinstance(ev, dict) and ev.get("is_verified"):
            known.append(f"{name}={ev.get('value')} ({ev.get('verification_status')})")

    return {
        "call_priority": call_priority,
        "source": src.get("source_name"),
        "phone": src.get("government_contract_phone") or src.get("general_phone"),
        "who_to_ask_for": src.get("preferred_contact_name"),
        "department_title": src.get("preferred_department") or src.get("preferred_contact_title"),
        "why_this_source_may_match": match.get("positive_match_reasons") or ["preliminary_screening"],
        "what_we_already_know": known,
        "what_we_still_need_to_verify": unknowns,
        "match_status": match.get("match_status"),
        "opening": opening,
        "questions": questions,
        "hard_requirements": dict(HARD_POLICY),
        "skipped_verified_questions": sorted(skip_keys),
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def record_contact_outcome(
    *,
    source_name: str,
    raw_operator_notes: str,
    contact_date: str | None = None,
    contact_method: str = "PHONE",
    contact_name: str | None = None,
    contact_title: str | None = None,
    structured_criteria: dict[str, Any] | None = None,
    ai_proposed_updates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Store verbatim notes + optional structured facts.
    AI proposals do NOT auto-become VERIFIED.
    """
    proposals = []
    for p in ai_proposed_updates or []:
        proposals.append(
            {
                **p,
                "requires_confirmation": True,
                "auto_verified": False,
                "verification_status": "PROPOSED_AI_UNCONFIRMED",
            }
        )
    return {
        "contact_date": contact_date or today_local().isoformat(),
        "contact_method": contact_method,
        "contact_name": contact_name,
        "contact_title": contact_title,
        "source": source_name,
        "raw_operator_notes": raw_operator_notes,  # retained verbatim
        "structured_criteria": structured_criteria or {},
        "ai_proposed_updates": proposals,
        "ai_proposals_auto_verified": False,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def build_future_openai_decision_packet(
    *,
    requirement: dict[str, Any],
    sources: list[dict[str, Any]] | None = None,
    match_results: list[dict[str, Any]] | None = None,
    contact_history: list[dict[str, Any]] | None = None,
    new_operator_notes: str | None = None,
    deadline: dict[str, Any] | None = None,
    economics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Schemas/interfaces only — does not call OpenAI."""
    input_schema = {
        "DEAL_FACTS": requirement,
        "HARD_CONSTRAINTS": dict(HARD_POLICY),
        "FUNDING_REQUIREMENT": requirement,
        "KNOWN_FUNDING_SOURCES": sources or [],
        "VERIFIED_CRITERIA": [
            {
                "source": s.get("source_name"),
                "criteria": {
                    k: v
                    for k, v in (s.get("criteria_evidence") or {}).items()
                    if isinstance(v, dict) and v.get("is_verified")
                },
            }
            for s in (sources or [])
        ],
        "UNKNOWN_CRITERIA": [
            u for m in (match_results or []) for u in (m.get("unknown_criteria") or [])
        ],
        "CONTACT_HISTORY": contact_history or [],
        "NEW_OPERATOR_NOTES": new_operator_notes,
        "AVAILABLE_FUNDING_PATH_TYPES": list(FUNDING_PATH_TYPES),
        "DEADLINE_RUNWAY": deadline or {
            "deadline_viability": requirement.get("deadline_viability"),
            "deadline_runway_days": requirement.get("deadline_runway_days"),
        },
        "DEAL_ECONOMICS": economics or {
            "estimated_gross_profit": requirement.get("estimated_gross_profit"),
            "estimated_gross_margin_pct": requirement.get("estimated_gross_margin_pct"),
        },
    }
    output_schema = {
        "recommended_next_action": None,
        "funding_source_assessment": [],
        "new_possible_structures": [],
        "questions_to_ask": [],
        "facts_to_confirm": [],
        "conflicts_detected": [],
        "reasoning_summary": None,
        "warnings": [
            "AI must not waive hard constraints",
            "AI must not convert UNKNOWN to VERIFIED",
            "AI must not claim approval without evidence",
            "AI must not claim funding secured from marketing",
            "AI must not overwrite verified lender facts silently",
        ],
    }
    return {
        "interface": "FUTURE_OPENAI_FUNDING_DECISION_SUPPORT",
        "openai_requests": 0,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "rules": {
            "hard_constraints_authoritative": True,
            "unknown_not_verified": True,
            "marketing_not_approval": True,
            "preliminary_not_secured": True,
        },
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "USAspending": 0,
        "paid": 0,
    }


def run_funding_path_workflow(
    *,
    opportunity: dict[str, Any] | None = None,
    economics: dict[str, Any] | None = None,
    deadline: dict[str, Any] | None = None,
    sources: list[dict[str, Any]] | None = None,
    supplier: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
    deal_qualified: bool = True,
) -> dict[str, Any]:
    """
    Operator workflow skeleton:
    deadline → economics → cash need → match → call sheet → readiness.
    Does not contact lenders or call APIs.
    """
    # Deadline gate for research
    try:
        from discovery.deadline_viability import may_trigger_paid_research

        viability = (deadline or opportunity or {}).get("deadline_viability")
        research_gate = may_trigger_paid_research(
            viability, deal_qualified=deal_qualified, research_kind="financing"
        )
    except Exception:
        research_gate = {"allowed": True, "reason": "deadline_module_unavailable"}

    x = dict(extras or {})
    x["deal_qualified"] = deal_qualified
    req = build_funding_requirement(
        opportunity=opportunity,
        economics=economics,
        deadline=deadline or opportunity,
        supplier=supplier,
        extras=x,
    )

    matches = [match_funding_source(req, s) for s in (sources or [])]
    conf = assess_deal_funding_confidence(requirement=req, match_results=matches)
    ready = evaluate_funding_readiness(requirement=req, confidence=conf, match_results=matches)
    hybrids = generate_hybrid_funding_paths(req)
    gov = assess_government_contract_financing()
    profit = apply_financing_cost_to_profit(
        profit_before_financing=_f(req.get("estimated_gross_profit")),
        financing_fee_estimate=_f((economics or {}).get("financing_fee_estimate")),
        bid_value=_f(req.get("estimated_bid_value")),
    )

    sheets = []
    priority = 1
    for src, m in zip(sources or [], matches):
        if m.get("match_status") == MATCH_REJECT:
            continue
        sheets.append(build_operator_call_sheet(requirement=req, source=src, match_result=m, call_priority=priority))
        priority += 1

    return {
        "requirement": req,
        "matches": matches,
        "funding_confidence": conf,
        "readiness": ready,
        "hybrid_paths": hybrids,
        "government_financing": gov,
        "economics_after_financing": profit,
        "call_sheets": sheets,
        "research_spend_gate": research_gate,
        "workflow": [
            "OPPORTUNITY_DISCOVERED",
            "DEADLINE_VIABLE?",
            "PRODUCT_ECONOMICS_PRELIMINARY?",
            "CASH_REQUIRED_TO_PERFORM?",
            "KNOWN_FUNDING_PATH_MATCH",
            "HARD_FAIL_OR_VERIFY_OR_CALL",
            "OPERATOR_CONTACT",
            "STRUCTURED_CRITERIA_UPDATE",
            "RECALCULATE_MATCH",
            "HYBRID_ALTERNATIVES",
            "PRE_BID_CONFIDENCE",
            "POST_AWARD_TRANSACTION_VERIFICATION",
        ],
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "USAspending": 0,
        "paid": 0,
    }

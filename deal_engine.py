"""Canonical deal economics, financing gate, decision, Deal Score, portfolio.

Hard gates always override score. No invented facts.
"""

from __future__ import annotations

from typing import Any

from data_integrity import STATUS_CALCULATED, STATUS_VERIFIED, calculated_fact, verified_fact
from economic_integrity import (
    BID_ELIGIBLE,
    BID_INELIGIBLE,
    BID_NEEDS_RESEARCH,
    BID_WATCH,
    COST_NOT_APPLICABLE,
    COST_REQUIRED_UNKNOWN,
    COST_VERIFIED,
    COST_VERIFIED_ZERO,
    ECON_CALCULATED,
    ECON_INCOMPLETE,
    GATE_FAIL,
    GATE_NEEDS_VERIFICATION,
    GATE_PASS,
    GATE_UNKNOWN,
    calculate_actual_profit,
    evaluate_bid_eligibility,
    evaluate_cash_pg_gates,
    min_actual_profit_usd,
    revenue_item,
)
from product_deal import FIT_CORE_PRODUCT, FIT_SECONDARY_SERVICE
from product_matching import MATCH_COMPLIANT_EQUAL, MATCH_EXACT

DECISION_BID = "BID"
DECISION_WATCH = "WATCH"
DECISION_REJECT = "REJECT"
DECISION_NEEDS_RESEARCH = "NEEDS_RESEARCH"

GROSS_RETENTION_PCT = 0.20  # BUSINESS/CASH POLICY — not a contract cost


def evaluate_financing_execution_gate(
    *,
    financing_term: dict[str, Any] | None = None,
    term_verified: bool = False,
) -> dict[str, Any]:
    """
    PASS / FAIL / UNRESOLVED for executable financing/payment path.

    Ordinary Net-30 does NOT automatically PASS.
    Generic marketing does NOT PASS.
    """
    term = financing_term or {}
    if term.get("is_generic_marketing") is True:
        return {
            "status": GATE_FAIL if term_verified else GATE_NEEDS_VERIFICATION,
            "reasons": ["generic_marketing_not_deal_approval"],
            "evidence": term,
        }

    if not term or not term_verified:
        # Net-30 alone is insufficient
        if str(term.get("advance_structure") or "").upper() in {"NET30", "NET-30", "NET 30"}:
            return {
                "status": GATE_NEEDS_VERIFICATION,
                "reasons": ["ordinary_net30_not_automatically_executable"],
                "evidence": term,
            }
        return {
            "status": GATE_NEEDS_VERIFICATION if not term else GATE_UNKNOWN,
            "reasons": ["financing_terms_not_verified"],
            "evidence": term,
        }

    reasons: list[str] = []
    if term.get("pg_required") is True:
        return {
            "status": GATE_NEEDS_VERIFICATION,
            "reasons": ["verified_pg_required_operator_review"],
            "operator_pg_review_required": True,
            "autonomous_pg_acceptance": False,
            "evidence": term,
        }
    if term.get("personal_credit_required") is True and term.get("personal_credit_materially_disqualifies") is True:
        return {"status": GATE_FAIL, "reasons": ["verified_personal_credit_materially_disqualifies"], "evidence": term}
    if term.get("personal_credit_required") is True:
        return {
            "status": GATE_NEEDS_VERIFICATION,
            "reasons": ["verified_personal_credit_checked_verify_fico_role"],
            "evidence": term,
        }
    if term.get("cash_deposit_required") is True:
        return {"status": GATE_FAIL, "reasons": ["verified_upfront_cash_required"], "evidence": term}

    # Affirmative executable structures
    mechanics = (
        f"{term.get('supplier_payment_mechanics') or ''} "
        f"{term.get('government_payment_mechanics') or ''} "
        f"{term.get('advance_structure') or ''}"
    ).lower()
    executable_hints = (
        "government po",
        "accepts po",
        "po financing",
        "waits for payment",
        "no personal guarantee",
        "zero upfront",
        "receivable",
    )
    if any(h in mechanics for h in executable_hints):
        if term.get("pg_required") is False and term.get("personal_credit_required") is False:
            if term.get("cash_deposit_required") is False:
                reasons.append("verified_executable_structure")
                return {"status": GATE_PASS, "reasons": reasons, "evidence": term}
            reasons.append("cash_deposit_flag_unresolved")
            return {"status": GATE_NEEDS_VERIFICATION, "reasons": reasons, "evidence": term}

    if (
        term.get("pg_required") is False
        and term.get("personal_credit_required") is False
        and term.get("cash_deposit_required") is False
        and term.get("supplier_payment_mechanics")
        and term.get("government_payment_mechanics")
    ):
        return {
            "status": GATE_PASS,
            "reasons": ["verified_no_pg_no_personal_credit_no_cash_with_payment_mechanics"],
            "evidence": term,
        }

    return {
        "status": GATE_NEEDS_VERIFICATION,
        "reasons": ["insufficient_verified_executable_financing_detail"],
        "evidence": term,
    }


def gross_retention_policy(gross_contract_amount: float | None) -> dict[str, Any]:
    """20% of every GROSS CONTRACT DOLLAR retained — BUSINESS/CASH POLICY."""
    if gross_contract_amount is None:
        return {
            "status": "POLICY",
            "retention_pct": GROSS_RETENTION_PCT,
            "gross_contract_amount": None,
            "retained_gross_reserve": None,
            "notes": "POLICY — not a contract cost; amount UNKNOWN until gross established",
        }
    gross = float(gross_contract_amount)
    reserve = round(gross * GROSS_RETENTION_PCT, 2)
    return {
        "status": "POLICY",
        "retention_pct": GROSS_RETENTION_PCT,
        "gross_contract_amount": gross,
        "retained_gross_reserve": reserve,
        "notes": "BUSINESS/CASH POLICY — distinct from actual profit and cost stack",
        "formula": "gross_contract_amount * 0.20",
    }


def build_deal_economics(
    *,
    operator_bid_amount: float | None = None,
    bid_amount_verified: bool = False,
    costs: dict[str, dict[str, Any]] | None = None,
    financing_fee: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical Deal Economics object from verified/calculated inputs only."""
    costs = dict(costs or {})
    if financing_fee and "financing" not in costs:
        costs["financing"] = financing_fee

    if operator_bid_amount is not None and bid_amount_verified:
        revenue = revenue_item(
            float(operator_bid_amount),
            status=STATUS_VERIFIED,
            source_type="OPERATOR",
            source_field="operator_bid_amount",
            notes="Operator-entered bid amount marked verified by operator action",
        )
    elif operator_bid_amount is not None:
        revenue = {
            "value": float(operator_bid_amount),
            "status": "ASSESSMENT",
            "source_type": "OPERATOR",
            "source_field": "operator_bid_amount",
            "notes": "Operator amount present but not marked verified — cannot drive actual profit",
        }
    else:
        revenue = revenue_item(None, status="UNKNOWN", source_field="operator_bid_amount")

    profit = calculate_actual_profit(revenue=revenue, costs=costs)
    gross = float(operator_bid_amount) if operator_bid_amount is not None else None
    retention = gross_retention_policy(gross if bid_amount_verified else None)

    margin = None
    if profit.get("status") == ECON_CALCULATED and gross and gross > 0 and profit.get("actual_profit") is not None:
        margin = round(float(profit["actual_profit"]) / gross, 4)

    # Cash required before government payment — UNKNOWN unless financing says otherwise
    cash_required = None
    cash_status = "UNKNOWN"
    fin = costs.get("financing") or {}
    if str(fin.get("status")) == COST_NOT_APPLICABLE:
        cash_required = 0.0
        cash_status = STATUS_VERIFIED
    elif str(fin.get("status")) in {COST_VERIFIED, COST_VERIFIED_ZERO, "CALCULATED"} and fin.get("value") is not None:
        # Financing cost known doesn't imply cash-before-pay is known
        cash_status = "UNKNOWN"

    return {
        "revenue": revenue,
        "costs": costs,
        "actual_profit_result": profit,
        "actual_profit": profit.get("actual_profit"),
        "actual_margin": margin,
        "actual_profit_status": profit.get("status"),
        "gross_contract_amount": gross if bid_amount_verified else None,
        "gross_retention_policy": retention,
        "cash_required_before_government_payment": cash_required,
        "cash_required_status": cash_status,
        "financing_requirement": fin,
        "meets_min_actual_profit": bool(profit.get("meets_min_actual_profit")),
        "min_actual_profit_usd": min_actual_profit_usd(),
    }


def decide_product_deal(
    *,
    core_fit: str,
    economics: dict[str, Any],
    financing_gate: dict[str, Any],
    match_result: dict[str, Any] | None = None,
    research_tasks: list[dict[str, Any]] | None = None,
    set_aside_eligible: bool | None = None,
    deadline_impossible: bool | None = None,
    channel_prohibited: bool | None = None,
    product_noncompliant: bool | None = None,
    watch_policy: bool = False,
) -> dict[str, Any]:
    """Deterministic BID / WATCH / REJECT / NEEDS_RESEARCH with reason codes."""
    reasons: list[str] = []
    match = match_result or {}

    if core_fit == FIT_SECONDARY_SERVICE:
        return {
            "decision": DECISION_WATCH,
            "reason_codes": ["SECONDARY_SERVICE_NOT_PRIMARY_MISSION"],
            "bid_eligible": False,
        }

    if set_aside_eligible is False:
        return {
            "decision": DECISION_REJECT,
            "reason_codes": ["VERIFIED_INELIGIBLE_SET_ASIDE"],
            "bid_eligible": False,
        }
    if deadline_impossible is True:
        return {
            "decision": DECISION_REJECT,
            "reason_codes": ["VERIFIED_IMPOSSIBLE_DEADLINE"],
            "bid_eligible": False,
        }
    if channel_prohibited is True:
        return {
            "decision": DECISION_REJECT,
            "reason_codes": ["VERIFIED_PROHIBITED_CHANNEL"],
            "bid_eligible": False,
        }
    if product_noncompliant is True or match.get("match_class") == "NONCOMPLIANT":
        return {
            "decision": DECISION_REJECT,
            "reason_codes": ["VERIFIED_PRODUCT_NONCOMPLIANCE"],
            "bid_eligible": False,
        }

    fin_status = str(financing_gate.get("status") or GATE_UNKNOWN)
    if fin_status == GATE_FAIL:
        return {
            "decision": DECISION_REJECT,
            "reason_codes": list(financing_gate.get("reasons") or ["FINANCING_GATE_FAIL"]),
            "bid_eligible": False,
        }

    profit = economics.get("actual_profit")
    profit_status = str(economics.get("actual_profit_status") or ECON_INCOMPLETE)
    if profit_status == ECON_CALCULATED and profit is not None and float(profit) < min_actual_profit_usd():
        return {
            "decision": DECISION_REJECT,
            "reason_codes": ["ACTUAL_PROFIT_BELOW_10K"],
            "bid_eligible": False,
            "actual_profit": profit,
        }

    blocking_research = [
        t for t in (research_tasks or []) if t.get("blocking") and t.get("status") != "SATISFIED"
    ]
    if blocking_research or fin_status in {GATE_UNKNOWN, GATE_NEEDS_VERIFICATION}:
        codes = [t.get("code") for t in blocking_research if t.get("code")]
        if fin_status in {GATE_UNKNOWN, GATE_NEEDS_VERIFICATION}:
            codes.append("FINANCING_UNRESOLVED")
        if profit_status != ECON_CALCULATED:
            codes.append("ACTUAL_PROFIT_INCOMPLETE")
        if match.get("match_class") not in {MATCH_EXACT, MATCH_COMPLIANT_EQUAL}:
            codes.append("COMPLIANT_PRODUCT_NOT_ESTABLISHED")
        return {
            "decision": DECISION_NEEDS_RESEARCH,
            "reason_codes": codes or ["CRITICAL_GATES_UNRESOLVED"],
            "bid_eligible": False,
        }

    cash_pg = evaluate_cash_pg_gates(
        personal_cash_upfront_required=False,
        personal_credit_required=False,
        personal_guarantee_required=False,
        financing_structure_known=True,
        no_pg_financing_verified=True,
        zero_upfront_financing_verified=True,
    )
    # Only when financing gate already PASS
    eligibility = evaluate_bid_eligibility(
        actual_profit_result=economics.get("actual_profit_result"),
        cash_pg=cash_pg,
        critical_gates={
            "core_product": GATE_PASS if core_fit == FIT_CORE_PRODUCT else GATE_FAIL,
            "product_match": GATE_PASS
            if match.get("match_class") in {MATCH_EXACT, MATCH_COMPLIANT_EQUAL}
            else GATE_UNKNOWN,
            "financing": GATE_PASS if fin_status == GATE_PASS else fin_status,
        },
    )
    if eligibility.get("bid_eligible") and not watch_policy:
        return {
            "decision": DECISION_BID,
            "reason_codes": ["ALL_CRITICAL_GATES_PASS"],
            "bid_eligible": True,
            "eligibility": eligibility,
        }
    if watch_policy:
        return {
            "decision": DECISION_WATCH,
            "reason_codes": ["OPERATOR_OR_PORTFOLIO_WATCH_POLICY"],
            "bid_eligible": False,
        }
    if eligibility.get("decision") == BID_INELIGIBLE:
        return {
            "decision": DECISION_REJECT,
            "reason_codes": eligibility.get("blockers") or ["BID_INELIGIBLE"],
            "bid_eligible": False,
        }
    return {
        "decision": DECISION_NEEDS_RESEARCH,
        "reason_codes": eligibility.get("blockers") or ["GATES_INCOMPLETE"],
        "bid_eligible": False,
        "eligibility": eligibility,
    }


def compute_deal_score(
    *,
    economics: dict[str, Any],
    decision: dict[str, Any],
    financing_gate: dict[str, Any],
    match_result: dict[str, Any] | None = None,
    requirements_completeness: float | None = None,
    days_remaining: int | None = None,
    competition_offers: int | None = None,
    competition_verified: bool = False,
) -> dict[str, Any]:
    """Prioritization score — never manufactures facts; hard gates override."""
    components: dict[str, Any] = {}
    score = 0.0
    confidence = 1.0

    if decision.get("decision") == DECISION_REJECT:
        return {
            "score": 0,
            "components": {"hard_gate_reject": True},
            "hard_gate_override": True,
            "confidence": 1.0,
            "notes": "Hard gates override Deal Score",
        }

    profit = economics.get("actual_profit")
    if economics.get("actual_profit_status") == ECON_CALCULATED and profit is not None:
        # Scale: $10k = 40 pts, $50k+ = 80
        pts = min(80.0, max(0.0, (float(profit) / 10000.0) * 40.0))
        components["actual_profit"] = {"points": pts, "status": "CALCULATED", "value": profit}
        score += pts
    else:
        components["actual_profit"] = {"points": 0, "status": "UNKNOWN"}
        confidence *= 0.5

    margin = economics.get("actual_margin")
    if margin is not None:
        pts = min(20.0, float(margin) * 100)
        components["actual_margin"] = {"points": pts, "status": "CALCULATED", "value": margin}
        score += pts
    else:
        components["actual_margin"] = {"points": 0, "status": "UNKNOWN"}
        confidence *= 0.7

    if requirements_completeness is not None:
        pts = float(requirements_completeness) * 15.0
        components["requirements_completeness"] = {
            "points": pts,
            "status": "CALCULATED",
            "value": requirements_completeness,
        }
        score += pts
    else:
        components["requirements_completeness"] = {"points": 0, "status": "UNKNOWN"}
        confidence *= 0.8

    fin = str(financing_gate.get("status") or GATE_UNKNOWN)
    if fin == GATE_PASS:
        components["financing_readiness"] = {"points": 15, "status": GATE_PASS}
        score += 15
    elif fin == GATE_FAIL:
        components["financing_readiness"] = {"points": 0, "status": GATE_FAIL}
    else:
        components["financing_readiness"] = {"points": 0, "status": fin}
        confidence *= 0.6

    match = match_result or {}
    if match.get("match_class") in {MATCH_EXACT, MATCH_COMPLIANT_EQUAL}:
        components["supplier_product_readiness"] = {"points": 15, "status": match.get("match_class")}
        score += 15
    else:
        components["supplier_product_readiness"] = {
            "points": 0,
            "status": match.get("match_class") or "UNKNOWN",
        }
        confidence *= 0.6

    if competition_verified and competition_offers is not None:
        # Fewer offers → higher points (policy), only when verified
        pts = max(0.0, 10.0 - float(competition_offers))
        components["competition"] = {
            "points": pts,
            "status": "VERIFIED",
            "offers": competition_offers,
        }
        score += pts
    else:
        components["competition"] = {"points": 0, "status": "UNKNOWN"}
        confidence *= 0.9

    if days_remaining is not None:
        pts = min(10.0, max(0.0, float(days_remaining) / 3.0))
        components["time_remaining"] = {"points": pts, "status": "CALCULATED", "days": days_remaining}
        score += pts
    else:
        components["time_remaining"] = {"points": 0, "status": "UNKNOWN"}

    return {
        "score": round(score, 2),
        "components": components,
        "hard_gate_override": False,
        "confidence": round(confidence, 3),
        "status": "POLICY_SCORE",
        "notes": "Missing data reduces confidence — never filled with assumed values",
    }


def evaluate_portfolio_capacity(
    *,
    active_bids: int | None = None,
    awarded_unpaid_obligations: float | None = None,
    financing_committed: float | None = None,
    cash_exposure: float | None = None,
    verified_financing_capacity: float | None = None,
    proposed_exposure: float | None = None,
) -> dict[str, Any]:
    """Portfolio capacity gate — UNRESOLVED when capacity cannot be established."""
    known = {
        "active_bids": active_bids,
        "awarded_unpaid_obligations": awarded_unpaid_obligations,
        "financing_committed": financing_committed,
        "cash_exposure": cash_exposure,
        "verified_financing_capacity": verified_financing_capacity,
    }
    if verified_financing_capacity is None:
        return {
            "portfolio_status": "UNRESOLVED",
            "reasons": ["verified_financing_capacity_unknown"],
            "known": known,
            "notes": "Unknown capacity must not be invented — not a fake PASS",
        }
    exposure = 0.0
    for v in (awarded_unpaid_obligations, financing_committed, cash_exposure, proposed_exposure):
        if v is not None:
            exposure += float(v)
    if exposure > float(verified_financing_capacity):
        return {
            "portfolio_status": "FAIL",
            "reasons": ["proposed_exposure_exceeds_verified_capacity"],
            "known": known,
            "total_exposure": exposure,
            "capacity": float(verified_financing_capacity),
        }
    # Still unresolved if core exposure inputs missing
    if cash_exposure is None and financing_committed is None:
        return {
            "portfolio_status": "UNRESOLVED",
            "reasons": ["cash_and_financing_exposure_unknown"],
            "known": known,
        }
    return {
        "portfolio_status": "PASS",
        "reasons": ["within_verified_capacity"],
        "known": known,
        "total_exposure": exposure,
        "capacity": float(verified_financing_capacity),
    }

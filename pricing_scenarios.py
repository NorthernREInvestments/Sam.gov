"""Pricing scenarios, sensitivity, break-even — no invented win probabilities."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from bid_pricing_constants import (
    CONF_UNKNOWN,
    REC_BID,
    REC_NEEDS_VERIFY,
    SCENARIO_AGGRESSIVE,
    SCENARIO_BALANCED,
    SCENARIO_BASE,
    SCENARIO_DOWNSIDE,
    SCENARIO_MARGIN,
    SCENARIO_SEVERE,
)
from economic_integrity import min_actual_profit_usd
from transaction_economics import (
    classify_profit_vs_floor,
    compute_transaction_economics,
    money_float,
    profit_floor_config,
)


def _landed(
    acquisition: float | None,
    freight: float | None,
    financing: float | None,
    expense: float | None,
    risk: float | None,
) -> float | None:
    if acquisition is None or freight is None or financing is None:
        return None
    return float(
        Decimal(str(acquisition))
        + Decimal(str(freight))
        + Decimal(str(financing))
        + Decimal(str(expense or 0))
        + Decimal(str(risk or 0))
    )


def build_pricing_scenarios(
    *,
    acquisition: float | None,
    freight: float | None,
    financing: float | None,
    expense: float | None = 0.0,
    risk: float | None = 0.0,
    historical_benchmark_unit: float | None = None,
    quantity: float | None = None,
    basket_msrp: float | None = None,
    government_estimate: float | None = None,
    evaluation_basis: str | None = None,
    acquisition_confidence: str = CONF_UNKNOWN,
    freight_confidence: str = CONF_UNKNOWN,
    financing_confidence: str = CONF_UNKNOWN,
    profit_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = profit_config or profit_floor_config()
    floor = cfg["minimum_transaction_profit"]
    landed = _landed(acquisition, freight, financing, expense, risk)
    if landed is None:
        return {
            "kind": "PricingScenarios",
            "scenarios": [],
            "recommended": None,
            "recommendation_status": REC_NEEDS_VERIFY,
            "win_probability_invented": False,
            "reason": "insufficient_cost_evidence",
        }

    hist_total = None
    if historical_benchmark_unit is not None and quantity:
        hist_total = historical_benchmark_unit * quantity

    # Scenario bids: relative to landed + floor and historical
    min_bid = landed + floor
    aggressive = min_bid  # meets floor only — competitive
    if hist_total is not None:
        balanced = max(min_bid, min(hist_total * 0.98, hist_total))
        # stay at/below hist when possible while meeting floor
        balanced = max(min_bid, hist_total * 0.97)
        margin_prot = max(min_bid * 1.08, hist_total * 1.02 if hist_total else min_bid * 1.08)
    else:
        balanced = min_bid * 1.05
        margin_prot = min_bid * 1.12

    # LPTA tends toward aggressive/balanced; best value allows margin protective
    if evaluation_basis and "LPTA" in str(evaluation_basis).upper():
        margin_prot = balanced * 1.03

    def scenario(name: str, bid: float) -> dict[str, Any]:
        econ = compute_transaction_economics(
            government_bid_revenue=bid,
            product_acquisition_cost=acquisition,
            freight_cost=freight,
            financing_cost=financing,
            transaction_expense=expense,
            risk_allowance=risk,
            acquisition_confidence=acquisition_confidence,
            freight_confidence=freight_confidence,
            financing_confidence=financing_confidence,
            profit_config=cfg,
        )
        profit = econ["ExpectedNetTransactionProfit"]["value"]
        return {
            "scenario": name,
            "total_bid": money_float(bid),
            "unit_pricing": money_float(bid / quantity) if quantity else None,
            "expected_profit": profit,
            "expected_margin": econ["NetTransactionMargin"],
            "relation_to_historical": (
                round((bid / hist_total - 1) * 100, 2) if hist_total else None
            ),
            "relation_to_msrp": round((bid / basket_msrp) * 100, 2) if basket_msrp else None,
            "relation_to_government_estimate": (
                round((bid / government_estimate) * 100, 2) if government_estimate else None
            ),
            "profit_floor_flag": econ["profit_floor_flag"],
            "confidence": "MEDIUM" if acquisition_confidence.startswith("VERIFIED") else "LOW",
            "major_assumptions": [
                f"acquisition={acquisition}",
                f"freight={freight}",
                f"financing={financing}",
                f"risk={risk}",
            ],
            "win_probability": None,
            "is_autonomous_bid_decision": False,
            "economics": econ,
        }

    scenarios = [
        scenario(SCENARIO_AGGRESSIVE, aggressive),
        scenario(SCENARIO_BALANCED, balanced),
        scenario(SCENARIO_MARGIN, margin_prot),
    ]

    # Recommend balanced when evidence sufficient (verified-ish costs + some hist or msrp)
    sufficient = acquisition_confidence in {
        "VERIFIED_BINDING",
        "VERIFIED_PUBLIC",
        "RECENT_HISTORICAL",
        "DEFENSIBLE_ESTIMATE",
    } and freight_confidence != CONF_UNKNOWN and financing_confidence != CONF_UNKNOWN

    recommended = None
    status = REC_NEEDS_VERIFY
    why = []
    if sufficient:
        pick = scenarios[1]  # balanced
        if pick["expected_profit"] is not None and pick["expected_profit"] >= floor:
            recommended = {
                "status": REC_BID,
                "total_bid": pick["total_bid"],
                "scenario": SCENARIO_BALANCED,
                "expected_profit": pick["expected_profit"],
                "expected_margin": pick["expected_margin"],
                "why": [
                    "Meets configured profit floor after acquisition, freight, financing, expenses, risk",
                    "Balanced vs historical/MSRP context when available",
                    f"Evaluation basis={evaluation_basis or 'UNKNOWN'}",
                    "Not a win-probability claim",
                ],
            }
            status = REC_BID
            why = recommended["why"]
        else:
            why = ["balanced_scenario_below_profit_floor"]
    else:
        why = ["insufficient_evidence_for_recommendation"]

    return {
        "kind": "PricingScenarios",
        "scenarios": scenarios,
        "recommended": recommended,
        "recommendation_status": status,
        "recommendation_why": why,
        "win_probability_invented": False,
        "false_precision_avoided": not sufficient,
    }


def sensitivity_analysis(
    *,
    base_bid: float,
    acquisition: float,
    freight: float,
    financing: float,
    expense: float = 0.0,
    risk: float = 0.0,
    profit_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = profit_config or profit_floor_config()
    floor = cfg["minimum_transaction_profit"]

    def run(acq, fr, fin, bid, label) -> dict[str, Any]:
        econ = compute_transaction_economics(
            government_bid_revenue=bid,
            product_acquisition_cost=acq,
            freight_cost=fr,
            financing_cost=fin,
            transaction_expense=expense,
            risk_allowance=risk,
            acquisition_confidence="DEFENSIBLE_ESTIMATE",
            freight_confidence="DEFENSIBLE_ESTIMATE",
            financing_confidence="DEFENSIBLE_ESTIMATE",
            profit_config=cfg,
        )
        profit = econ["ExpectedNetTransactionProfit"]["value"]
        return {
            "case": label,
            "bid": bid,
            "acquisition": acq,
            "freight": fr,
            "financing": fin,
            "expected_profit": profit,
            "below_floor": profit is not None and profit < floor,
            "profit_floor_flag": econ["profit_floor_flag"],
        }

    base = run(acquisition, freight, financing, base_bid, SCENARIO_BASE)
    downside = run(acquisition * 1.05, freight * 1.25, financing * 1.1, base_bid * 0.95, SCENARIO_DOWNSIDE)
    severe = run(acquisition * 1.10, freight * 1.25, financing * 1.2, base_bid * 0.90, SCENARIO_SEVERE)

    # How much acquisition can rise before profit hits floor at base bid
    # profit = bid - acq - fr - fin - exp - risk >= floor
    # acq_max = bid - fr - fin - exp - risk - floor
    max_acq = base_bid - freight - financing - expense - risk - floor
    headroom = max_acq - acquisition if max_acq is not None else None

    return {
        "kind": "SensitivityAnalysis",
        "cases": [base, downside, severe],
        "profit_floor": floor,
        "acquisition_headroom_before_floor": money_float(headroom),
        "question_answered": "HOW MUCH CAN GO WRONG BEFORE THIS FALLS BELOW OUR PROFIT FLOOR?",
        "scenario_count_limited": True,
    }


def break_even_and_max_costs(
    *,
    bid_revenue: float | None,
    freight: float | None = None,
    financing: float | None = None,
    expense: float | None = 0.0,
    risk: float | None = 0.0,
    acquisition: float | None = None,
    target_profit: float | None = None,
) -> dict[str, Any]:
    floor = float(target_profit if target_profit is not None else min_actual_profit_usd())
    if bid_revenue is None or freight is None or financing is None:
        return {
            "kind": "BreakEvenAnalysis",
            "status": "INCOMPLETE",
            "break_even_acquisition_cost": None,
            "max_acquisition_for_target_profit": None,
            "max_freight": None,
            "max_financing": None,
            "minimum_acceptable_bid_revenue": None,
        }
    be_acq = bid_revenue - freight - financing - (expense or 0) - (risk or 0)
    max_acq = be_acq - floor
    max_freight = None
    max_fin = None
    if acquisition is not None:
        max_freight = bid_revenue - acquisition - financing - (expense or 0) - (risk or 0) - floor
        max_fin = bid_revenue - acquisition - freight - (expense or 0) - (risk or 0) - floor
    min_rev = None
    if acquisition is not None:
        min_rev = acquisition + freight + financing + (expense or 0) + (risk or 0) + floor

    narrative = None
    if bid_revenue is not None and max_acq is not None:
        narrative = (
            f"At a ${bid_revenue:,.0f} bid, we can pay up to ${max_acq:,.0f} landed "
            f"(acq+freight+financing+fees+risk structure) components while retaining "
            f"${floor:,.0f} target profit — see component maxes."
        )

    return {
        "kind": "BreakEvenAnalysis",
        "status": "CALCULATED",
        "break_even_acquisition_cost": money_float(be_acq),
        "max_acquisition_for_target_profit": money_float(max_acq),
        "max_freight": money_float(max_freight),
        "max_financing": money_float(max_fin),
        "minimum_acceptable_bid_revenue": money_float(min_rev),
        "target_profit": floor,
        "narrative": narrative,
        "exact_transaction_math": True,
    }


def commercial_verification_targets(
    *,
    bid_revenue: float | None,
    freight: float | None,
    financing: float | None,
    expense: float | None = 0.0,
    risk: float | None = 0.0,
    acquisition: float | None = None,
    delivery_by: str | None = None,
    authorization_document: str | None = None,
    target_profit: float | None = None,
) -> dict[str, Any]:
    """Specific FUTURE_ACTION targets — do not perform outreach."""
    be = break_even_and_max_costs(
        bid_revenue=bid_revenue,
        freight=freight,
        financing=financing,
        expense=expense,
        risk=risk,
        acquisition=acquisition,
        target_profit=target_profit,
    )
    actions = []
    if be.get("max_acquisition_for_target_profit") is not None:
        landed_cap = None
        if bid_revenue is not None:
            floor = float(target_profit if target_profit is not None else min_actual_profit_usd())
            landed_cap = bid_revenue - floor - (expense or 0) - (risk or 0)
        actions.append(
            {
                "action": "obtain_binding_supplier_quote",
                "status": "FUTURE_ACTION_IF_PURSUED",
                "target": f"Need <= ${be['max_acquisition_for_target_profit']:,.2f} acquisition"
                + (f" / landed budget <= ${landed_cap:,.2f}" if landed_cap else ""),
                "numeric_target_usd": be["max_acquisition_for_target_profit"],
            }
        )
    fin_cap = be.get("max_financing")
    if fin_cap is None and bid_revenue is not None and acquisition is not None and freight is not None:
        floor = float(target_profit if target_profit is not None else min_actual_profit_usd())
        fin_cap = bid_revenue - acquisition - freight - (expense or 0) - (risk or 0) - floor
    if fin_cap is not None:
        actions.append(
            {
                "action": "obtain_financing_indication",
                "status": "FUTURE_ACTION_IF_PURSUED",
                "target": f"Financing must cost <= ${fin_cap:,.2f}",
                "numeric_target_usd": fin_cap,
            }
        )
    elif financing is not None:
        actions.append(
            {
                "action": "obtain_financing_indication",
                "status": "FUTURE_ACTION_IF_PURSUED",
                "target": f"Financing must cost <= ${financing:,.2f} (current estimate ceiling)",
                "numeric_target_usd": financing,
            }
        )
    if delivery_by:
        actions.append(
            {
                "action": "verify_lead_time",
                "status": "FUTURE_ACTION_IF_PURSUED",
                "target": f"Must arrive by {delivery_by}",
            }
        )
    if authorization_document:
        actions.append(
            {
                "action": "verify_authorized_channel",
                "status": "FUTURE_ACTION_IF_PURSUED",
                "target": f"Must provide {authorization_document}",
            }
        )
    return {
        "kind": "CommercialVerificationTargets",
        "actions": actions,
        "performed": False,
        "outreach_count": 0,
        "note": "FUTURE_ACTION_IF_PURSUED only",
    }

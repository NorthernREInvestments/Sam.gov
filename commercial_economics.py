"""Company proposed bid price + live economics recalculation."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

from economic_integrity import (
    COST_CALCULATED,
    COST_NOT_APPLICABLE,
    COST_REQUIRED_UNKNOWN,
    COST_UNKNOWN,
    COST_VERIFIED,
    COST_VERIFIED_ZERO,
    ECON_CALCULATED,
    ECON_INCOMPLETE,
    calculate_actual_profit,
    cost_item,
    revenue_item,
)

PROPOSED_BID_FACT_CLASS = "COMPANY_PROPOSED_BID_PRICE"
GROSS_RETENTION_POLICY = 0.20  # POLICY — not a contract cost


def record_proposed_bid(
    *,
    amount: float | None,
    entered_by: str | None = None,
    reason: str | None = None,
    prior_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    history = list(prior_history or [])
    entry = {
        "amount": amount,
        "entered_by": entered_by,
        "entered_at": now_utc().isoformat(),
        "reason": reason,
        "fact_class": PROPOSED_BID_FACT_CLASS,
        "is_government_verified_value": False,
    }
    if amount is not None:
        history.append(entry)
    return {
        "current": entry if amount is not None else {
            "amount": None,
            "fact_class": PROPOSED_BID_FACT_CLASS,
            "status": "UNKNOWN",
            "is_government_verified_value": False,
        },
        "history": history,
        "LIVE_API_REQUESTS": 0,
    }


def recalculate_commercial_economics(
    *,
    proposed_bid_amount: float | None,
    supplier_acquisition: float | None = None,
    supplier_status: str = COST_REQUIRED_UNKNOWN,
    freight: float | None = None,
    freight_status: str = COST_REQUIRED_UNKNOWN,
    financing_cost: float | None = None,
    financing_status: str = COST_REQUIRED_UNKNOWN,
    installation_status: str = COST_NOT_APPLICABLE,
    installation_value: float | None = None,
    subcontract_status: str = COST_NOT_APPLICABLE,
    other_required: float | None = None,
    other_status: str = COST_NOT_APPLICABLE,
    warranty_support_included: bool = True,
) -> dict[str, Any]:
    """
    Revenue = COMPANY_PROPOSED_BID_PRICE (not government value).
    20% gross retention is POLICY display only — not subtracted as cost.
    Blank/unknown costs do not become zero.
    """
    if proposed_bid_amount is None:
        revenue = revenue_item(value=None, status="UNKNOWN", source_field="operator_proposed_bid")
    else:
        revenue = revenue_item(
            value=float(proposed_bid_amount),
            status="CALCULATED",
            source_type="OPERATOR",
            source_field="company_proposed_bid_price",
            notes="COMPANY_PROPOSED_BID_PRICE — not government verified value",
        )
        revenue["fact_class"] = PROPOSED_BID_FACT_CLASS
        revenue["is_government_verified_value"] = False

    def _cost(cat: str, value: float | None, status: str, *, required: bool) -> dict[str, Any]:
        st = status
        if st == COST_NOT_APPLICABLE:
            return cost_item(
                category=cat,
                status=COST_NOT_APPLICABLE,
                required=False,
                value=0,
                basis="commercial_workspace",
            )
        if st in {COST_UNKNOWN, COST_REQUIRED_UNKNOWN} or value is None:
            return cost_item(
                category=cat,
                status=COST_REQUIRED_UNKNOWN if required else COST_UNKNOWN,
                required=required,
                value=None,
                basis="commercial_workspace",
            )
        return cost_item(
            category=cat,
            status=st,
            required=required,
            value=float(value),
            basis="commercial_workspace",
        )

    costs = {
        "supplier": _cost("supplier", supplier_acquisition, supplier_status, required=True),
        "freight": _cost("freight", freight, freight_status, required=True),
        "financing": _cost("financing", financing_cost, financing_status, required=True),
        "installation": _cost(
            "installation",
            installation_value,
            installation_status,
            required=installation_status != COST_NOT_APPLICABLE,
        ),
        "subcontract": _cost(
            "subcontract",
            None,
            subcontract_status,
            required=subcontract_status != COST_NOT_APPLICABLE,
        ),
        "fees": _cost(
            "fees",
            other_required,
            other_status,
            required=other_status != COST_NOT_APPLICABLE,
        ),
    }
    if warranty_support_included:
        costs["warranty_support"] = cost_item(
            category="warranty_support",
            status=COST_NOT_APPLICABLE,
            required=False,
            value=0,
            notes="Assumed included in product quote unless quote says otherwise",
            basis="included_in_bom_support_line",
        )

    profit = calculate_actual_profit(revenue=revenue, costs=costs)

    usable_total = 0.0
    usable_ok = True
    for c in costs.values():
        st = str(c.get("status") or "")
        if st in {COST_REQUIRED_UNKNOWN, COST_UNKNOWN}:
            usable_ok = False
        elif st in {COST_VERIFIED, COST_CALCULATED, COST_VERIFIED_ZERO} and c.get("value") is not None:
            usable_total += float(c["value"])

    actual_margin = None
    if profit.get("status") == ECON_CALCULATED and proposed_bid_amount:
        try:
            actual_margin = float(profit["actual_profit"]) / float(proposed_bid_amount)
        except (TypeError, ValueError, ZeroDivisionError):
            actual_margin = None

    retention_target = (
        float(proposed_bid_amount) * GROSS_RETENTION_POLICY if proposed_bid_amount is not None else None
    )

    return {
        "proposed_bid_price": {
            "value": proposed_bid_amount,
            "fact_class": PROPOSED_BID_FACT_CLASS,
            "is_government_verified_value": False,
        },
        "total_verified_calculated_required_cost": usable_total if usable_ok else None,
        "total_cost_status": "CALCULATED" if usable_ok else "INCOMPLETE",
        "actual_profit": profit.get("actual_profit"),
        "actual_profit_status": profit.get("status"),
        "actual_margin": actual_margin,
        "gross_retention_20pct_target": {
            "value": retention_target,
            "status": "POLICY",
            "notes": "Portfolio planning only — NOT subtracted as a contract cost",
        },
        "cash_required_before_government_payment": {
            "value": None,
            "status": "UNKNOWN",
            "notes": "Depends on verified financing advance structure",
        },
        "financing_status": financing_status,
        "costs": costs,
        "revenue": revenue,
        "actual_profit_result": profit,
        "LIVE_API_REQUESTS": 0,
    }


def evaluate_financing_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Classify financing from operator entry — UNKNOWN blocks PASS."""
    pg = raw.get("pg_required")
    credit = raw.get("personal_credit_required")
    cash = raw.get("cash_contribution")
    eligible = raw.get("transaction_eligible")

    blockers = []
    if pg is True:
        blockers.append("PG operator review required")
    if credit is True and raw.get("personal_credit_materially_disqualifies") is True:
        blockers.append("personal credit materially disqualifies")
    elif credit is True:
        blockers.append("personal credit role/floor unknown")
    if cash not in (None, "", 0, 0.0, "0", False) and cash != "UNKNOWN":
        try:
            if float(cash) > 0:
                blockers.append("cash contribution required")
        except (TypeError, ValueError):
            if str(cash).upper() not in {"NO", "NONE", "ZERO"}:
                blockers.append("cash contribution unclear")

    if pg is None or pg == "UNKNOWN":
        blockers.append("PG unknown")
    if credit is None or credit == "UNKNOWN":
        blockers.append("personal credit unknown")
    if cash in (None, "", "UNKNOWN"):
        blockers.append("cash contribution unknown")
    if eligible is not True:
        blockers.append("transaction eligibility not verified")

    hard = {"personal credit materially disqualifies", "cash contribution required"}
    if any(b in hard for b in blockers):
        status = "FINANCING_FAIL"
    elif blockers:
        status = "FINANCING_UNRESOLVED"
    else:
        status = "FINANCING_PASS"

    return {
        "status": status,
        "blockers": blockers,
        "operator_pg_review_required": pg is True,
        "autonomous_pg_acceptance": False,
        "provider": raw.get("provider"),
        "LIVE_API_REQUESTS": 0,
    }

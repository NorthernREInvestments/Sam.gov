"""Real deal economics — UNKNOWN ≠ 0; max supplier cost / min bid from known inputs only."""

from __future__ import annotations

from typing import Any

from deep_deal_constants import (
    PROFIT_BELOW,
    PROFIT_ESTIMATED,
    PROFIT_HIGH,
    PROFIT_POTENTIAL,
    PROFIT_UNKNOWN,
    PROFIT_VERIFIED,
)
from economic_integrity import min_actual_profit_usd


def _f(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _money(status: str, value: float | None = None, *, required: bool = False, notes: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "value": value,
        "required": required,
        "notes": notes,
    }


def build_deal_economics(
    *,
    expected_revenue: float | None = None,
    revenue_status: str = "UNKNOWN",
    supplier_cost: float | None = None,
    supplier_cost_status: str = "UNKNOWN",
    freight: float | None = None,
    freight_status: str = "UNKNOWN",
    installation_subcontract: float | None = None,
    installation_status: str = "UNKNOWN",
    bond_cost: float | None = None,
    bond_status: str = "NOT_APPLICABLE",
    financing_fee: float | None = None,
    financing_status: str = "UNKNOWN",
    other_performance: float | None = None,
    other_status: str = "UNKNOWN",
    accessories: float | None = None,
    accessories_status: str = "NOT_APPLICABLE",
    warranty_cost: float | None = None,
    warranty_status: str = "NOT_APPLICABLE",
    insurance_cost: float | None = None,
    insurance_status: str = "NOT_APPLICABLE",
    bank_cost: float | None = None,
    bank_status: str = "NOT_APPLICABLE",
) -> dict[str, Any]:
    """
    Deal-level economics from evidence only.
    Unknown costs stay UNKNOWN and reduce confidence — never become $0.
    """
    target = min_actual_profit_usd()
    costs = {
        "supplier_product_cost": _money(supplier_cost_status, supplier_cost, required=True),
        "freight": _money(freight_status, freight, required=True),
        "shipping": _money("NOT_APPLICABLE", None, notes="folded_into_freight_when_known"),
        "insurance": _money(insurance_status, insurance_cost),
        "subcontract_installation": _money(installation_status, installation_subcontract),
        "bond_cost": _money(bond_status, bond_cost),
        "financing_fees": _money(financing_status, financing_fee),
        "payment_processing_bank": _money(bank_status, bank_cost),
        "required_accessories": _money(accessories_status, accessories),
        "warranty_cost": _money(warranty_status, warranty_cost),
        "other_performance_costs": _money(other_status, other_performance),
    }

    known_cost_values = [c["value"] for c in costs.values() if c["status"] in {"VERIFIED", "CALCULATED", "VERIFIED_ZERO"} and c["value"] is not None]
    required_unknown = [k for k, c in costs.items() if c.get("required") and c["status"] == "UNKNOWN"]
    # freight required only when supplier path exists
    if supplier_cost is None and "freight" in required_unknown:
        required_unknown = [k for k in required_unknown if k != "freight"]

    total_known_cost = sum(known_cost_values) if known_cost_values else None
    cogs = supplier_cost if supplier_cost_status in {"VERIFIED", "CALCULATED"} else None
    non_financing = None
    if known_cost_values:
        non_financing_parts = [
            c["value"]
            for k, c in costs.items()
            if k != "financing_fees" and c["status"] in {"VERIFIED", "CALCULATED", "VERIFIED_ZERO"} and c["value"] is not None
        ]
        non_financing = sum(non_financing_parts) if non_financing_parts else None

    funding_cost = financing_fee if financing_status in {"VERIFIED", "CALCULATED"} else None
    total_cost = total_known_cost
    if required_unknown:
        total_cost = None  # incomplete

    gross_profit = None
    net_profit = None
    gross_margin = None
    expected_profit_pct = None
    rev = expected_revenue if revenue_status in {"VERIFIED", "CALCULATED", "ESTIMATED"} else None

    if rev is not None and cogs is not None:
        gross_profit = rev - cogs
        if rev > 0:
            gross_margin = round(100.0 * gross_profit / rev, 2)
    if rev is not None and total_cost is not None:
        net_profit = rev - total_cost
        if rev > 0:
            expected_profit_pct = round(100.0 * net_profit / rev, 2)

    working_capital = supplier_cost  # theoretical pre-performance need when deposit unknown
    max_cash_exposure = working_capital

    # Profit confidence
    if rev is not None and total_cost is not None and revenue_status in {"VERIFIED", "CALCULATED"} and not required_unknown:
        if net_profit is not None and net_profit >= target:
            profit_conf = PROFIT_VERIFIED if revenue_status == "VERIFIED" and supplier_cost_status == "VERIFIED" else PROFIT_HIGH
        elif net_profit is not None and net_profit < target:
            profit_conf = PROFIT_BELOW
        else:
            profit_conf = PROFIT_UNKNOWN
    elif rev is not None and supplier_cost is not None:
        profit_conf = PROFIT_ESTIMATED if (gross_profit or 0) >= target else (PROFIT_BELOW if gross_profit is not None and gross_profit < target else PROFIT_POTENTIAL)
    elif rev is not None and rev >= target * 4:
        profit_conf = PROFIT_POTENTIAL
    else:
        profit_conf = PROFIT_UNKNOWN

    return {
        "gross_revenue": _money(revenue_status, rev),
        "cogs": _money(supplier_cost_status, cogs),
        "non_financing_performance_cost": _money("CALCULATED" if non_financing is not None else "UNKNOWN", non_financing),
        "funding_cost": _money(financing_status, funding_cost),
        "total_cost": _money("CALCULATED" if total_cost is not None else "UNKNOWN", total_cost),
        "gross_profit": _money("CALCULATED" if gross_profit is not None else "UNKNOWN", gross_profit),
        "expected_deal_profit": _money("CALCULATED" if net_profit is not None else "UNKNOWN", net_profit),
        "gross_margin_pct": gross_margin,
        "expected_profit_pct": expected_profit_pct,
        "working_capital_required": _money("ESTIMATED" if working_capital is not None else "UNKNOWN", working_capital),
        "max_cash_exposure": _money("ESTIMATED" if max_cash_exposure is not None else "UNKNOWN", max_cash_exposure),
        "cost_breakdown": costs,
        "required_unknown_costs": required_unknown,
        "profit_confidence": profit_conf,
        "profit_target_usd": target,
        "meets_profit_target": bool(net_profit is not None and net_profit >= target),
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def maximum_allowable_supplier_cost(
    *,
    expected_revenue: float | None,
    min_profit: float | None = None,
    freight: float | None = None,
    financing_fee: float | None = None,
    other_known_costs: float | None = None,
) -> dict[str, Any]:
    """Back-solve max supplier cost from known revenue and known other costs only."""
    target = min_profit if min_profit is not None else min_actual_profit_usd()
    rev = _f(expected_revenue)
    if rev is None:
        return {
            "maximum_allowable_supplier_cost": None,
            "status": "UNKNOWN",
            "reason": "expected_revenue_unknown",
            "inputs_used": {},
            "LIVE_API_REQUESTS": 0,
        }
    known = 0.0
    used = {"revenue": rev, "min_profit": target}
    for name, val in (("freight", freight), ("financing_fee", financing_fee), ("other_known_costs", other_known_costs)):
        f = _f(val)
        if f is not None:
            known += f
            used[name] = f
    max_cost = rev - target - known
    return {
        "maximum_allowable_supplier_cost": round(max_cost, 2),
        "status": "CALCULATED",
        "reason": "back_solved_from_known_inputs",
        "inputs_used": used,
        "note": "If package can be purchased at or below this cost, $10K target clears (given known inputs)",
        "LIVE_API_REQUESTS": 0,
    }


def minimum_required_bid_price(
    *,
    supplier_cost: float | None,
    min_profit: float | None = None,
    freight: float | None = None,
    financing_fee: float | None = None,
    other_known_costs: float | None = None,
) -> dict[str, Any]:
    """Back-solve minimum bid when supplier cost known and government price uncertain."""
    target = min_profit if min_profit is not None else min_actual_profit_usd()
    cost = _f(supplier_cost)
    if cost is None:
        return {
            "minimum_required_bid_price": None,
            "status": "UNKNOWN",
            "reason": "supplier_cost_unknown",
            "LIVE_API_REQUESTS": 0,
        }
    known = cost
    used = {"supplier_cost": cost, "min_profit": target}
    for name, val in (("freight", freight), ("financing_fee", financing_fee), ("other_known_costs", other_known_costs)):
        f = _f(val)
        if f is not None:
            known += f
            used[name] = f
    min_bid = known + target
    return {
        "minimum_required_bid_price": round(min_bid, 2),
        "status": "CALCULATED",
        "reason": "back_solved_from_known_costs",
        "inputs_used": used,
        "LIVE_API_REQUESTS": 0,
    }

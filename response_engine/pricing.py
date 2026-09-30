"""R2 pricing scenarios + Decimal max-buy (reuses L6 formula, Decimal only)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from response_engine.models import new_id
from response_engine.money import D, as_str, money
from response_engine.r2_constants import (
    DEFAULT_FINANCING_RATE,
    SCENARIO,
    SCENARIO_PROFIT,
    UNKNOWN,
    UNKNOWN_PROFIT,
    VERIFIED,
    VERIFIED_PROFIT,
    PROVISIONAL_PROFIT,
)


def calculate_max_buy_decimal(
    *,
    revenue: Any,
    financing_rate: Any = DEFAULT_FINANCING_RATE,
    freight: Any = 0,
    other_fees: Any = 0,
    risk_reserve_rate: Any = 0,
    profit_targets: tuple[Decimal, ...] | None = None,
) -> dict[str, Any]:
    """
    Max acquisition = (revenue − freight − fees − risk − profit) / (1 + financing_rate)
    Same algebra as phase_l.quote_economics.calculate_max_buy_engine — Decimal, internal-only.
    """
    rev = D(revenue)
    if rev is None or rev <= 0:
        return {"ok": False, "reason": "revenue_unknown", "thresholds": {}, "evidence_state": UNKNOWN}
    rate = D(financing_rate) if financing_rate is not None else D(DEFAULT_FINANCING_RATE)
    rate = rate or Decimal("0")
    fr = D(freight) or Decimal("0")
    fees = D(other_fees) or Decimal("0")
    risk_r = D(risk_reserve_rate) or Decimal("0")
    risk = money(rev * risk_r) or Decimal("0")
    denom = Decimal("1") + rate
    targets = profit_targets or (
        Decimal("0"),
        Decimal("5000"),
        Decimal("10000"),
        Decimal("25000"),
    )
    thresholds: dict[str, str | None] = {}
    names = (
        "BREAK_EVEN_MAX_BUY",
        "MAX_BUY_FOR_5K_PROFIT",
        "MAX_BUY_FOR_10K_PROFIT",
        "MAX_BUY_FOR_25K_PROFIT",
    )
    for name, profit in zip(names, targets):
        numer = rev - fr - fees - risk - profit
        val = money(numer / denom) if denom > 0 else None
        thresholds[name] = as_str(val) if val is not None and val > 0 else None
    for name, margin in (
        ("MAX_BUY_FOR_15_PERCENT_MARGIN", Decimal("0.15")),
        ("MAX_BUY_FOR_20_PERCENT_MARGIN", Decimal("0.20")),
        ("MAX_BUY_FOR_25_PERCENT_MARGIN", Decimal("0.25")),
    ):
        numer = rev * (Decimal("1") - margin) - fr - fees - risk
        val = money(numer / denom) if denom > 0 else None
        thresholds[name] = as_str(val) if val is not None and val > 0 else None
    primary = thresholds.get("MAX_BUY_FOR_10K_PROFIT") or thresholds.get("BREAK_EVEN_MAX_BUY")
    return {
        "ok": True,
        "kind": "MAXIMUM_BUY_PRICE",
        "namespace": "INTERNAL_ONLY",
        "revenue": as_str(money(rev)),
        "financing_rate": as_str(rate),
        "freight": as_str(money(fr)),
        "other_fees": as_str(money(fees)),
        "thresholds": thresholds,
        "supplier_quote_target": primary,
        "evidence_state": SCENARIO,
        "formula": "(revenue - freight - fees - risk - profit) / (1 + financing_rate)",
        "legacy_source": "phase_l.quote_economics.calculate_max_buy_engine",
    }


def bid_price_for_target_profit(
    *,
    total_execution_cost: Any,
    target_profit: Any,
) -> dict[str, Any]:
    """bid = cost + profit. Scenario only."""
    cost = D(total_execution_cost)
    profit = D(target_profit)
    if cost is None or profit is None:
        return {"ok": False, "bid_price": None, "evidence_state": UNKNOWN}
    bid = money(cost + profit)
    return {
        "ok": True,
        "bid_price": as_str(bid),
        "target_profit": as_str(money(profit)),
        "execution_cost": as_str(money(cost)),
        "evidence_state": SCENARIO,
        "scenario_type": "TARGET_PROFIT",
    }


def bid_price_for_target_margin(
    *,
    total_execution_cost: Any,
    target_margin: Any,
) -> dict[str, Any]:
    """bid = cost / (1 - margin). Margin is fraction 0-1 or percent>1."""
    cost = D(total_execution_cost)
    m = D(target_margin)
    if cost is None or m is None:
        return {"ok": False, "bid_price": None, "evidence_state": UNKNOWN}
    if m > 1:
        m = m / Decimal("100")
    if m >= 1 or m < 0:
        return {"ok": False, "bid_price": None, "reason": "invalid_margin", "evidence_state": UNKNOWN}
    bid = money(cost / (Decimal("1") - m))
    return {
        "ok": True,
        "bid_price": as_str(bid),
        "target_margin": as_str(m),
        "execution_cost": as_str(money(cost)),
        "evidence_state": SCENARIO,
        "scenario_type": "TARGET_MARGIN",
        "note": "margin = profit/revenue; not markup",
    }


def max_acquisition_for_bid(
    *,
    bid_price: Any,
    target_profit: Any,
    freight: Any = 0,
    packaging: Any = 0,
    financing_rate: Any = DEFAULT_FINANCING_RATE,
    other_fees: Any = 0,
) -> dict[str, Any]:
    """Internal max-buy at a given bid + target profit. INTERNAL ONLY."""
    bid = D(bid_price)
    profit = D(target_profit) or Decimal("0")
    if bid is None:
        return {"ok": False, "max_acquisition": None, "namespace": "INTERNAL_ONLY"}
    # Approximate: solve acq such that bid - (acq*(1+rate)+freight+pack+fees) = profit
    rate = D(financing_rate) or Decimal("0")
    fr = D(freight) or Decimal("0")
    pk = D(packaging) or Decimal("0")
    fees = D(other_fees) or Decimal("0")
    numer = bid - profit - fr - pk - fees
    denom = Decimal("1") + rate
    acq = money(numer / denom) if denom > 0 else None
    return {
        "ok": acq is not None and acq > 0,
        "max_acquisition": as_str(acq) if acq and acq > 0 else None,
        "namespace": "INTERNAL_ONLY",
        "evidence_state": SCENARIO,
        "must_not_reveal_to_supplier": True,
        "must_not_reveal_to_government": True,
    }


def compute_profit(
    *,
    bid_revenue: Any,
    total_execution_cost: Any,
    evidence_quality: str = UNKNOWN,
) -> dict[str, Any]:
    rev = D(bid_revenue)
    cost = D(total_execution_cost)
    if rev is None or cost is None:
        return {
            "expected_profit": None,
            "margin": None,
            "markup": None,
            "profit_status": UNKNOWN_PROFIT,
            "evidence_quality": evidence_quality,
        }
    profit = money(rev - cost)
    margin = money((profit / rev) * Decimal("100")) if rev != 0 and profit is not None else None
    markup = money((profit / cost) * Decimal("100")) if cost != 0 and profit is not None else None
    if evidence_quality == VERIFIED:
        status = VERIFIED_PROFIT
    elif evidence_quality == SCENARIO:
        status = SCENARIO_PROFIT
    else:
        status = PROVISIONAL_PROFIT
    tiers = []
    if profit is not None and profit > 0:
        tiers.append("POSITIVE")
        for t in (5000, 10000, 25000, 50000, 100000):
            if profit >= Decimal(t):
                tiers.append(f"GTE_{t}")
    return {
        "expected_profit": as_str(profit),
        "margin_pct": as_str(margin),
        "markup_pct": as_str(markup),
        "profit_status": status,
        "evidence_quality": evidence_quality,
        "profit_tiers": tiers,
        "economic_fail": bool(profit is not None and profit < 0),
        "audit": {
            "revenue": as_str(money(rev)),
            "execution_cost": as_str(money(cost)),
            "profit": as_str(profit),
        },
    }


def new_bid_price_scenario(
    *,
    response_project_id: str,
    pricing_basis: str,
    unit_bid_price: Any = None,
    extended_bid_price: Any = None,
    total_bid_price: Any = None,
    acquisition_cost: Any = None,
    financing_cost: Any = None,
    expected_profit: Any = None,
    margin: Any = None,
    markup: Any = None,
    internal_max_buy: Any = None,
    evidence_quality: str = SCENARIO,
    line_item_ids: list[str] | None = None,
    scenario_type: str = "OWNER_OR_ENGINE",
) -> dict[str, Any]:
    return {
        "kind": "BidPriceScenario",
        "scenario_id": new_id("BPS"),
        "response_project_id": response_project_id,
        "line_item_ids": line_item_ids or [],
        "pricing_basis": pricing_basis,
        "scenario_type": scenario_type,
        "unit_bid_price": as_str(money(D(unit_bid_price))) if D(unit_bid_price) is not None else None,
        "extended_bid_price": as_str(money(D(extended_bid_price))) if D(extended_bid_price) is not None else None,
        "total_bid_price": as_str(money(D(total_bid_price))) if D(total_bid_price) is not None else None,
        "revenue_basis": pricing_basis,
        "acquisition_cost": as_str(money(D(acquisition_cost))) if D(acquisition_cost) is not None else None,
        "financing_cost": as_str(money(D(financing_cost))) if D(financing_cost) is not None else None,
        "expected_profit": as_str(money(D(expected_profit))) if D(expected_profit) is not None else None,
        "margin": as_str(money(D(margin))) if D(margin) is not None else None,
        "markup": as_str(money(D(markup))) if D(markup) is not None else None,
        "internal_max_buy": as_str(money(D(internal_max_buy))) if D(internal_max_buy) is not None else None,
        "internal_max_buy_namespace": "INTERNAL_ONLY",
        "evidence_quality": evidence_quality,
        "scenario_status": "ACTIVE",
        "not_final_bid": True,
    }

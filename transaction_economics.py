"""Canonical transaction economics for bid assembly — UNKNOWN ≠ $0."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from application_clock import now_utc
from bid_pricing_constants import (
    CONF_COMMERCIAL_VERIFY,
    CONF_UNKNOWN,
    CONF_VERIFIED_BINDING,
    PROFIT_BELOW,
    PROFIT_EXCEEDED,
    PROFIT_MET,
    PROFIT_THIN,
    PROFIT_UNCERTAIN,
)
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
    min_actual_profit_usd,
    revenue_item,
)


def money_dec(v: float | Decimal | None) -> Decimal | None:
    if v is None:
        return None
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money_float(v: Decimal | float | None) -> float | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return float(money_dec(v) or 0)


def cost_with_confidence(
    *,
    category: str,
    amount: float | None,
    confidence: str = CONF_UNKNOWN,
    unit: str = "USD",
    quantity: float | None = None,
    source: str | None = None,
    evidence: Any = None,
    validity_period: str | None = None,
    assumptions: list[str] | None = None,
    required: bool = True,
    protects_against: str | None = None,
) -> dict[str, Any]:
    conf = str(confidence or CONF_UNKNOWN).upper()
    if amount is None or conf in {CONF_UNKNOWN, CONF_COMMERCIAL_VERIFY}:
        st = COST_REQUIRED_UNKNOWN if required else COST_UNKNOWN
        val = None
    elif conf == CONF_VERIFIED_BINDING and amount == 0:
        st = COST_VERIFIED_ZERO
        val = 0.0
    elif conf in {CONF_VERIFIED_BINDING, "VERIFIED_PUBLIC"}:
        st = COST_VERIFIED
        val = money_float(amount)
    else:
        st = COST_CALCULATED
        val = money_float(amount)
    item = cost_item(
        category=category,
        status=st,
        value=val,
        required=required,
        source_type=source,
        notes=protects_against,
        basis=conf,
    )
    item.update(
        {
            "confidence": conf,
            "unit": unit,
            "quantity": quantity,
            "source": source,
            "timestamp": now_utc().isoformat(),
            "evidence": evidence,
            "validity_period": validity_period,
            "assumptions": assumptions or [],
            "protects_against": protects_against,
            "unknown_treated_as_zero": False,
        }
    )
    return item


def profit_floor_config(
    *,
    minimum_transaction_profit: float | None = None,
    minimum_transaction_margin: float | None = None,
    risk_buffer: float | None = None,
    commercial_verification_threshold: float | None = None,
) -> dict[str, Any]:
    return {
        "minimum_transaction_profit": float(
            minimum_transaction_profit if minimum_transaction_profit is not None else min_actual_profit_usd()
        ),
        "minimum_transaction_margin": float(minimum_transaction_margin if minimum_transaction_margin is not None else 0.05),
        "risk_buffer": float(risk_buffer if risk_buffer is not None else 0.0),
        "commercial_verification_threshold": float(
            commercial_verification_threshold if commercial_verification_threshold is not None else 0.15
        ),
    }


def classify_profit_vs_floor(
    profit: float | None,
    revenue: float | None,
    *,
    config: dict[str, Any] | None = None,
) -> str:
    cfg = config or profit_floor_config()
    if profit is None:
        return PROFIT_UNCERTAIN
    floor = cfg["minimum_transaction_profit"]
    if profit < floor:
        return PROFIT_BELOW
    margin = (profit / revenue) if revenue and revenue > 0 else None
    if margin is not None and margin < cfg["minimum_transaction_margin"]:
        return PROFIT_THIN
    if profit > floor * 1.5:
        return PROFIT_EXCEEDED
    return PROFIT_MET


def compute_transaction_economics(
    *,
    government_bid_revenue: float | None,
    product_acquisition_cost: float | None,
    freight_cost: float | None = None,
    financing_cost: float | None = None,
    transaction_expense: float | None = None,
    risk_allowance: float | None = None,
    acquisition_confidence: str = CONF_UNKNOWN,
    freight_confidence: str = CONF_UNKNOWN,
    financing_confidence: str = CONF_UNKNOWN,
    expense_confidence: str = CONF_UNKNOWN,
    risk_confidence: str = "DEFENSIBLE_ESTIMATE",
    risk_protects_against: str | None = None,
    freight_status_label: str | None = None,
    financing_status_label: str | None = None,
    profit_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    EXPECTED TRANSACTION PROFIT =
      bid revenue − acquisition − freight − financing − expenses − risk
    """
    cfg = profit_config or profit_floor_config()
    revenue = revenue_item(
        value=money_float(government_bid_revenue) if government_bid_revenue is not None else None,
        status="CALCULATED" if government_bid_revenue is not None else "UNKNOWN",
        source_field="government_bid_revenue",
        notes="Company/proposed bid revenue — not government verified award",
    )

    costs = {
        "ProductAcquisitionCost": cost_with_confidence(
            category="ProductAcquisitionCost",
            amount=product_acquisition_cost,
            confidence=acquisition_confidence,
            required=True,
        ),
        "FreightCost": cost_with_confidence(
            category="FreightCost",
            amount=freight_cost,
            confidence=freight_confidence,
            required=True,
        ),
        "FinancingCost": cost_with_confidence(
            category="FinancingCost",
            amount=financing_cost,
            confidence=financing_confidence,
            required=True,
        ),
        "TransactionExpense": cost_with_confidence(
            category="TransactionExpense",
            amount=transaction_expense if transaction_expense is not None else 0.0,
            confidence=(
                expense_confidence
                if transaction_expense is not None and expense_confidence != CONF_UNKNOWN
                else ("VERIFIED_BINDING" if transaction_expense is not None else "VERIFIED_BINDING")
            ),
            required=False,
        ),
        "RiskAllowance": cost_with_confidence(
            category="RiskAllowance",
            amount=risk_allowance if risk_allowance is not None else 0.0,
            confidence=risk_confidence,
            required=False,
            protects_against=risk_protects_against or "pricing_uncertainty",
        ),
    }
    if freight_status_label:
        costs["FreightCost"]["freight_state"] = freight_status_label
    if financing_status_label:
        costs["FinancingCost"]["financing_state"] = financing_status_label

    # If required costs unknown, profit incomplete
    profit = calculate_actual_profit(revenue=revenue, costs=costs)

    expected_profit = profit.get("actual_profit")
    rev_val = money_float(government_bid_revenue)
    gross = None
    if rev_val is not None and product_acquisition_cost is not None:
        # Gross before freight/financing — contextual only
        gross = money_float(Decimal(str(rev_val)) - Decimal(str(product_acquisition_cost)))

    net = money_float(expected_profit) if expected_profit is not None else None
    gross_margin = None
    net_margin = None
    if rev_val and rev_val > 0:
        if gross is not None:
            gross_margin = round(gross / rev_val, 4)
        if net is not None:
            net_margin = round(net / rev_val, 4)

    floor_flag = classify_profit_vs_floor(net, rev_val, config=cfg)

    return {
        "kind": "TransactionEconomics",
        "GovernmentBidRevenue": revenue,
        "ProductAcquisitionCost": costs["ProductAcquisitionCost"],
        "FreightCost": costs["FreightCost"],
        "FinancingCost": costs["FinancingCost"],
        "TransactionExpense": costs["TransactionExpense"],
        "RiskAllowance": costs["RiskAllowance"],
        "ExpectedGrossProfit": {"value": gross, "note": "revenue − acquisition only; not full transaction profit"},
        "ExpectedNetTransactionProfit": {"value": net, "status": profit.get("status")},
        "GrossMargin": gross_margin,
        "NetTransactionMargin": net_margin,
        "profit_floor": cfg,
        "profit_floor_flag": floor_flag,
        "calculation": profit,
        "msrp_not_used_as_profit": True,
        "unknown_cost_as_zero": False,
        "status": profit.get("status"),
    }

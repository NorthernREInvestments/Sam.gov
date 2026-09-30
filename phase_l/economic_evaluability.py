"""Phase L.8 — economic evaluability states + immediate recompute."""

from __future__ import annotations

from typing import Any

from phase_l.quote_economics import (
    GOV_VALUE_COMPARABLE,
    GOV_VALUE_EXACT,
    GOV_VALUE_RANGE,
    GOV_VALUE_STRONG,
    GOV_VALUE_UNKNOWN,
    calculate_max_buy_engine,
    expected_revenue,
    freight_reserve_for_row,
    quote_dependent_positive,
    _f,
)

BUILD = "20260927-m3-phase-l8-population-evidence-recovery"

ECONOMICALLY_EVALUABLE_VERIFIED = "ECONOMICALLY_EVALUABLE_VERIFIED"
ECONOMICALLY_EVALUABLE_QUOTE_DEPENDENT = "ECONOMICALLY_EVALUABLE_QUOTE_DEPENDENT"
ECONOMICALLY_EVALUABLE_UNIT_ONLY = "ECONOMICALLY_EVALUABLE_UNIT_ONLY"
ECONOMICALLY_EVALUABLE_RANGE = "ECONOMICALLY_EVALUABLE_RANGE"
NOT_EVALUABLE_GOV_VALUE = "NOT_EVALUABLE_GOV_VALUE"
NOT_EVALUABLE_SUPPLIER = "NOT_EVALUABLE_SUPPLIER"
NOT_EVALUABLE_QUANTITY = "NOT_EVALUABLE_QUANTITY"
NOT_EVALUABLE_CONFIGURATION = "NOT_EVALUABLE_CONFIGURATION"
NOT_EVALUABLE_MULTIPLE = "NOT_EVALUABLE_MULTIPLE"

RECON_QUOTE_TARGET_ONLY = "RECON_QUOTE_TARGET_ONLY"
USABLE_GOV = {GOV_VALUE_EXACT, GOV_VALUE_STRONG, GOV_VALUE_RANGE, GOV_VALUE_COMPARABLE}


def classify_economic_evaluability(
    *,
    gov: dict[str, Any] | None,
    suppliers: list[dict[str, Any]] | None,
    quantity_info: dict[str, Any] | None,
    max_buy: dict[str, Any] | None = None,
    verified_price: float | None = None,
    config_blocked: bool = False,
) -> dict[str, Any]:
    gov = gov or {}
    suppliers = suppliers or []
    quantity_info = quantity_info or {}
    reasons: list[str] = []

    gov_ok = gov.get("state") in USABLE_GOV and (
        _f(gov.get("unit_value")) or _f(gov.get("total_value"))
    )
    if not gov_ok:
        reasons.append(NOT_EVALUABLE_GOV_VALUE)

    has_suppliers = len(suppliers) >= 1
    if not has_suppliers and not verified_price:
        reasons.append(NOT_EVALUABLE_SUPPLIER)

    qty = _f(quantity_info.get("quantity"))
    unit_only = bool(quantity_info.get("unit_only") or quantity_info.get("quality") == "UNIT_ONLY")
    qty_ok = bool(qty) or unit_only or bool(_f(gov.get("total_value")))
    if not qty_ok:
        reasons.append(NOT_EVALUABLE_QUANTITY)

    if config_blocked:
        reasons.append(NOT_EVALUABLE_CONFIGURATION)

    if len(reasons) >= 2:
        state = NOT_EVALUABLE_MULTIPLE
        evaluable = False
    elif len(reasons) == 1:
        state = reasons[0]
        evaluable = False
    else:
        evaluable = True
        if verified_price and gov_ok:
            state = ECONOMICALLY_EVALUABLE_VERIFIED
        elif unit_only and not qty:
            state = ECONOMICALLY_EVALUABLE_UNIT_ONLY
        elif gov.get("state") == GOV_VALUE_RANGE or (
            _f(gov.get("unit_low")) and _f(gov.get("unit_high")) and gov.get("unit_low") != gov.get("unit_high")
        ):
            state = ECONOMICALLY_EVALUABLE_RANGE
        elif has_suppliers and max_buy and max_buy.get("supplier_quote_target"):
            state = ECONOMICALLY_EVALUABLE_QUOTE_DEPENDENT
        else:
            state = ECONOMICALLY_EVALUABLE_QUOTE_DEPENDENT if has_suppliers else ECONOMICALLY_EVALUABLE_RANGE

    return {
        "evaluable": evaluable,
        "state": state,
        "reasons": reasons,
        "path": (
            "verified"
            if state == ECONOMICALLY_EVALUABLE_VERIFIED
            else "unit_only"
            if state == ECONOMICALLY_EVALUABLE_UNIT_ONLY
            else "range"
            if state == ECONOMICALLY_EVALUABLE_RANGE
            else "quote_dependent"
            if state == ECONOMICALLY_EVALUABLE_QUOTE_DEPENDENT
            else "not_evaluable"
        ),
    }


def recompute_economics_from_recovery(
    row: dict[str, Any],
    *,
    gov_rec: dict[str, Any] | None,
    qty_rec: dict[str, Any] | None,
    supplier_rec: dict[str, Any] | None,
) -> dict[str, Any]:
    """Immediate recompute when any recovery component arrives."""
    gov = {
        "state": (gov_rec or {}).get("state") or GOV_VALUE_UNKNOWN,
        "unit_value": (gov_rec or {}).get("unit_value"),
        "unit_low": (gov_rec or {}).get("unit_low"),
        "unit_high": (gov_rec or {}).get("unit_high"),
        "total_value": (gov_rec or {}).get("total_value"),
        "tier": (gov_rec or {}).get("tier"),
        "source": (gov_rec or {}).get("source"),
        "confidence": (gov_rec or {}).get("confidence"),
        "final_award_value": (gov_rec or {}).get("final_award_value"),
    }
    freight = freight_reserve_for_row(row)
    qty = _f((qty_rec or {}).get("quantity")) or 1.0
    unit_only = bool((qty_rec or {}).get("unit_only"))
    max_buy = None
    if gov.get("unit_value"):
        max_buy = calculate_max_buy_engine(
            government_unit=gov["unit_value"],
            quantity=qty if not unit_only else 1.0,
            freight_reserve=freight["amount"],
            unit_low=gov.get("unit_low"),
            unit_high=gov.get("unit_high"),
            risk_reserve_rate=0.02,
        )
    elif gov.get("total_value"):
        max_buy = calculate_max_buy_engine(
            government_total=gov["total_value"],
            freight_reserve=freight["amount"],
            risk_reserve_rate=0.02,
        )

    suppliers = (supplier_rec or {}).get("suppliers") or []
    qdep = quote_dependent_positive(max_buy=max_buy, suppliers=suppliers, gov=gov)
    revenue = expected_revenue(gov, quantity=qty if gov.get("unit_value") else None)

    # Recon-only target when Tier C comparable
    quote_target_kind = None
    if max_buy and max_buy.get("supplier_quote_target"):
        if gov.get("tier") == "C" or not gov.get("final_award_value"):
            quote_target_kind = RECON_QUOTE_TARGET_ONLY
        else:
            quote_target_kind = "ACTIONABLE_QUOTE_TARGET"

    evaluability = classify_economic_evaluability(
        gov=gov,
        suppliers=suppliers,
        quantity_info=qty_rec or {},
        max_buy=max_buy,
    )

    return {
        "government_value": gov,
        "max_buy": max_buy,
        "expected_revenue": revenue,
        "quote_dependent": qdep,
        "freight": freight,
        "evaluability": evaluability,
        "quote_target_kind": quote_target_kind,
        "suppliers": suppliers,
        "quantity": qty_rec,
    }

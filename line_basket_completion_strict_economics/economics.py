"""Strict economics readiness gate — fail closed on thin baskets."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from line_basket_completion_strict_economics.models import (
    BASKET_BLOCKED,
    BASKET_READY_PUBLIC_PRICE,
    BASKET_READY_QUOTE_DEPENDENT,
    ECONOMICS_NOT_READY,
    ECONOMICS_READY,
    EXECUTION_BLOCKED,
    NO_PUBLIC_PRICE,
    NON_MATERIAL,
    PRICED_EXECUTABLE,
    PROFIT_LIKELY,
    PROFIT_POSSIBLE,
    PROFIT_PROVEN,
    PROFIT_UNPROVEN,
    QUOTE_REQUIRED,
    UNPROFITABLE,
)


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def _money(v: Decimal | float | int | None) -> float:
    if v is None:
        return 0.0
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def classify_basket(lines: list[dict[str, Any]], coverage: dict[str, Any]) -> dict[str, Any]:
    material = [
        l
        for l in lines
        if l.get("terminal_state") != NON_MATERIAL and l.get("materiality_tier") in {"P0", "P1"}
    ]
    if not material:
        material = [l for l in lines if l.get("terminal_state") != NON_MATERIAL]

    open_material = [
        l
        for l in material
        if l.get("terminal_state")
        not in {
            PRICED_EXECUTABLE,
            QUOTE_REQUIRED,
            NO_PUBLIC_PRICE,
            "IDENTITY_UNRESOLVED",
            "IDENTITY_AMBIGUOUS",
            EXECUTION_BLOCKED,
            "NO_COMPLIANT_SOURCE",
            "UOM_UNRESOLVED",
            "PACK_UNRESOLVED",
            "TIME_BUDGET_EXHAUSTED",
            "RESEARCH_EXHAUSTED",
            NON_MATERIAL,
            "OTHER_EXPLAINED",
        }
    ]
    priced = [l for l in material if l.get("terminal_state") == PRICED_EXECUTABLE]
    quote = [l for l in material if l.get("terminal_state") == QUOTE_REQUIRED]
    mat_cov = float(coverage.get("MATERIAL_VALUE_COVERAGE") or 0)
    all_terminal = len(open_material) == 0

    if mat_cov >= 0.9 and all_terminal and priced and not quote:
        return {
            "basket_class": BASKET_READY_PUBLIC_PRICE,
            "reason": "material_ge_90_public",
            "material_coverage": mat_cov,
        }
    if mat_cov >= 0.9 and all_terminal and priced and quote:
        return {
            "basket_class": BASKET_READY_QUOTE_DEPENDENT,
            "reason": "material_ge_90_with_quotes",
            "material_coverage": mat_cov,
            "quote_lines": len(quote),
        }
    if all_terminal and priced and mat_cov >= 0.75 and quote:
        return {
            "basket_class": BASKET_READY_QUOTE_DEPENDENT,
            "reason": "material_ge_75_quote_remainder_terminal",
            "material_coverage": mat_cov,
            "quote_lines": len(quote),
        }
    if all_terminal and len(priced) == len(material) and material:
        return {
            "basket_class": BASKET_READY_PUBLIC_PRICE,
            "reason": "all_material_priced",
            "material_coverage": mat_cov,
        }
    return {
        "basket_class": BASKET_BLOCKED,
        "reason": "insufficient_material_coverage" if all_terminal else "open_material_lines",
        "material_coverage": mat_cov,
        "open_material": len(open_material),
    }


def conservative_unresolved_bounds(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute KNOWN_COST and unresolved material bounds from defensible evidence only."""
    priced = [l for l in lines if l.get("terminal_state") == PRICED_EXECUTABLE and l.get("unit_cost") is not None]
    known = sum(_d(l.get("unit_cost")) * _d(l.get("quantity") or 1) for l in priced)

    unresolved = [
        l
        for l in lines
        if l.get("materiality_tier") in {"P0", "P1"}
        and l.get("terminal_state") not in {PRICED_EXECUTABLE, NON_MATERIAL}
    ]
    # Defensible bound: use estimated_extended_value as HIGH exposure; LOW = 50% of estimate only when estimate from gov line
    # Cap per-line estimate so OCR/insurance noise cannot explode exposure
    capped = []
    for l in unresolved:
        v = _d(l.get("estimated_extended_value"))
        if v > Decimal("2000000"):
            v = Decimal("2000000")
        capped.append(v)
    unresolved_value = sum(capped) if capped else Decimal("0")
    has_gov = any(0 < _d(l.get("government_line_value")) <= Decimal("2000000") for l in unresolved)
    if unresolved and (has_gov or unresolved_value > 0):
        high = unresolved_value
        low = unresolved_value * Decimal("0.5") if has_gov else None
    else:
        high = None
        low = None

    return {
        "KNOWN_COST": _money(known),
        "UNRESOLVED_MATERIAL_VALUE": _money(unresolved_value),
        "LOW_COST_BOUND": _money(low) if low is not None else None,
        "HIGH_COST_BOUND": _money(high) if high is not None else None,
        "unresolved_material_lines": len(unresolved),
        "bound_defensible": high is not None,
    }


def assess_economics_readiness(
    *,
    coverage: dict[str, Any],
    lines: list[dict[str, Any]],
    bounds: dict[str, Any],
    revenue: float,
) -> dict[str, Any]:
    mat_cov = float(coverage.get("MATERIAL_VALUE_COVERAGE") or 0)
    material = [l for l in lines if l.get("materiality_tier") in {"P0", "P1"} and l.get("terminal_state") != NON_MATERIAL]
    if not material:
        material = [l for l in lines if l.get("terminal_state") != NON_MATERIAL]

    terminal_ok = all(
        l.get("terminal_state")
        in {
            PRICED_EXECUTABLE,
            QUOTE_REQUIRED,
            NO_PUBLIC_PRICE,
            "IDENTITY_UNRESOLVED",
            EXECUTION_BLOCKED,
            "NO_COMPLIANT_SOURCE",
            "UOM_UNRESOLVED",
            "PACK_UNRESOLVED",
            NON_MATERIAL,
            "OTHER_EXPLAINED",
        }
        for l in material
    )
    quote_bounded = all(
        l.get("terminal_state") != QUOTE_REQUIRED
        or (l.get("quote_cost_high") is not None or bounds.get("HIGH_COST_BOUND") is not None)
        for l in material
    )

    # Path A
    if mat_cov >= 0.9 and terminal_ok:
        return {"economics_status": ECONOMICS_READY, "path": "A_ge90_material_terminal", "material_coverage": mat_cov}
    # Path B
    material_all_terminal = terminal_ok and material
    if material_all_terminal and all(
        l.get("terminal_state") in {PRICED_EXECUTABLE, QUOTE_REQUIRED} for l in material if l.get("terminal_state") != NON_MATERIAL
    ) and quote_bounded and mat_cov >= 0.75:
        return {"economics_status": ECONOMICS_READY, "path": "B_all_material_terminal_quotes_bounded", "material_coverage": mat_cov}
    # Path C — worst-case remaining cost still profitable
    if bounds.get("bound_defensible") and revenue > 0 and bounds.get("HIGH_COST_BOUND") is not None:
        known = _d(bounds.get("KNOWN_COST"))
        high = _d(bounds.get("HIGH_COST_BOUND"))
        # Conservative: freight 10% + financing 3% on known+high
        freight = (known + high) * Decimal("0.10")
        fin = (known + high) * Decimal("0.03")
        worst_profit = _d(revenue) - known - high - freight - fin
        if worst_profit > 0 and mat_cov >= 0.5:
            return {
                "economics_status": ECONOMICS_READY,
                "path": "C_worst_case_still_profitable",
                "material_coverage": mat_cov,
                "worst_case_profit": _money(worst_profit),
            }

    return {
        "economics_status": ECONOMICS_NOT_READY,
        "path": None,
        "material_coverage": mat_cov,
        "reason": "insufficient_material_basket_evidence",
    }


def estimate_freight(acquisition: Decimal) -> dict[str, Any]:
    if acquisition <= 0:
        return {"freight": 0.0, "freight_status": "FREIGHT_UNKNOWN", "note": "No acquisition base"}
    # Conservative 8–12% without public freight confirmation
    est = acquisition * Decimal("0.10")
    return {
        "freight": _money(est),
        "freight_status": "FREIGHT_ESTIMATED_CONSERVATIVE",
        "note": "Conservative 10% freight reserve — not free shipping.",
    }


def assess_financing(opportunity_id: str, *, revenue: float, acquisition: float, freight: float) -> dict[str, Any]:
    try:
        from financing_intelligence.assess import assess_opportunity_financing

        card = assess_opportunity_financing(
            opportunity_id=opportunity_id,
            contract_value=revenue,
            supplier_cost=acquisition,
            freight=freight,
            persist=False,
        )
        owner_cash = float(card.get("owner_cash_required") or card.get("company_capital_required_to_close") or 0)
        fin_cost = 0.0
        best = card.get("best_stack") or {}
        try:
            fin_cost = float(best.get("financing_cost") or best.get("estimated_financing_cost") or 0)
        except Exception:
            fin_cost = 0.0
        if fin_cost <= 0 and acquisition > 0:
            fin_cost = round(acquisition * 0.03, 2)
        status = "FINANCEABLE_MODELED"
        if owner_cash > 0:
            status = "FINANCING_BLOCKED"
        return {
            "financing_status": status,
            "owner_cash_required": owner_cash,
            "financing_cost": fin_cost,
            "amount_requiring_financing": acquisition + freight,
            "plain_status": card.get("plain_status"),
            "blockers": card.get("blockers") or [],
        }
    except Exception as exc:
        fin_cost = round(acquisition * 0.03, 2) if acquisition > 0 else 0.0
        return {
            "financing_status": "FINANCEABLE_MODELED",
            "owner_cash_required": 0.0,
            "financing_cost": fin_cost,
            "amount_requiring_financing": acquisition + freight,
            "plain_status": "Modeled financing (fallback)",
            "blockers": [],
            "error": str(exc)[:160],
        }


def compute_strict_economics(
    opportunity_id: str,
    *,
    lines: list[dict[str, Any]],
    coverage: dict[str, Any],
    basket: dict[str, Any],
    revenue: float,
    revenue_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bounds = conservative_unresolved_bounds(lines)
    ready = assess_economics_readiness(coverage=coverage, lines=lines, bounds=bounds, revenue=revenue)

    priced = [l for l in lines if l.get("terminal_state") == PRICED_EXECUTABLE and l.get("unit_cost") is not None]
    acquisition = sum(_d(l.get("unit_cost")) * _d(l.get("quantity") or 1) for l in priced)
    freight_info = estimate_freight(acquisition)
    freight = _d(freight_info.get("freight"))
    fin = assess_financing(
        opportunity_id,
        revenue=float(revenue or 0),
        acquisition=_money(acquisition),
        freight=_money(freight),
    )
    fin_cost = _d(fin.get("financing_cost"))
    other = Decimal("0")
    total_cost = acquisition + freight + fin_cost + other
    rev = _d(revenue)
    net = rev - total_cost
    margin = (net / rev * Decimal("100")) if rev > 0 else Decimal("0")

    econ_status = ready.get("economics_status")
    profit_confidence = PROFIT_UNPROVEN
    economic_label = ECONOMICS_NOT_READY

    if econ_status == ECONOMICS_NOT_READY:
        profit_confidence = PROFIT_UNPROVEN
        economic_label = ECONOMICS_NOT_READY
        # Do NOT emit positive actionable profit
        display_profit = None
        actionable_profit = False
    else:
        economic_label = ECONOMICS_READY
        if rev <= 0:
            profit_confidence = PROFIT_UNPROVEN
            display_profit = None
            actionable_profit = False
        elif net < 0:
            profit_confidence = UNPROFITABLE
            display_profit = _money(net)
            actionable_profit = False
        else:
            mat_cov = float(coverage.get("MATERIAL_VALUE_COVERAGE") or 0)
            if mat_cov >= 0.9 and basket.get("basket_class") == BASKET_READY_PUBLIC_PRICE and net >= 5000:
                profit_confidence = PROFIT_PROVEN
            elif mat_cov >= 0.75 and bounds.get("unresolved_material_lines", 1) <= 2:
                profit_confidence = PROFIT_LIKELY
            elif mat_cov >= 0.5:
                profit_confidence = PROFIT_POSSIBLE
            else:
                profit_confidence = PROFIT_UNPROVEN
            display_profit = _money(net) if profit_confidence != PROFIT_UNPROVEN else None
            actionable_profit = profit_confidence in {PROFIT_PROVEN, PROFIT_LIKELY} and _money(net) > 0

    return {
        "opportunity_id": opportunity_id,
        "economics_status": economic_label,
        "economics_path": ready.get("path"),
        "profit_confidence": profit_confidence,
        "government_revenue": _money(rev) if rev > 0 else None,
        "revenue_evidence": revenue_evidence,
        "product_acquisition_cost": _money(acquisition),
        "freight": _money(freight),
        "freight_status": freight_info.get("freight_status"),
        "financing_cost": _money(fin_cost),
        "financing_status": fin.get("financing_status"),
        "owner_cash_required": fin.get("owner_cash_required"),
        "other_fees": _money(other),
        "total_execution_cost": _money(total_cost),
        "expected_profit": display_profit,
        "margin_pct": float(margin.quantize(Decimal("0.1"))) if display_profit is not None else None,
        "unresolved_cost_exposure": bounds.get("HIGH_COST_BOUND") or bounds.get("UNRESOLVED_MATERIAL_VALUE"),
        "bounds": bounds,
        "actionable_profit": actionable_profit,
        "basket_class": basket.get("basket_class"),
        "material_coverage": coverage.get("MATERIAL_VALUE_COVERAGE"),
        "financing_detail": fin,
        "headline": (
            "PROFIT NOT YET PROVEN"
            if economic_label == ECONOMICS_NOT_READY or profit_confidence == PROFIT_UNPROVEN
            else f"Expected profit ${_money(display_profit or 0):,.2f}"
        ),
    }

"""Freight, financing, and terminal economics for basket-ready opportunities."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from basket_full_funnel_reconcile.models import (
    BASKET_BLOCKED,
    BASKET_READY_PUBLIC_PRICE,
    BASKET_READY_QUOTE_DEPENDENT,
    ECON_EXECUTION_BLOCKED,
    FINANCEABLE_MODELED,
    FINANCEABLE_PUBLIC_TERMS,
    FINANCING_BLOCKED,
    FINANCING_UNKNOWN,
    FREIGHT_ESTIMATED_CONSERVATIVE,
    FREIGHT_QUOTE_REQUIRED,
    LIKELY_PROFITABLE,
    POSSIBLE_PROFIT,
    PRICED_EXECUTABLE,
    PROVEN_PROFITABLE,
    PUBLIC_FREIGHT_CONFIRMED,
    QUOTE_REQUIRED,
    RESEARCH_EXHAUSTED,
    SUPPLIER_TERMS_REQUIRED,
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


def classify_basket_ready(line_result: dict[str, Any]) -> dict[str, Any]:
    lines = line_result.get("lines") or []
    total = int(line_result.get("TOTAL_LINES") or 0)
    if total == 0:
        return {"basket_class": BASKET_BLOCKED, "reason": "no_lines"}

    terminals = {r.get("terminal_state") for r in lines}
    all_terminal = all(r.get("terminal_state") for r in lines)
    priced_n = int(line_result.get("PRICED_LINES") or 0)
    quote_n = int(line_result.get("QUOTE_REQUIRED_LINES") or 0)
    line_cov = float(line_result.get("line_coverage") or 0)
    value_cov = float(line_result.get("value_weighted_coverage") or 0)

    # Material lines = all non-NOT_REQUIRED
    material = [r for r in lines if r.get("terminal_state") != "NOT_REQUIRED_FOR_BASKET"]
    material_priced = [r for r in material if r.get("terminal_state") == PRICED_EXECUTABLE]
    material_quote = [r for r in material if r.get("terminal_state") == QUOTE_REQUIRED]
    # Exhausted public-price search is terminal for basket purposes
    exhausted = {"PUBLIC_PRICE_UNAVAILABLE", "NO_COMPLIANT_SOURCE", "IDENTITY_AMBIGUOUS"}
    material_open = [
        r
        for r in material
        if r.get("terminal_state")
        not in {
            PRICED_EXECUTABLE,
            QUOTE_REQUIRED,
            "NOT_REQUIRED_FOR_BASKET",
            "PUBLIC_PRICE_UNAVAILABLE",
            "NO_COMPLIANT_SOURCE",
            "IDENTITY_AMBIGUOUS",
            "WRONG_OR_MISSING_PACK",
            "UOM_UNRESOLVED",
            "CONDITION_UNRESOLVED",
            "EXECUTION_BLOCKED",
        }
    ]

    if all_terminal and not material_open and priced_n == total:
        return {
            "basket_class": BASKET_READY_PUBLIC_PRICE,
            "reason": "all_required_lines_priced",
            "line_coverage": line_cov,
            "value_coverage": value_cov,
        }
    if all_terminal and not material_open and material_quote and material_priced:
        return {
            "basket_class": BASKET_READY_QUOTE_DEPENDENT,
            "reason": "remaining_lines_explicit_quote_required",
            "line_coverage": line_cov,
            "value_coverage": value_cov,
            "quote_lines": quote_n,
        }
    if line_cov >= 0.9 and all_terminal and not material_open:
        return {
            "basket_class": BASKET_READY_PUBLIC_PRICE,
            "reason": "ge_90_coverage_all_terminal",
            "line_coverage": line_cov,
            "value_coverage": value_cov,
        }
    if line_cov >= 0.75 and all_terminal and not material_open and (material_quote or any(r.get("terminal_state") in exhausted for r in material)):
        return {
            "basket_class": BASKET_READY_QUOTE_DEPENDENT if material_quote else BASKET_READY_PUBLIC_PRICE,
            "reason": "ge_75_coverage_remainder_terminal_exhausted",
            "line_coverage": line_cov,
            "value_coverage": value_cov,
        }
    if all_terminal and not material_open and priced_n >= max(1, int(total * 0.75)) and quote_n:
        return {
            "basket_class": BASKET_READY_QUOTE_DEPENDENT,
            "reason": "high_coverage_with_modeled_quote_remainder",
            "line_coverage": line_cov,
            "value_coverage": value_cov,
        }
    return {
        "basket_class": BASKET_BLOCKED,
        "reason": "insufficient_terminal_coverage" if all_terminal else "open_nonterminal_or_unresolved",
        "line_coverage": line_cov,
        "value_coverage": value_cov,
        "open_lines": len(material_open),
    }


def estimate_freight(line_result: dict[str, Any], corpus_row: dict[str, Any]) -> dict[str, Any]:
    from line_item_economics.freight import build_freight_block
    from cost_intelligence import freight_range_from_cost

    lines = line_result.get("lines") or []
    priced = [r for r in lines if r.get("terminal_state") == PRICED_EXECUTABLE]
    acq = sum(_d(r.get("unit_cost")) * _d(r.get("quantity") or 1) for r in priced)
    dest = next((r.get("delivery_destination") for r in lines if r.get("delivery_destination")), None)
    fob = next((r.get("freight_terms") for r in lines if r.get("freight_terms")), None)

    block = build_freight_block(
        delivery_location=dest,
        fob_terms=fob,
        line_count=len(lines),
        supplier_included_freight=False,
    )
    # Conservative estimate from cost base when unknown
    if block.get("estimated_freight") is None and acq > 0:
        try:
            fr = freight_range_from_cost(cost_base=float(acq), fob_terms=fob, freight_included=False)
            low = _d((fr or {}).get("low") or (fr or {}).get("min") or 0)
            high = _d((fr or {}).get("high") or (fr or {}).get("max") or low)
            # Use high end as conservative
            est = high if high > 0 else (acq * Decimal("0.08"))
            status = FREIGHT_ESTIMATED_CONSERVATIVE
            block["estimated_freight"] = _money(est)
            block["freight_known"] = False
            block["note"] = "Conservative freight estimate — not free shipping assumed."
        except Exception:
            est = acq * Decimal("0.10")
            status = FREIGHT_ESTIMATED_CONSERVATIVE
            block["estimated_freight"] = _money(est)
            block["note"] = "Fallback 10% freight reserve."
    elif block.get("estimated_freight") is not None and block.get("freight_known"):
        status = PUBLIC_FREIGHT_CONFIRMED
    else:
        status = FREIGHT_QUOTE_REQUIRED
        if block.get("estimated_freight") is None:
            block["estimated_freight"] = _money(acq * Decimal("0.12")) if acq > 0 else None
            block["note"] = "Freight quote required; reserve applied for economics modeling."

    return {
        "freight_status": status,
        "fob_terms": fob,
        "shipping_destination": dest,
        "estimated_freight": block.get("estimated_freight"),
        "mode": block.get("mode"),
        "block": block,
    }


def assess_financing(
    opportunity_id: str,
    *,
    revenue: float,
    acquisition: float,
    freight: float,
) -> dict[str, Any]:
    try:
        from financing_intelligence.assess import assess_opportunity_financing

        card = assess_opportunity_financing(
            opportunity_id=opportunity_id,
            contract_value=revenue,
            supplier_cost=acquisition,
            freight=freight,
            persist=False,
        )
        status = str(card.get("status") or FINANCING_UNKNOWN)
        mapped = FINANCING_UNKNOWN
        if "LIKELY_FINANCEABLE" in status or "CAPITAL_STACK_CLOSED" in status:
            mapped = FINANCEABLE_MODELED
        elif "SUPPLIER_TERMS" in status:
            mapped = SUPPLIER_TERMS_REQUIRED
        elif "GAP" in status or "EXECUTION_FAIL" in status or "BLOCK" in status:
            mapped = FINANCING_BLOCKED
        elif "APPROVAL" in status:
            mapped = "PO_FINANCING_REQUIRED"
        elif "PUBLIC" in status:
            mapped = FINANCEABLE_PUBLIC_TERMS
        else:
            mapped = FINANCEABLE_MODELED if card.get("best_stack") else FINANCING_UNKNOWN

        # Hard constraint: owner/company prepay cash required initially must be $0 for FINANCEABLE_PUBLIC
        owner_cash = float(card.get("owner_cash_required") or card.get("company_capital_required_to_close") or 0)
        fin_cost = 0.0
        best = card.get("best_stack") or {}
        try:
            fin_cost = float(best.get("financing_cost") or best.get("estimated_financing_cost") or 0)
        except Exception:
            fin_cost = 0.0
        # Conservative modeled financing cost if unknown: 3% of acquisition
        if fin_cost <= 0 and acquisition > 0 and mapped in {FINANCEABLE_MODELED, FINANCEABLE_PUBLIC_TERMS}:
            fin_cost = round(acquisition * 0.03, 2)

        return {
            "financing_status": mapped,
            "raw_status": status,
            "owner_cash_required": owner_cash,
            "financing_cost": fin_cost,
            "plain_status": card.get("plain_status"),
            "blockers": card.get("blockers") or [],
            "card": {k: card.get(k) for k in ("status", "plain_status", "status_means", "owner_cash_required") if k in card},
        }
    except Exception as exc:
        # Conservative modeled fallback — no owner cash assumed required
        fin_cost = round(acquisition * 0.03, 2) if acquisition > 0 else 0.0
        return {
            "financing_status": FINANCEABLE_MODELED,
            "raw_status": "FALLBACK_MODELED",
            "owner_cash_required": 0.0,
            "financing_cost": fin_cost,
            "plain_status": "Modeled financing",
            "blockers": [],
            "error": str(exc)[:200],
        }


def compute_economics(
    opportunity_id: str,
    corpus_row: dict[str, Any],
    line_result: dict[str, Any],
    basket: dict[str, Any],
) -> dict[str, Any]:
    lines = line_result.get("lines") or []
    priced = [r for r in lines if r.get("terminal_state") == PRICED_EXECUTABLE]

    acquisition = sum(_d(r.get("unit_cost")) * _d(r.get("quantity") or 1) for r in priced)

    # Revenue: only use full contract revenue when basket coverage is material.
    # Low coverage + full contract value produces dishonest profit.
    rev_ev = (corpus_row.get("revenue_evidence") or {})
    existing = (corpus_row.get("existing_economics") or {})
    line_cov = Decimal(str(line_result.get("line_coverage") or 0))
    rev_val = _d(rev_ev.get("value"))
    prior_profit = _d(existing.get("profit"))
    prior_acq = _d((corpus_row.get("acquisition_evidence") or {}).get("acq_sum"))

    if line_cov < Decimal("0.75"):
        # Attribute only priced-subset economics; optionally keep prior known profit.
        if prior_profit > 0 and prior_acq > 0 and acquisition > 0 and line_cov >= Decimal("0.25"):
            # Blend: do not import full contract value
            rev_val = acquisition + min(prior_profit, acquisition * Decimal("5"))
        elif acquisition > 0:
            rev_val = acquisition * Decimal("1.15")
        else:
            rev_val = Decimal("0")
    else:
        if rev_val <= 0 and prior_profit > 0 and prior_acq > 0:
            rev_val = prior_acq + prior_profit
        if rev_val <= 0 and acquisition > 0:
            if _d(rev_ev.get("value")) > 0:
                rev_val = _d(rev_ev.get("value")) * max(line_cov, Decimal("0.75"))
            else:
                rev_val = acquisition * Decimal("1.15")
        # Guard: never let acquisition exceed revenue by absurd multiples of junk unit prices
        if acquisition > 0 and rev_val > 0 and acquisition > rev_val * Decimal("20"):
            acquisition = rev_val / Decimal("1.15")

    freight_info = estimate_freight(line_result, corpus_row)
    freight = _d(freight_info.get("estimated_freight"))
    fin = assess_financing(
        opportunity_id,
        revenue=_money(rev_val),
        acquisition=_money(acquisition),
        freight=_money(freight),
    )
    fin_cost = _d(fin.get("financing_cost"))
    other_fees = Decimal("0")
    total_cost = acquisition + freight + fin_cost + other_fees
    gross = rev_val - acquisition
    net = rev_val - total_cost
    margin = (net / rev_val * Decimal("100")) if rev_val > 0 else Decimal("0")

    basket_class = basket.get("basket_class")
    line_cov = float(line_result.get("line_coverage") or 0)

    # Terminal economic state — BOTH_SIDES is not allowed as final
    if basket_class == BASKET_BLOCKED and line_cov < 0.25:
        terminal = RESEARCH_EXHAUSTED
    elif basket_class == BASKET_BLOCKED:
        terminal = ECON_EXECUTION_BLOCKED if line_cov < 0.5 else RESEARCH_EXHAUSTED
    elif fin.get("financing_status") == FINANCING_BLOCKED and net > 0:
        terminal = ECON_EXECUTION_BLOCKED
    elif net < 0:
        terminal = UNPROFITABLE
    elif basket_class == BASKET_READY_PUBLIC_PRICE and line_cov >= 0.9 and net >= 5000:
        terminal = PROVEN_PROFITABLE
    elif basket_class in {BASKET_READY_PUBLIC_PRICE, BASKET_READY_QUOTE_DEPENDENT} and net > 0 and line_cov >= 0.5:
        terminal = LIKELY_PROFITABLE if line_cov >= 0.75 else POSSIBLE_PROFIT
    elif net > 0:
        terminal = POSSIBLE_PROFIT
    else:
        terminal = UNPROFITABLE

    # Preserve known prior economics when acquisition-scale already had a defensible profit.
    prior_status = existing.get("profit_status")
    if prior_profit > 0 and prior_status in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE", "POSSIBLE_PROFIT"}:
        if basket_class in {BASKET_READY_PUBLIC_PRICE, BASKET_READY_QUOTE_DEPENDENT} or line_cov >= Decimal("0.4"):
            net = prior_profit
            if acquisition <= 0 and prior_acq > 0:
                acquisition = prior_acq
            if rev_val <= acquisition:
                rev_val = acquisition + net + freight + fin_cost
            total_cost = acquisition + freight + fin_cost + other_fees
            gross = rev_val - acquisition
            margin = (net / rev_val * Decimal("100")) if rev_val > 0 else Decimal("0")
            if prior_status == "PROVEN_PROFITABLE" and line_cov >= Decimal("0.9"):
                terminal = PROVEN_PROFITABLE
            elif float(prior_profit) >= 1000:
                terminal = LIKELY_PROFITABLE
            else:
                terminal = POSSIBLE_PROFIT
    elif prior_profit > 0 and terminal in {POSSIBLE_PROFIT, LIKELY_PROFITABLE, RESEARCH_EXHAUSTED}:
        if prior_status in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE", "POSSIBLE_PROFIT"}:
            if float(prior_profit) >= 5000 and line_cov >= Decimal("0.4"):
                terminal = LIKELY_PROFITABLE if terminal != PROVEN_PROFITABLE else PROVEN_PROFITABLE
                net = prior_profit
                if acquisition > 0 and rev_val <= acquisition:
                    rev_val = acquisition + net + freight + fin_cost

    return {
        "opportunity_id": opportunity_id,
        "government_revenue": _money(rev_val),
        "product_acquisition_cost": _money(acquisition),
        "freight": _money(freight),
        "freight_status": freight_info.get("freight_status"),
        "financing_cost": _money(fin_cost),
        "financing_status": fin.get("financing_status"),
        "other_fees": _money(other_fees),
        "total_execution_cost": _money(total_cost),
        "gross_profit": _money(gross),
        "net_expected_profit": _money(net),
        "margin_pct": float(margin.quantize(Decimal("0.1"))),
        "economic_terminal": terminal,
        "basket_class": basket_class,
        "owner_cash_required": fin.get("owner_cash_required"),
        "freight_detail": freight_info,
        "financing_detail": {k: fin.get(k) for k in ("financing_status", "raw_status", "plain_status", "owner_cash_required", "blockers")},
    }

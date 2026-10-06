"""Opportunity aggregation, basket economics, lender/near-ready classification."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from scale_evidence_profit.models import (
    BASKET_READY_COVERAGE,
    BOTH_SIDES_READY,
    COMPLETE_COVERAGE,
    EXECUTION_BLOCKED,
    EXECUTION_READY,
    EXECUTION_REVIEW,
    LENDER_READY,
    MAJORITY_COVERAGE,
    NEAR_READY_24H,
    OPPORTUNITY_BOTH_SIDES_COMPLETE,
    OPPORTUNITY_BOTH_SIDES_MAJORITY,
    OPPORTUNITY_BOTH_SIDES_NONE,
    OPPORTUNITY_BOTH_SIDES_PARTIAL,
)


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def aggregate_opportunity(
    opportunity_id: str,
    line_results: list[dict[str, Any]],
    *,
    total_purchasing_lines: int | None = None,
) -> dict[str, Any]:
    """Roll line evidence into opportunity both-sides + basket coverage."""
    n_ident = len(line_results)
    n_total = total_purchasing_lines or n_ident or 1
    gov_n = sum(1 for r in line_results if (r.get("government_value") or {}).get("status") == "FOUND")
    cost_n = sum(1 for r in line_results if (r.get("public_cost") or {}).get("status") == "FOUND")
    both_n = sum(1 for r in line_results if r.get("line_status") == BOTH_SIDES_READY)

    # Value-weighted coverage using gov extended or qty*unit
    material_total = 0.0
    material_both = 0.0
    for r in line_results:
        ident = r.get("identity") or {}
        qty = _f(ident.get("quantity")) or 1.0
        gov = (r.get("government_value") or {}).get("evidence") or {}
        cost = (r.get("public_cost") or {}).get("evidence") or {}
        unit = _f(gov.get("awarded_unit_price")) or _f(cost.get("unit_price")) or 0.0
        ext = qty * unit
        material_total += ext
        if r.get("line_status") == BOTH_SIDES_READY:
            material_both += ext

    line_cov = both_n / n_total if n_total else 0.0
    # Value coverage only among lines that have any price signal; do not let
    # zero-priced unknowns vanish from the denominator for "complete" claims.
    priced_n = sum(
        1
        for r in line_results
        if (r.get("government_value") or {}).get("status") == "FOUND"
        or (r.get("public_cost") or {}).get("status") == "FOUND"
    )
    value_cov = (material_both / material_total) if material_total > 0 else 0.0
    # Material coverage for readiness = LINE coverage (honest basket completeness)
    material_cov = line_cov

    if both_n == 0:
        bs_class = OPPORTUNITY_BOTH_SIDES_NONE
    elif material_cov >= COMPLETE_COVERAGE:
        bs_class = OPPORTUNITY_BOTH_SIDES_COMPLETE
    elif material_cov >= MAJORITY_COVERAGE:
        bs_class = OPPORTUNITY_BOTH_SIDES_MAJORITY
    else:
        bs_class = OPPORTUNITY_BOTH_SIDES_PARTIAL

    basket_ready = material_cov >= BASKET_READY_COVERAGE and both_n >= 2
    complete = material_cov >= COMPLETE_COVERAGE

    return {
        "opportunity_id": opportunity_id,
        "total_purchasing_lines": n_total,
        "usable_identities": n_ident,
        "gov_value_lines": gov_n,
        "cost_known_lines": cost_n,
        "both_sides_lines": both_n,
        "line_coverage_pct": round(line_cov * 100, 1),
        "value_coverage_pct": round(value_cov * 100, 1),
        "material_coverage_pct": round(material_cov * 100, 1),
        "both_sides_class": bs_class,
        "basket_ready": basket_ready,
        "complete_basket": complete,
        "material_gov_value": round(material_total, 2) if material_total else None,
        "material_both_value": round(material_both, 2) if material_both else None,
        "updated_at": now_utc().isoformat(),
    }


def compute_basket_economics(line_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum both-sides lines into revenue/cost; no invented freight/financing."""
    revenue = 0.0
    cost = 0.0
    priced = 0
    for r in line_results:
        if r.get("line_status") != BOTH_SIDES_READY:
            continue
        ident = r.get("identity") or {}
        qty = _f(ident.get("quantity")) or 1.0
        gov = (r.get("government_value") or {}).get("evidence") or {}
        acq = (r.get("public_cost") or {}).get("evidence") or {}
        gu = _f(gov.get("awarded_unit_price"))
        cu = _f(acq.get("unit_price"))
        if gu is None or cu is None:
            continue
        revenue += gu * qty
        cost += cu * qty
        priced += 1

    spread = revenue - cost if priced else None
    margin = (spread / revenue * 100.0) if revenue and spread is not None else None
    # Conservative financing: 3% of product cost as placeholder range mid (existing model often uses explicit)
    financing = round(cost * 0.03, 2) if cost else None
    # Freight unknown → review required when spread exists
    freight = None
    freight_status = "FREIGHT_REVIEW_REQUIRED" if priced else None
    # Net after financing (freight unknown → do not subtract invented freight)
    expected_profit = None
    if spread is not None:
        expected_profit = spread - (financing or 0.0)

    revenue_basis = "LINE_BY_LINE_HISTORICAL_GOV_VALUE" if priced else None
    cost_basis = "PUBLIC_VENDOR_BID_REFERENCE" if priced else None

    return {
        "lines_priced": priced,
        "expected_revenue": round(revenue, 2) if priced else None,
        "product_cost": round(cost, 2) if priced else None,
        "freight": freight,
        "freight_status": freight_status,
        "financing_cost": financing,
        "financing_note": "conservative_3pct_of_product_cost" if financing else None,
        "expected_profit": round(expected_profit, 2) if expected_profit is not None else None,
        "margin_pct": round(margin, 2) if margin is not None else None,
        "revenue_basis": revenue_basis,
        "acquisition_cost_basis": cost_basis,
        "spread_before_freight_financing": round(spread, 2) if spread is not None else None,
    }


def classify_execution(agg: dict[str, Any], econ: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    if not agg.get("basket_ready") and not agg.get("complete_basket"):
        if int(agg.get("both_sides_lines") or 0) == 0:
            blockers.append("NO_BOTH_SIDES")
        else:
            blockers.append("BASKET_INCOMPLETE")
    if econ.get("freight_status") == "FREIGHT_REVIEW_REQUIRED":
        # minor — review not hard block for NEAR_READY
        pass
    if econ.get("expected_profit") is not None and econ["expected_profit"] <= 0:
        blockers.append("NONPOSITIVE_SPREAD")

    if "NO_BOTH_SIDES" in blockers or "NONPOSITIVE_SPREAD" in blockers:
        status = EXECUTION_BLOCKED
    elif "BASKET_INCOMPLETE" in blockers:
        status = EXECUTION_REVIEW
    elif agg.get("basket_ready") or agg.get("complete_basket"):
        status = EXECUTION_READY
    else:
        status = EXECUTION_REVIEW

    return {"status": status, "blockers": blockers}


def classify_pipeline(
    agg: dict[str, Any],
    econ: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any]:
    """LENDER_READY / NEAR_READY_24H / reserve."""
    profit = _f(econ.get("expected_profit"))
    both = int(agg.get("both_sides_lines") or 0)
    mat = float(agg.get("material_coverage_pct") or 0) / 100.0
    freight_review = econ.get("freight_status") == "FREIGHT_REVIEW_REQUIRED"

    research_next = None
    if both == 0:
        research_next = "FIND_PRIOR_AWARD"
    elif mat < BASKET_READY_COVERAGE:
        research_next = "PRICE_REMAINING_LINES"
    elif freight_review:
        research_next = "CONFIRM_FREIGHT"
    elif profit is not None and profit <= 0:
        research_next = "GET_RETAIL_PRICE"
    else:
        research_next = "VERIFY_ELIGIBILITY"

    # Profit status
    if profit is None:
        profit_status = "UNPROVEN"
    elif profit <= 0:
        profit_status = "UNPROFITABLE"
    elif mat >= COMPLETE_COVERAGE and not freight_review and execution.get("status") == EXECUTION_READY:
        profit_status = "PROVEN_PROFITABLE"
    elif mat >= BASKET_READY_COVERAGE and profit >= 1000:
        profit_status = "LIKELY_PROFITABLE"
    else:
        profit_status = "POSSIBLE_PROFIT"

    lender = False
    near = False
    # LENDER_READY: majority basket, positive profit, execution not hard-blocked, both sides
    if (
        both >= 2
        and mat >= BASKET_READY_COVERAGE
        and profit is not None
        and profit > 0
        and execution.get("status") != EXECUTION_BLOCKED
        and "NONPOSITIVE_SPREAD" not in (execution.get("blockers") or [])
    ):
        # Allow freight review as minor if profit still healthy after conservative cushion
        if not freight_review or profit >= 2500:
            lender = True

    # NEAR_READY: positive economics, usable identity, <=2 missing actions
    # PRICE_REMAINING_LINES only counts as a 24h action when nearly complete.
    n_total = int(agg.get("total_purchasing_lines") or 0) or 1
    remaining_lines = max(0, n_total - both)
    near_priceable = mat >= 0.75 or remaining_lines <= 2

    missing_actions = []
    if mat < BASKET_READY_COVERAGE:
        if near_priceable:
            missing_actions.append("PRICE_REMAINING_LINES")
        else:
            missing_actions.append("PRICE_REMAINING_LINES")
            missing_actions.append("EXPAND_BASKET_COVERAGE")  # structural — not 24h
    if freight_review:
        missing_actions.append("CONFIRM_FREIGHT")
    if execution.get("status") == EXECUTION_REVIEW and "BASKET_INCOMPLETE" in (execution.get("blockers") or []):
        if "PRICE_REMAINING_LINES" not in missing_actions:
            missing_actions.append("PRICE_REMAINING_LINES")

    if (
        not lender
        and profit is not None
        and profit > 0
        and both >= 1
        and near_priceable
        and len(missing_actions) <= 2
        and execution.get("status") != EXECUTION_BLOCKED
    ):
        near = True

    readiness = LENDER_READY if lender else (NEAR_READY_24H if near else None)
    return {
        "readiness": readiness,
        "profit_status": profit_status,
        "research_next": research_next,
        "missing_actions": missing_actions,
        "time_to_ready_estimate": (
            "ready_now"
            if lender
            else ("<24h" if near else ("1-3d" if both >= 1 else "unknown"))
        ),
        "lender_ready": lender,
        "near_ready_24h": near,
    }


def build_lender_packet(
    opportunity_id: str,
    agg: dict[str, Any],
    econ: dict[str, Any],
    pipeline: dict[str, Any],
    line_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Structured lender fields — no document generation."""
    sample = next((r for r in line_results if r.get("line_status") == BOTH_SIDES_READY), None)
    links = []
    for r in line_results:
        for side in ("government_value", "public_cost"):
            for p in ((r.get(side) or {}).get("provenance") or []):
                if p.get("url") and p["url"] not in links:
                    links.append(p["url"])
    buyer = None
    if sample:
        buyer = ((sample.get("government_value") or {}).get("evidence") or {}).get("historical_buyer")
    parts = opportunity_id.split(":")
    source = parts[0] if parts else "unknown"
    return {
        "opportunity_id": opportunity_id,
        "buyer": buyer or (parts[1] if len(parts) > 1 else None),
        "solicitation": opportunity_id,
        "source": source,
        "close_date": None,
        "what_buying": (sample or {}).get("identity", {}).get("description") if sample else None,
        "historical_government_value": econ.get("expected_revenue"),
        "historical_evidence_basis": econ.get("revenue_basis"),
        "current_acquisition_cost": econ.get("product_cost"),
        "cost_source": econ.get("acquisition_cost_basis"),
        "both_sides_lines": agg.get("both_sides_lines"),
        "material_coverage_pct": agg.get("material_coverage_pct"),
        "expected_revenue": econ.get("expected_revenue"),
        "product_cost": econ.get("product_cost"),
        "freight": econ.get("freight"),
        "freight_status": econ.get("freight_status"),
        "financing_requirement": econ.get("product_cost"),
        "estimated_financing_cost": econ.get("financing_cost"),
        "expected_profit": econ.get("expected_profit"),
        "margin_pct": econ.get("margin_pct"),
        "profit_status": pipeline.get("profit_status"),
        "readiness": pipeline.get("readiness"),
        "research_next": pipeline.get("research_next"),
        "execution_risks": [],
        "evidence_links": links[:12],
        "updated_at": now_utc().isoformat(),
    }


def run_profit_first_handoff(
    opportunity_id: str,
    econ: dict[str, Any],
    title: str | None = None,
) -> dict[str, Any]:
    """Call existing profit_first without changing its math."""
    try:
        from profit_first.router import evaluate_opportunity_profit
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    try:
        pev = evaluate_opportunity_profit(
            opportunity_id=opportunity_id,
            title=title,
            expected_revenue=econ.get("expected_revenue"),
            product_cost=econ.get("product_cost"),
            freight=econ.get("freight"),
            financing=econ.get("financing_cost"),
        )
        return {"ok": True, "result": pev}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}

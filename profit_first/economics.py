"""Unified single-line and multi-line expected-profit calculation."""

from __future__ import annotations

from typing import Any

from profit_first.config import (
    EXECUTION_BLOCKED,
    LIKELY_PROFITABLE,
    POSSIBLE_PROFIT,
    PROFITABLE_AT_DISTRIBUTOR_COST,
    PROFITABLE_AT_PUBLIC_RETAIL,
    PROFITABLE_AT_QUOTED_COST,
    PROFITABLE_WITH_PERMITTED_EQUAL,
    PROVEN_PROFITABLE,
    UNPROFITABLE,
    UNPROVEN,
    load_targets_from_store,
)


def _f(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() in {"UNKNOWN", "NONE", "NULL"}:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _grade_rank(g: str | None) -> int:
    return {"A": 4, "B": 3, "C": 2, "D": 1}.get(str(g or "").upper(), 0)


def compute_expected_profit(
    *,
    expected_revenue: float | None,
    product_cost: float | None,
    freight: float | None = None,
    financing: float | None = None,
    other_costs: float | None = None,
    price_basis: str | None = None,
    completeness_pct: float | None = None,
    evidence_grade: str | None = None,
    execution_pass: bool | None = None,
    execution_blockers: list[str] | None = None,
    targets: dict[str, Any] | None = None,
    unresolved_material: bool = False,
) -> dict[str, Any]:
    """
    expected revenue - product - freight - financing - other = expected profit.

    Unknown costs are NOT treated as zero unless explicitly verified zero.
    Missing freight/financing → profit marked incomplete (not invented).
    """
    targets = targets or load_targets_from_store()
    other = _f(other_costs) or 0.0
    rev = _f(expected_revenue)
    cost = _f(product_cost)
    fr = _f(freight)
    fin = _f(financing)

    missing: list[str] = []
    if rev is None:
        missing.append("GOVERNMENT_VALUE")
    if cost is None:
        missing.append("ACQUISITION_COST")

    gross_spread = (rev - cost) if (rev is not None and cost is not None) else None

    # Freight/financing: if unknown, do not claim post-cost profit as final
    freight_known = fr is not None
    financing_known = fin is not None
    if not freight_known:
        missing.append("FREIGHT")
    if not financing_known:
        missing.append("FINANCING")

    post_freight = None
    if gross_spread is not None and freight_known:
        post_freight = gross_spread - float(fr)
    elif gross_spread is not None and fr == 0:
        post_freight = gross_spread

    post_financing = None
    if post_freight is not None and financing_known:
        post_financing = post_freight - float(fin) - other
    elif post_freight is not None and fin == 0:
        post_financing = post_freight - other

    # Decision profit: prefer post-financing; else post-freight; else gross (with caveat)
    decision_profit = post_financing
    decision_basis = "post_financing"
    if decision_profit is None and post_freight is not None:
        decision_profit = post_freight
        decision_basis = "post_freight_financing_unknown"
    elif decision_profit is None and gross_spread is not None:
        decision_profit = gross_spread
        decision_basis = "gross_spread_costs_unknown"

    margin_pct = None
    if rev and decision_profit is not None and rev > 0:
        margin_pct = round(decision_profit / rev * 100.0, 2)

    # Price-basis signals (do not mix)
    signals: list[str] = []
    basis = str(price_basis or "").upper()
    profitable_enough = (
        decision_profit is not None
        and decision_profit >= float(targets["minimum_post_financing_profit_usd"])
        and (freight_known and financing_known)
    )
    if profitable_enough:
        if basis in {"PUBLIC_RETAIL", "RETAIL", "PUBLIC"}:
            signals.append(PROFITABLE_AT_PUBLIC_RETAIL)
        elif basis in {"QUOTED", "SUPPLIER_QUOTE", "QUOTE"}:
            signals.append(PROFITABLE_AT_QUOTED_COST)
        elif basis in {"DISTRIBUTOR", "WHOLESALE", "DISTRIBUTOR_COST"}:
            signals.append(PROFITABLE_AT_DISTRIBUTOR_COST)
        elif basis in {"PERMITTED_EQUAL", "OR_EQUAL", "EQUAL"}:
            signals.append(PROFITABLE_WITH_PERMITTED_EQUAL)

    # Optional margin gate — only if configured
    margin_ok = True
    min_m = targets.get("minimum_margin_pct")
    if min_m is not None and margin_pct is not None and margin_pct < float(min_m):
        # Large dollar can still pass — only fail margin if also below dollar target
        if decision_profit is None or decision_profit < float(targets["minimum_expected_profit_usd"]):
            margin_ok = False

    blockers = list(execution_blockers or [])
    if execution_pass is False or blockers:
        status = EXECUTION_BLOCKED
    elif rev is None or cost is None:
        status = UNPROVEN
    elif decision_profit is not None and decision_profit <= 0 and freight_known and financing_known:
        status = UNPROFITABLE
    elif decision_profit is not None and decision_profit < 0:
        status = UNPROFITABLE
    elif (
        decision_profit is not None
        and freight_known
        and financing_known
        and not unresolved_material
        and _grade_rank(evidence_grade) >= _grade_rank(targets.get("minimum_evidence_confidence"))
        and (completeness_pct or 0) >= float(targets.get("minimum_completeness_for_approval") or 80)
        and decision_profit >= float(targets["minimum_post_financing_profit_usd"])
        and margin_ok
    ):
        status = PROVEN_PROFITABLE
    elif (
        decision_profit is not None
        and decision_profit >= float(targets["minimum_post_financing_profit_usd"])
        and (completeness_pct or 0) >= 60
    ):
        status = LIKELY_PROFITABLE if (freight_known or (fr is not None)) else POSSIBLE_PROFIT
        if not freight_known or not financing_known or unresolved_material:
            status = LIKELY_PROFITABLE if decision_profit >= float(targets["minimum_expected_profit_usd"]) else POSSIBLE_PROFIT
    elif decision_profit is not None and decision_profit > 0:
        status = POSSIBLE_PROFIT
    else:
        status = UNPROVEN

    # If gross positive but freight unknown and could erase — POSSIBLE not PROVEN
    if status == PROVEN_PROFITABLE and not freight_known:
        status = LIKELY_PROFITABLE

    return {
        "expected_revenue": rev,
        "product_cost": cost,
        "freight": fr,
        "financing": fin,
        "other_costs": other if other else None,
        "gross_spread": round(gross_spread, 2) if gross_spread is not None else None,
        "post_freight_profit": round(post_freight, 2) if post_freight is not None else None,
        "post_financing_profit": round(post_financing, 2) if post_financing is not None else None,
        "expected_profit": round(decision_profit, 2) if decision_profit is not None else None,
        "decision_basis": decision_basis,
        "margin_pct": margin_pct,
        "price_basis": price_basis,
        "proof_signals": signals,
        "profit_status": status,
        "missing_facts": missing,
        "completeness_pct": completeness_pct,
        "evidence_grade": evidence_grade,
        "execution_pass": execution_pass,
        "execution_blockers": blockers,
        "targets_applied": {
            "minimum_expected_profit_usd": targets["minimum_expected_profit_usd"],
            "minimum_post_financing_profit_usd": targets["minimum_post_financing_profit_usd"],
            "minimum_margin_pct": targets.get("minimum_margin_pct"),
        },
        "note": "Gross spread is not final profit. Unknown costs are not zero.",
    }


def from_line_item_rollup(lie: dict[str, Any], *, targets: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map line_item_economics analysis into unified profit result."""
    roll = lie.get("rollup") or {}
    freight = roll.get("estimated_freight")
    financing = roll.get("financing_cost")
    unresolved_share = float(roll.get("unresolved_value_share") or 0)
    return compute_expected_profit(
        expected_revenue=roll.get("TOTAL_KNOWN_HISTORICAL_VALUE"),
        product_cost=roll.get("TOTAL_KNOWN_RETAIL_COST"),
        freight=freight if roll.get("freight_known") else freight,
        financing=financing if financing is not None else 0.0,
        other_costs=0.0,
        price_basis="PUBLIC_RETAIL",
        completeness_pct=roll.get("coverage_pct"),
        evidence_grade=roll.get("completeness_grade"),
        execution_pass=True,
        targets=targets,
        unresolved_material=unresolved_share > 0.15,
    )

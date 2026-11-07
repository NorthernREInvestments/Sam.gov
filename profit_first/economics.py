"""Unified profit calculation — delegates to verified_commercial_decision_v1.

Historical award totals are NEVER proposed bid revenue.
Unknown freight/financing are NEVER treated as zero.
execution_pass=True is never invented by adapters.
"""

from __future__ import annotations

from typing import Any

from profit_first.config import load_targets_from_store
from verified_commercial_decision_v1 import (
    COST_NOT_APPLICABLE,
    COST_REQUIRED_UNKNOWN,
    COST_VERIFIED,
    COST_VERIFIED_ZERO,
    decision_from_profit_first_inputs,
    evaluate_verified_commercial_decision,
    load_commercial_targets,
)


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
    proposed_bid_revenue: float | None = None,
    historical_award_value: float | None = None,
    financing_reason: str | None = None,
    financing_transaction_verified: bool = False,
    personal_cash_required_usd: float | None = 0.0,
    line_count_required: int | None = None,
    line_count_sourced: int | None = None,
    product_only: bool = True,
) -> dict[str, Any]:
    """
    Authoritative path: NET_PROFIT = proposed_bid − verified costs.

    Unknown costs are NOT treated as zero.
    Missing freight/financing → VERIFY (not PROVEN).
    """
    # Map legacy targets dict into commercial targets when provided
    vcd_targets = None
    if targets:
        vcd_targets = load_commercial_targets(
            {
                "minimum_expected_profit_usd": targets.get("minimum_expected_profit_usd")
                or targets.get("minimum_post_financing_profit_usd")
                or targets.get("bid_ready_profit_floor_usd"),
                "minimum_margin_pct": targets.get("minimum_net_margin")
                or (
                    (targets.get("minimum_margin_pct") / 100.0)
                    if targets.get("minimum_margin_pct") is not None
                    and float(targets.get("minimum_margin_pct")) > 1
                    else targets.get("minimum_margin_pct")
                ),
            }
        )

    out = decision_from_profit_first_inputs(
        expected_revenue=expected_revenue,
        product_cost=product_cost,
        freight=freight,
        financing=financing,
        other_costs=other_costs,
        price_basis=price_basis,
        completeness_pct=completeness_pct,
        evidence_grade=evidence_grade,
        execution_pass=execution_pass,
        execution_blockers=execution_blockers,
        unresolved_material=unresolved_material,
        historical_award_value=historical_award_value,
        proposed_bid_revenue=proposed_bid_revenue if proposed_bid_revenue is not None else expected_revenue,
        targets=vcd_targets,
        line_count_required=line_count_required,
        line_count_sourced=line_count_sourced,
        financing_transaction_verified=financing_transaction_verified,
        financing_reason=financing_reason
        or (
            "zero_financing_cost_with_documented_zero_cash_gap"
            if financing == 0
            else None
        ),
        personal_cash_required_usd=personal_cash_required_usd,
        product_only=product_only,
    )
    out["targets_applied"] = {
        "minimum_expected_profit_usd": out["targets_applied"]["minimum_net_profit_usd"],
        "minimum_post_financing_profit_usd": out["targets_applied"]["minimum_net_profit_usd"],
        "minimum_margin_pct": out["targets_applied"]["minimum_net_margin"] * 100.0,
    }
    out["gross_spread"] = None
    if out.get("proposed_bid_revenue") is not None and out.get("product_cost") is not None:
        out["gross_spread"] = round(float(out["proposed_bid_revenue"]) - float(out["product_cost"]), 2)
    out["post_freight_profit"] = None
    if out.get("gross_spread") is not None and out.get("freight") is not None:
        out["post_freight_profit"] = round(float(out["gross_spread"]) - float(out["freight"]), 2)
    out["note"] = (
        "Authoritative verified commercial decision. "
        "Gross spread is not final profit. Unknown costs are not zero. "
        "Historical awards are not proposed revenue."
    )
    return out


def from_line_item_rollup(lie: dict[str, Any], *, targets: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map line_item_economics analysis into unified profit result.

    CRITICAL: TOTAL_KNOWN_HISTORICAL_VALUE is supporting evidence only —
    it does NOT become proposed bid revenue.
    """
    roll = lie.get("rollup") or {}
    freight = roll.get("estimated_freight")
    financing = roll.get("financing_cost")
    unresolved_share = float(roll.get("unresolved_value_share") or 0)

    # Explicit proposed bid only — never historical total
    proposed = roll.get("proposed_bid_revenue") or roll.get("COMPANY_PROPOSED_BID_PRICE")
    historical = roll.get("TOTAL_KNOWN_HISTORICAL_VALUE")

    line_required = roll.get("line_count") or roll.get("lines_total")
    line_sourced = roll.get("lines_with_acquisition") or roll.get("priced_lines")

    freight_known = bool(roll.get("freight_known")) or freight is not None
    fin_known = financing is not None

    return compute_expected_profit(
        expected_revenue=None,  # do not use historical as revenue
        proposed_bid_revenue=float(proposed) if proposed is not None else None,
        historical_award_value=float(historical) if historical is not None else None,
        product_cost=roll.get("TOTAL_KNOWN_RETAIL_COST"),
        freight=freight if freight_known else None,
        financing=financing if fin_known else None,
        other_costs=None,  # unknown — not zero
        price_basis="PUBLIC_RETAIL",
        completeness_pct=roll.get("coverage_pct"),
        evidence_grade=roll.get("completeness_grade"),
        execution_pass=None,  # never invent PASS
        targets=targets or load_targets_from_store(),
        unresolved_material=unresolved_share > 0.15,
        financing_reason=(
            "rollup_financing_cost_verified"
            if fin_known and financing not in (0, 0.0, "0")
            else ("rollup_zero_financing_documented" if financing in (0, 0.0, "0") else None)
        ),
        financing_transaction_verified=bool(fin_known),
        line_count_required=int(line_required) if line_required is not None else None,
        line_count_sourced=int(line_sourced) if line_sourced is not None else None,
    )


# Re-export for callers that imported evaluate helpers from economics
__all__ = [
    "compute_expected_profit",
    "from_line_item_rollup",
    "evaluate_verified_commercial_decision",
    "COST_VERIFIED",
    "COST_VERIFIED_ZERO",
    "COST_NOT_APPLICABLE",
    "COST_REQUIRED_UNKNOWN",
]

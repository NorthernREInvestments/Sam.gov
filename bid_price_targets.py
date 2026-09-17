"""Evidence-based bid-price thresholds — never invents a winning price."""

from __future__ import annotations

from typing import Any

from document_ingestion_constants import (
    BID_BREAK_EVEN,
    BID_HISTORICAL,
    BID_MARKET_REF,
    BID_MIN_10K,
    BID_MIN_MARGIN,
    BID_USER_SELECTED,
    THRESHOLD_CALCULATED,
    THRESHOLD_HISTORICAL,
    THRESHOLD_MARKET,
    THRESHOLD_SUBMITTED,
)
from economic_integrity import min_actual_profit_usd


def _f(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_bid_price_targets(
    *,
    landed_cost: float | None = None,
    supplier_cost: float | None = None,
    freight: float | None = None,
    other_mandatory_costs: float | None = None,
    target_margin_pct: float | None = None,
    historical_award_price: float | None = None,
    market_reference_price: float | None = None,
    user_selected_bid_price: float | None = None,
    min_profit: float | None = None,
) -> dict[str, Any]:
    """
    Calculate useful bid thresholds for bidder-priced solicitations.
    Does NOT claim any threshold will win.
    """
    profit_target = min_profit if min_profit is not None else min_actual_profit_usd()
    cost_parts = []
    for v in (supplier_cost, freight, other_mandatory_costs):
        fv = _f(v)
        if fv is not None:
            cost_parts.append(fv)
    computed_landed = _f(landed_cost)
    if computed_landed is None and cost_parts:
        # Only sum known parts — unknown freight does not become 0 in the label
        if freight is None and supplier_cost is not None:
            computed_landed = None  # incomplete landed
            known_cost = _f(supplier_cost)
        else:
            computed_landed = sum(cost_parts)
            known_cost = computed_landed
    else:
        known_cost = computed_landed

    thresholds: list[dict[str, Any]] = []

    def add(kind: str, value: float | None, basis: str, *, notes: str | None = None) -> None:
        thresholds.append(
            {
                "kind": kind,
                "value": value,
                "status": "CALCULATED" if value is not None else "UNKNOWN",
                "evidence_basis": basis,
                "is_winning_price_claim": False,
                "notes": notes,
            }
        )

    if known_cost is not None:
        add(BID_BREAK_EVEN, round(known_cost, 2), THRESHOLD_CALCULATED, notes="bid must cover known costs only")
        add(
            BID_MIN_10K,
            round(known_cost + float(profit_target), 2),
            THRESHOLD_CALCULATED,
            notes=f"known_cost + ${profit_target:.0f} profit target — not a win prediction",
        )
        if target_margin_pct is not None and target_margin_pct > 0 and target_margin_pct < 100:
            # bid = cost / (1 - margin)
            margin = float(target_margin_pct) / 100.0
            bid = known_cost / (1.0 - margin)
            add(
                BID_MIN_MARGIN,
                round(bid, 2),
                THRESHOLD_CALCULATED,
                notes=f"target_margin={target_margin_pct}% — not a win prediction",
            )
        else:
            add(BID_MIN_MARGIN, None, THRESHOLD_CALCULATED, notes="target_margin_pct not provided")
    else:
        add(BID_BREAK_EVEN, None, THRESHOLD_CALCULATED, notes="landed/supplier cost incomplete")
        add(BID_MIN_10K, None, THRESHOLD_CALCULATED, notes="requires meaningful cost evidence first")
        add(BID_MIN_MARGIN, None, THRESHOLD_CALCULATED, notes="requires meaningful cost evidence first")

    hist = _f(historical_award_price)
    add(
        BID_HISTORICAL,
        hist,
        THRESHOLD_HISTORICAL,
        notes="reference only" if hist is not None else "no historical award evidence",
    )
    market = _f(market_reference_price)
    add(
        BID_MARKET_REF,
        market,
        THRESHOLD_MARKET,
        notes="optional market reference" if market is not None else "no market reference provided",
    )
    user = _f(user_selected_bid_price)
    add(
        BID_USER_SELECTED,
        user,
        THRESHOLD_SUBMITTED if user is not None else THRESHOLD_CALCULATED,
        notes="operator-selected candidate bid" if user is not None else "no user bid selected",
    )

    return {
        "bidder_priced_model": True,
        "known_cost_basis": known_cost,
        "landed_cost_complete": computed_landed is not None and freight is not None,
        "profit_target_usd": profit_target,
        "thresholds": thresholds,
        "winning_price_invented": False,
        "disclaimer": (
            "Thresholds are calculated floors/references only. "
            "They are NOT predicted winning prices and MUST NOT be submitted as such."
        ),
        "supplier_outreach": 0,
        "lender_outreach": 0,
        "bid_submissions": 0,
    }

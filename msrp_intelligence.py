"""MSRP / list-price intelligence — never treat MSRP as acquisition cost."""

from __future__ import annotations

from typing import Any

from bid_pricing_constants import CONF_UNKNOWN


def _f(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def money2(v: float | None) -> float | None:
    if v is None:
        return None
    return round(float(v) + 1e-12, 2)


def compute_msrp_intelligence(
    *,
    line_items: list[dict[str, Any]] | None = None,
    basket_msrp: float | None = None,
    acquisition_cost: float | None = None,
    historical_gov_unit: float | None = None,
    historical_gov_extended: float | None = None,
    proposed_bid: float | None = None,
) -> dict[str, Any]:
    """
    MSRP is contextual evidence only — not expected acquisition cost / not profit.
    """
    lines = []
    unit_msrp_sum = 0.0
    ext_msrp_sum = 0.0
    has_any = False
    for li in line_items or []:
        qty = _f(li.get("quantity")) or 0.0
        unit = _f(li.get("msrp") or li.get("list_price") or li.get("unit_msrp"))
        if unit is None:
            lines.append(
                {
                    "line_id": li.get("line_id") or li.get("clin"),
                    "unit_msrp": None,
                    "extended_msrp": None,
                    "status": CONF_UNKNOWN,
                }
            )
            continue
        has_any = True
        ext = money2(unit * qty)
        unit_msrp_sum += unit * qty  # for weighted context
        ext_msrp_sum += ext or 0.0
        lines.append(
            {
                "line_id": li.get("line_id") or li.get("clin"),
                "description": li.get("description"),
                "quantity": qty,
                "unit_msrp": money2(unit),
                "extended_msrp": ext,
                "status": li.get("msrp_confidence") or "VERIFIED_PUBLIC",
                "is_acquisition_cost": False,
            }
        )

    basket = money2(basket_msrp) if basket_msrp is not None else (money2(ext_msrp_sum) if has_any else None)
    acq = money2(acquisition_cost)
    hist_ext = money2(historical_gov_extended)
    if hist_ext is None and historical_gov_unit is not None and line_items:
        qty_tot = sum(_f(li.get("quantity")) or 0 for li in line_items)
        if qty_tot:
            hist_ext = money2(historical_gov_unit * qty_tot)
    bid = money2(proposed_bid)

    discount_from_msrp = None
    if basket is not None and acq is not None and basket > 0:
        discount_from_msrp = round((basket - acq) / basket * 100.0, 2)

    hist_vs_msrp = None
    if basket is not None and hist_ext is not None and basket > 0:
        hist_vs_msrp = round(hist_ext / basket * 100.0, 2)

    bid_vs_msrp = None
    if basket is not None and bid is not None and basket > 0:
        bid_vs_msrp = round(bid / basket * 100.0, 2)

    # Potential gross spread (MSRP - acq) is NOT profit
    potential_gross_spread = money2(basket - acq) if basket is not None and acq is not None else None

    return {
        "kind": "MsrpIntelligence",
        "line_items": lines,
        "basket_msrp": basket,
        "acquisition_cost": acq,
        "acquisition_is_not_msrp": True,
        "acquisition_discount_from_msrp_pct": discount_from_msrp,
        "historical_government_extended": hist_ext,
        "historical_government_vs_msrp_pct": hist_vs_msrp,
        "proposed_bid": bid,
        "proposed_bid_vs_msrp_pct": bid_vs_msrp,
        "potential_gross_spread_msrp_minus_acq": potential_gross_spread,
        "potential_gross_spread_is_not_profit": True,
        "msrp_represented_as_acquisition_cost": False,
        "shorthand": {
            "what_they_want": "see_line_items",
            "msrp": basket,
            "historical_government_price": hist_ext,
            "estimated_acquisition_cost": acq,
            "potential_gross_spread": potential_gross_spread,
            "note": "Gross spread ≠ expected transaction profit",
        },
    }

"""Phase L economics — retail baseline + default 5% financing + profit tiers."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from economic_integrity import min_actual_profit_usd

TIER_FAIL = "FAIL"
TIER_PASS = "PASS"
TIER_STRONG = "STRONG"
TIER_EXCELLENT = "EXCELLENT"
TIER_EXCEPTIONAL = "EXCEPTIONAL"
TIER_MONSTER = "MONSTER"

DEFAULT_FINANCING_RATE = 0.05


def _f(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def profit_tier(expected_net: float | None) -> str:
    if expected_net is None:
        return TIER_FAIL
    if expected_net < 10000:
        return TIER_FAIL
    if expected_net < 25000:
        return TIER_PASS
    if expected_net < 50000:
        return TIER_STRONG
    if expected_net < 75000:
        return TIER_EXCELLENT
    if expected_net < 100000:
        return TIER_EXCEPTIONAL
    return TIER_MONSTER


def estimate_financing_cost(
    acquisition_amount: float | None,
    *,
    rate: float = DEFAULT_FINANCING_RATE,
) -> float | None:
    if acquisition_amount is None:
        return None
    if acquisition_amount < 0:
        return None
    return round(float(acquisition_amount) * float(rate), 2)


def build_phase_l_economics(
    *,
    quantity: float | None,
    uom: str | None = None,
    historical_unit_price: float | None = None,
    expected_bid_unit_price: float | None = None,
    public_retail_unit_price: float | None = None,
    public_retail_source: str | None = None,
    freight: float | None = None,
    packaging: float | None = None,
    compliance_direct_costs: float | None = None,
    contingency: float | None = None,
    financing_rate: float = DEFAULT_FINANCING_RATE,
    include_financing_scenarios: bool = True,
) -> dict[str, Any]:
    """
    Conservative retail-first screening.
    Do NOT invent discounts to create a PASS.
    Default decision case uses financing_rate (5%).
    """
    qty = _f(quantity)
    hist_u = _f(historical_unit_price)
    bid_u = _f(expected_bid_unit_price) if expected_bid_unit_price is not None else hist_u
    retail_u = _f(public_retail_unit_price)
    freight_v = _f(freight) or 0.0
    packaging_v = _f(packaging) or 0.0
    compliance_v = _f(compliance_direct_costs) or 0.0
    contingency_v = _f(contingency) or 0.0

    expected_revenue = None
    if bid_u is not None and qty is not None:
        expected_revenue = round(bid_u * qty, 2)

    public_retail_total = None
    if retail_u is not None and qty is not None:
        public_retail_total = round(retail_u * qty, 2)

    gross_retail_spread = None
    if expected_revenue is not None and public_retail_total is not None:
        gross_retail_spread = round(expected_revenue - public_retail_total, 2)

    estimated_direct_costs = round(freight_v + packaging_v + compliance_v + contingency_v, 2)
    financing_cost = estimate_financing_cost(public_retail_total, rate=financing_rate)

    expected_net = None
    blocker = None
    if expected_revenue is None:
        blocker = "MISSING_REVENUE_BASIS"
    elif public_retail_total is None:
        blocker = "MISSING_PUBLIC_RETAIL"
    else:
        fin = financing_cost if financing_cost is not None else 0.0
        expected_net = round(expected_revenue - public_retail_total - estimated_direct_costs - fin, 2)
        # Financing must not rescue negative unit economics
        if gross_retail_spread is not None and gross_retail_spread <= 0:
            expected_net = round(gross_retail_spread - estimated_direct_costs - fin, 2)
            blocker = "NEGATIVE_GROSS_RETAIL_SPREAD"

    floor = min_actual_profit_usd()
    tier = profit_tier(expected_net)

    scenarios = {}
    if include_financing_scenarios and public_retail_total is not None and expected_revenue is not None:
        for label, rate in (("0pct", 0.0), ("3pct", 0.03), ("5pct", 0.05)):
            fin = estimate_financing_cost(public_retail_total, rate=rate) or 0.0
            net = round(expected_revenue - public_retail_total - estimated_direct_costs - fin, 2)
            scenarios[label] = {
                "financing_rate": rate,
                "financing_cost": fin,
                "expected_net_profit": net,
                "profit_tier": profit_tier(net),
            }

    return {
        "kind": "PhaseLEconomics",
        "quantity": qty,
        "uom": uom,
        "historical_unit_price": hist_u,
        "expected_bid_unit_price": bid_u,
        "expected_revenue": expected_revenue,
        "public_retail_unit_price": retail_u,
        "public_retail_total": public_retail_total,
        "public_retail_source": public_retail_source,
        "public_retail_checked_at": now_utc().isoformat() if retail_u is not None else None,
        "acquisition_cost": public_retail_total,
        "gross_retail_spread": gross_retail_spread,
        "estimated_direct_costs": estimated_direct_costs,
        "estimated_financing_cost": financing_cost,
        "financing_rate_default": financing_rate,
        "expected_net_profit": expected_net,
        "profit_tier": tier,
        "meets_floor": bool(expected_net is not None and expected_net >= floor),
        "profit_floor": floor,
        "blocker": blocker,
        "financing_scenarios": scenarios,
        "note": (
            "Retail is the conservative public baseline. "
            "Unverified discounts must not create a PASS."
        ),
    }

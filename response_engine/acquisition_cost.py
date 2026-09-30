"""R2 acquisition cost + opportunity economics — Decimal, no fabricated costs."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from response_engine.financing import financing_cost_of
from response_engine.money import D, as_str, money
from response_engine.pricing import compute_profit
from response_engine.r2_constants import (
    PROVISIONAL_PROFIT,
    UNKNOWN,
    UNKNOWN_PROFIT,
    VERIFIED,
    VERIFIED_PROFIT,
)


def build_acquisition_cost_breakdown(
    *,
    product_subtotal: Any = None,
    product_evidence: str = UNKNOWN,
    inbound_freight: Any = None,
    freight_evidence: str = UNKNOWN,
    packaging: Any = None,
    packaging_evidence: str = UNKNOWN,
    other_fees: Any = None,
    other_evidence: str = UNKNOWN,
    apply_financing: bool = True,
    financing_rate: Any = None,
) -> dict[str, Any]:
    """PRODUCT + FREIGHT + PACKAGING + FEES (+ FINANCING) = TOTAL. No silent zeros for UNKNOWN."""

    def _comp(name: str, value: Any, evidence: str) -> dict[str, Any]:
        d = D(value)
        return {
            "component": name,
            "amount": as_str(money(d)) if d is not None else None,
            "evidence_state": evidence if d is not None else UNKNOWN,
            "blocks_verified": d is None and evidence != "NOT_APPLICABLE",
        }

    comps = [
        _comp("product_subtotal", product_subtotal, product_evidence),
        _comp("inbound_freight", inbound_freight, freight_evidence),
        _comp("packaging", packaging, packaging_evidence),
        _comp("other_fees", other_fees, other_evidence if other_fees is not None else "NOT_APPLICABLE"),
    ]
    # Sum only known amounts — UNKNOWN components block verified total
    known = []
    blocked = []
    for c in comps:
        if c["amount"] is not None:
            known.append(D(c["amount"]) or Decimal("0"))
        elif c.get("blocks_verified") and c["component"] in {"product_subtotal", "inbound_freight", "packaging"}:
            if c["component"] == "inbound_freight" and freight_evidence == "NOT_APPLICABLE":
                continue
            if c["component"] == "packaging" and packaging_evidence == "NOT_APPLICABLE":
                continue
            blocked.append(c["component"])

    pre_fin = money(sum(known, Decimal("0"))) if known and not blocked else None
    if blocked and "product_subtotal" in blocked:
        pre_fin = None

    fin = {"financing_cost": None, "evidence_state": UNKNOWN}
    if apply_financing and pre_fin is not None:
        fin = financing_cost_of(amount=pre_fin, rate=financing_rate)

    total = None
    if pre_fin is not None:
        fc = D(fin.get("financing_cost")) or Decimal("0")
        total = money(pre_fin + fc)

    verified_ok = (
        product_evidence == VERIFIED
        and (freight_evidence in {VERIFIED, "NOT_APPLICABLE", "INCLUDED_VERIFIED"} or D(inbound_freight) is not None and freight_evidence == VERIFIED)
        and (packaging_evidence in {VERIFIED, "NOT_APPLICABLE"} or (D(packaging) is not None and packaging_evidence == VERIFIED))
        and not blocked
    )

    return {
        "kind": "AcquisitionCostBreakdown",
        "components": comps,
        "pre_financing_acquisition_cost": as_str(pre_fin),
        "financing": fin,
        "total_execution_cost": as_str(total),
        "blocked_components": blocked,
        "verified_acquisition": verified_ok,
        "evidence_quality": VERIFIED if verified_ok else (PROVISIONAL_PROFIT if pre_fin is not None else UNKNOWN),
    }


def line_economics(
    *,
    quantity: Any,
    unit_acquisition: Any,
    unit_bid: Any = None,
    freight_total: Any = None,
    packaging_total: Any = None,
    product_evidence: str = UNKNOWN,
    freight_evidence: str = UNKNOWN,
    packaging_evidence: str = "NOT_APPLICABLE",
) -> dict[str, Any]:
    qty = D(quantity)
    unit = D(unit_acquisition)
    if qty is None or unit is None:
        return {
            "ok": False,
            "reason": "quantity_or_unit_cost_unknown",
            "profit_status": UNKNOWN_PROFIT,
        }
    product = money(qty * unit)
    breakdown = build_acquisition_cost_breakdown(
        product_subtotal=product,
        product_evidence=product_evidence,
        inbound_freight=freight_total,
        freight_evidence=freight_evidence if freight_total is not None else "NOT_APPLICABLE",
        packaging=packaging_total,
        packaging_evidence=packaging_evidence if packaging_total is not None else "NOT_APPLICABLE",
    )
    bid_unit = D(unit_bid)
    revenue = money(qty * bid_unit) if bid_unit is not None else None
    profit = compute_profit(
        bid_revenue=revenue,
        total_execution_cost=breakdown.get("total_execution_cost"),
        evidence_quality=VERIFIED if breakdown.get("verified_acquisition") and revenue is not None else UNKNOWN,
    )
    return {
        "ok": True,
        "quantity": as_str(qty),
        "unit_acquisition": as_str(money(unit)),
        "product_subtotal": as_str(product),
        "breakdown": breakdown,
        "revenue": as_str(revenue),
        "profit": profit,
    }

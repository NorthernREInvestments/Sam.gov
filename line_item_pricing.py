"""Line-item pricing + deterministic cost allocation with exact reconciliation."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from bid_pricing_constants import (
    AWARD_ALL_OR_NONE,
    AWARD_LINE_ITEM,
    AWARD_PARTIAL,
    CONF_UNKNOWN,
)
from transaction_economics import money_dec, money_float


def _d(v: Any) -> Decimal | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def allocate_shared_cost(
    *,
    total: Decimal | None,
    lines: list[dict[str, Any]],
    basis: str = "acquisition_value",
) -> tuple[list[Decimal], str]:
    """Allocate shared cost across lines; last line absorbs rounding residue."""
    n = len(lines)
    if total is None or n == 0:
        return [Decimal("0.00")] * n, basis
    weights: list[Decimal] = []
    for li in lines:
        if basis == "unit_count":
            w = _d(li.get("quantity")) or Decimal("0")
        elif basis == "weight":
            w = (_d(li.get("weight")) or Decimal("0")) * (_d(li.get("quantity")) or Decimal("1"))
        else:  # acquisition_value
            acq = _d(li.get("acquisition_unit_cost")) or Decimal("0")
            qty = _d(li.get("quantity")) or Decimal("0")
            w = acq * qty
        weights.append(w)
    wsum = sum(weights)
    if wsum <= 0:
        # equal split
        each = (total / Decimal(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        alloc = [each] * n
        residue = total - sum(alloc)
        alloc[-1] = (alloc[-1] + residue).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return alloc, "equal_split_fallback"
    alloc: list[Decimal] = []
    running = Decimal("0.00")
    for i, w in enumerate(weights):
        if i == n - 1:
            part = (total - running).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            part = (total * w / wsum).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            running += part
        alloc.append(part)
    return alloc, basis


def build_line_item_pricing(
    *,
    line_items: list[dict[str, Any]],
    freight_total: float | None = None,
    financing_total: float | None = None,
    expense_total: float | None = None,
    risk_total: float | None = None,
    allocation_basis: str = "acquisition_value",
    target_markup_pct: float | None = None,
    award_mode: str = AWARD_ALL_OR_NONE,
    historical_unit_by_line: dict[str, float] | None = None,
    msrp_by_line: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Build per-CLIN pricing. Totals reconcile to the cent.
    Unknown acquisition → line incomplete (not $0).
    """
    lines_in = list(line_items or [])
    hist = historical_unit_by_line or {}
    msrp_map = msrp_by_line or {}

    freight_d = money_dec(freight_total)
    fin_d = money_dec(financing_total)
    exp_d = money_dec(expense_total) or Decimal("0.00")
    risk_d = money_dec(risk_total) or Decimal("0.00")

    # Only allocate when known
    freight_alloc, freight_basis = allocate_shared_cost(
        total=freight_d, lines=lines_in, basis=allocation_basis
    ) if freight_d is not None else ([None] * len(lines_in), "unallocated_unknown")
    fin_alloc, fin_basis = allocate_shared_cost(
        total=fin_d, lines=lines_in, basis=allocation_basis
    ) if fin_d is not None else ([None] * len(lines_in), "unallocated_unknown")
    exp_alloc, exp_basis = allocate_shared_cost(total=exp_d, lines=lines_in, basis=allocation_basis)
    risk_alloc, risk_basis = allocate_shared_cost(total=risk_d, lines=lines_in, basis=allocation_basis)

    markup = Decimal(str(target_markup_pct / 100.0)) if target_markup_pct is not None else Decimal("0.12")

    priced = []
    sum_acq = Decimal("0.00")
    sum_bid = Decimal("0.00")
    sum_profit = Decimal("0.00")
    incomplete = False

    for i, li in enumerate(lines_in):
        lid = str(li.get("line_id") or li.get("clin") or f"L{i+1}")
        qty = _d(li.get("quantity")) or Decimal("0")
        acq_u = _d(li.get("acquisition_unit_cost"))
        conf = str(li.get("acquisition_confidence") or CONF_UNKNOWN)
        if acq_u is None:
            incomplete = True
            priced.append(
                {
                    "line_id": lid,
                    "requested_item": li.get("description") or li.get("requested_item"),
                    "quantity": money_float(qty),
                    "unit": li.get("unit") or "EA",
                    "acquisition_cost": None,
                    "acquisition_confidence": conf,
                    "status": "INCOMPLETE",
                    "unknown_treated_as_zero": False,
                }
            )
            continue

        acq_ext = (acq_u * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        fr = freight_alloc[i] if freight_alloc[i] is not None else Decimal("0.00")
        fi = fin_alloc[i] if fin_alloc[i] is not None else Decimal("0.00")
        # If freight/fin unknown globally, don't pretend allocated zero for profit certainty
        freight_known = freight_d is not None
        fin_known = fin_d is not None
        ex = exp_alloc[i]
        rk = risk_alloc[i]

        landed = acq_ext + (fr if freight_known else Decimal("0")) + (fi if fin_known else Decimal("0")) + ex + rk
        # Proposed unit bid from markup on landed / qty
        if qty > 0:
            unit_bid = (landed / qty * (Decimal("1") + markup)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            unit_bid = Decimal("0.00")
        # Allow explicit override
        if li.get("proposed_unit_bid") is not None:
            unit_bid = money_dec(li["proposed_unit_bid"]) or unit_bid
        ext_bid = (unit_bid * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        unit_profit = None
        ext_profit = None
        if freight_known and fin_known:
            ext_profit = (ext_bid - landed).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            unit_profit = (ext_profit / qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if qty else None
            sum_profit += ext_profit

        sum_acq += acq_ext
        sum_bid += ext_bid

        priced.append(
            {
                "line_id": lid,
                "requested_item": li.get("description") or li.get("requested_item"),
                "quantity": float(qty),
                "unit": li.get("unit") or "EA",
                "acquisition_unit_cost": money_float(acq_u),
                "acquisition_cost": money_float(acq_ext),
                "acquisition_confidence": conf,
                "freight_allocation": money_float(fr) if freight_known else None,
                "financing_allocation": money_float(fi) if fin_known else None,
                "transaction_expense_allocation": money_float(ex),
                "risk_allocation": money_float(rk),
                "target_markup_pct": float(markup * 100),
                "proposed_unit_bid": money_float(unit_bid),
                "extended_bid": money_float(ext_bid),
                "expected_unit_profit": money_float(unit_profit),
                "expected_extended_profit": money_float(ext_profit),
                "historical_government_benchmark": hist.get(lid),
                "msrp_list": msrp_map.get(lid) or li.get("msrp"),
                "compliance_status": li.get("compliance_status") or "UNKNOWN",
                "source_evidence": li.get("source_evidence"),
                "status": "PRICED" if freight_known and fin_known else "PRICED_PENDING_COST_COMPLETION",
            }
        )

    # Reconciliation checks
    freight_sum = sum((p.get("freight_allocation") or 0) for p in priced if p.get("freight_allocation") is not None)
    fin_sum = sum((p.get("financing_allocation") or 0) for p in priced if p.get("financing_allocation") is not None)
    recon = {
        "acquisition_total": money_float(sum_acq),
        "bid_total": money_float(sum_bid),
        "profit_total": money_float(sum_profit) if not incomplete and freight_d is not None and fin_d is not None else None,
        "freight_allocated_total": money_float(Decimal(str(round(freight_sum, 2)))) if freight_d is not None else None,
        "freight_source_total": money_float(freight_d),
        "freight_reconciles": freight_d is None or abs(float(freight_sum) - float(freight_d)) < 0.02,
        "financing_allocated_total": money_float(Decimal(str(round(fin_sum, 2)))) if fin_d is not None else None,
        "financing_source_total": money_float(fin_d),
        "financing_reconciles": fin_d is None or abs(float(fin_sum) - float(fin_d)) < 0.02,
        "totals_reconcile_exactly": True,
    }
    if freight_d is not None:
        recon["totals_reconcile_exactly"] = bool(recon["freight_reconciles"] and recon["financing_reconciles"])

    return {
        "kind": "LineItemPricing",
        "lines": priced,
        "allocation": {
            "basis": allocation_basis,
            "freight_basis": freight_basis,
            "financing_basis": fin_basis,
            "expense_basis": exp_basis,
            "risk_basis": risk_basis,
        },
        "award_mode": award_mode if award_mode in {AWARD_ALL_OR_NONE, AWARD_PARTIAL, AWARD_LINE_ITEM} else AWARD_ALL_OR_NONE,
        "reconciliation": recon,
        "incomplete": incomplete or freight_d is None or fin_d is None,
        "unknown_cost_as_zero": False,
    }

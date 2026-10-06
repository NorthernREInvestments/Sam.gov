"""Target-cost envelopes + financing model (owner cash = $0)."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from deep_completion_3to5.models import (
    FINANCING_RESERVE_PCT,
    FREIGHT_RESERVE_PCT,
    LINE_TARGET_COST_ALLOCATED,
    LINE_TARGET_COST_CONFIRMED,
    NO_LINE_TARGET_AVAILABLE,
    REVENUE_NOT_READY,
)


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def _money(v: Decimal | float | int | None) -> float | None:
    if v is None:
        return None
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_target_economics(revenue: dict[str, Any]) -> dict[str, Any]:
    if revenue.get("status") == REVENUE_NOT_READY or not revenue.get("usable_as_revenue"):
        return {
            "revenue": None,
            "status": REVENUE_NOT_READY,
            "MAX_ACQUISITION_COST_FOR_$5K_PROFIT": None,
            "MAX_ACQUISITION_COST_FOR_$10K_PROFIT": None,
            "MAX_ACQUISITION_COST_FOR_15_PERCENT_MARGIN": None,
            "MAX_ACQUISITION_COST_FOR_20_PERCENT_MARGIN": None,
            "note": "No usable revenue — cannot compute target-cost envelope",
        }

    rev = _d(revenue.get("value"))
    if rev <= 0:
        return {
            "revenue": 0.0,
            "status": REVENUE_NOT_READY,
            "MAX_ACQUISITION_COST_FOR_$5K_PROFIT": None,
            "MAX_ACQUISITION_COST_FOR_$10K_PROFIT": None,
            "MAX_ACQUISITION_COST_FOR_15_PERCENT_MARGIN": None,
            "MAX_ACQUISITION_COST_FOR_20_PERCENT_MARGIN": None,
        }

    # total_cost = acq + freight + financing = acq * (1 + f + fin)
    load = Decimal("1") + Decimal(str(FREIGHT_RESERVE_PCT)) + Decimal(str(FINANCING_RESERVE_PCT))

    def max_acq_for_profit(profit: Decimal) -> float | None:
        # rev - acq*load >= profit → acq <= (rev - profit) / load
        room = rev - profit
        if room <= 0:
            return None
        return _money(room / load)

    def max_acq_for_margin(margin_pct: Decimal) -> float | None:
        # profit = margin * rev; same as above
        return max_acq_for_profit(rev * margin_pct / Decimal("100"))

    return {
        "revenue": _money(rev),
        "revenue_classification": revenue.get("classification"),
        "revenue_confidence": revenue.get("confidence"),
        "status": "READY" if revenue.get("confidence") != "LOW" else "WEAK_REVENUE",
        "freight_reserve_pct": FREIGHT_RESERVE_PCT,
        "financing_reserve_pct": FINANCING_RESERVE_PCT,
        "MAX_ACQUISITION_COST_FOR_$5K_PROFIT": max_acq_for_profit(Decimal("5000")),
        "MAX_ACQUISITION_COST_FOR_$10K_PROFIT": max_acq_for_profit(Decimal("10000")),
        "MAX_ACQUISITION_COST_FOR_15_PERCENT_MARGIN": max_acq_for_margin(Decimal("15")),
        "MAX_ACQUISITION_COST_FOR_20_PERCENT_MARGIN": max_acq_for_margin(Decimal("20")),
        "note": (
            "Weak historical/category revenue — treat ceilings as exploratory only"
            if revenue.get("confidence") == "LOW"
            else "Conservative freight 10% + financing 3% reserves included"
        ),
    }


def allocate_line_targets(
    lines: list[dict[str, Any]],
    target: dict[str, Any],
) -> list[dict[str, Any]]:
    """Allocate basket acquisition ceiling across quote-ready lines by qty weight."""
    ceiling = target.get("MAX_ACQUISITION_COST_FOR_$5K_PROFIT")
    out = []
    usable = [l for l in lines if l.get("identity_usable")]
    if ceiling is None or not usable:
        for ln in lines:
            row = dict(ln)
            row["line_target_cost_class"] = NO_LINE_TARGET_AVAILABLE
            row["line_target_cost"] = None
            out.append(row)
        return out

    weights = []
    for ln in usable:
        w = float(ln.get("quantity") or 1)
        # bump exact MPN slightly
        if str(ln.get("identity_class") or "").startswith("A_"):
            w *= 1.2
        weights.append(max(w, 0.1))
    total_w = sum(weights) or 1.0
    by_id = {}
    for ln, w in zip(usable, weights):
        share = float(ceiling) * (w / total_w)
        by_id[ln.get("line_id")] = {
            "line_target_cost": round(share, 2),
            "line_target_cost_class": LINE_TARGET_COST_ALLOCATED,
        }

    for ln in lines:
        row = dict(ln)
        if ln.get("line_id") in by_id:
            row.update(by_id[ln["line_id"]])
        else:
            row["line_target_cost_class"] = NO_LINE_TARGET_AVAILABLE
            row["line_target_cost"] = None
        out.append(row)
    return out


def model_financing(
    opportunity_id: str,
    *,
    revenue: dict[str, Any],
    target: dict[str, Any],
    packet_coverage: float,
) -> dict[str, Any]:
    """Hard rule: owner pre-payment cash = $0."""
    ceiling = target.get("MAX_ACQUISITION_COST_FOR_$5K_PROFIT")
    # Use ceiling as proxy supplier amount until quotes return
    supplier_amount = float(ceiling) if ceiling is not None else None
    freight = round(supplier_amount * FREIGHT_RESERVE_PCT, 2) if supplier_amount else None
    fin_need = round((supplier_amount or 0) + (freight or 0), 2) if supplier_amount else None
    fin_cost = round(fin_need * FINANCING_RESERVE_PCT, 2) if fin_need else None

    status = "FINANCING_UNKNOWN"
    if revenue.get("status") == REVENUE_NOT_READY:
        status = "FINANCING_UNKNOWN"
    elif supplier_amount is None:
        status = "FINANCING_UNKNOWN"
    elif packet_coverage >= 0.75:
        status = "SUPPLIER_TERMS_REQUIRED"  # quotes needed to confirm terms
        # Modeled stack assumes PO financing with $0 owner cash
        try:
            from financing_intelligence.assess import assess_opportunity_financing

            card = assess_opportunity_financing(
                opportunity_id=opportunity_id,
                contract_value=float(revenue.get("value") or 0),
                supplier_cost=supplier_amount,
                freight=freight or 0,
                persist=False,
            )
            owner_cash = float(card.get("owner_cash_required") or card.get("company_capital_required_to_close") or 0)
            if owner_cash > 0:
                status = "FINANCING_BLOCKED"
            else:
                status = "FINANCEABLE_MODELED"
                fin_cost = float(
                    (card.get("best_stack") or {}).get("financing_cost")
                    or (card.get("best_stack") or {}).get("estimated_financing_cost")
                    or fin_cost
                    or 0
                )
        except Exception:
            status = "FINANCEABLE_MODELED"
    else:
        status = "SUPPLIER_TERMS_REQUIRED"

    return {
        "opportunity_id": opportunity_id,
        "supplier_cost_ceiling": supplier_amount,
        "estimated_financing_need": fin_need,
        "modeled_financing_cost": fin_cost,
        "supplier_terms_required": status in {"SUPPLIER_TERMS_REQUIRED", "FINANCEABLE_MODELED"},
        "owner_cash_required": 0.0 if status != "FINANCING_BLOCKED" else None,
        "status": status,
        "compatible_with_zero_owner_cash": status != "FINANCING_BLOCKED",
        "financing_duration_note": "Modeled until supplier payment vs government payment timing known",
    }

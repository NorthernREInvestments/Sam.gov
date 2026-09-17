"""Evidence-bounded preliminary economics — not final bid economics."""

from __future__ import annotations

from typing import Any

from economic_integrity import min_actual_profit_usd


def _f(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_evidence_bounded_economics(
    *,
    quantities_by_line: list[dict[str, Any]] | None = None,
    unit_price_evidence: list[dict[str, Any]] | None = None,
    freight_evidence: list[dict[str, Any]] | None = None,
    total_quantity: float | None = None,
) -> dict[str, Any]:
    """
    Build LOW/MID/HIGH evidence cost cases from dated price evidence.
    Never invents missing inputs. Old prices are never labeled current supplier cost.
    """
    prices = []
    for p in unit_price_evidence or []:
        val = _f(p.get("unit_price") if "unit_price" in p else p.get("value"))
        if val is None:
            continue
        prices.append(
            {
                "unit_price": val,
                "price_class": p.get("price_class") or p.get("evidence_class"),
                "date": p.get("date") or p.get("price_date"),
                "source": p.get("source_url") or p.get("source"),
                "notes": p.get("notes"),
                "is_current_supplier_cost": False,  # historical/public ≠ current cost
            }
        )

    if not prices:
        return {
            "status": "UNKNOWN",
            "reason": "insufficient_price_evidence",
            "LOW_EVIDENCE_COST_CASE": None,
            "MID_EVIDENCE_COST_CASE": None,
            "HIGH_EVIDENCE_COST_CASE": None,
            "estimated_supplier_cost_range": None,
            "freight": None,
            "preliminary_landed_range": None,
            "break_even_bid_range": None,
            "minimum_bid_for_10k_profit_range": None,
            "winning_price_claimed": False,
            "inputs_used": [],
        }

    qty = _f(total_quantity)
    if qty is None and quantities_by_line:
        qty = sum(_f(li.get("quantity")) or 0 for li in quantities_by_line)
    if not qty:
        return {
            "status": "UNKNOWN",
            "reason": "quantity_unknown",
            "LOW_EVIDENCE_COST_CASE": None,
            "MID_EVIDENCE_COST_CASE": None,
            "HIGH_EVIDENCE_COST_CASE": None,
            "winning_price_claimed": False,
            "inputs_used": prices,
        }

    unit_vals = sorted(p["unit_price"] for p in prices)
    low_u, high_u = unit_vals[0], unit_vals[-1]
    mid_u = unit_vals[len(unit_vals) // 2]

    def case(label: str, unit: float) -> dict[str, Any]:
        extended = round(unit * float(qty), 2)
        matching = [p for p in prices if p["unit_price"] == unit] or [prices[0]]
        return {
            "label": label,
            "assumed_unit_price": unit,
            "quantity": qty,
            "extended_supplier_cost": extended,
            "evidence_producing_number": matching[0],
            "is_current_supplier_cost": False,
            "notes": "evidence_bounded_not_final_quote",
        }

    low_c = case("LOW_EVIDENCE_COST_CASE", low_u)
    mid_c = case("MID_EVIDENCE_COST_CASE", mid_u)
    high_c = case("HIGH_EVIDENCE_COST_CASE", high_u)

    freight_vals = []
    for fe in freight_evidence or []:
        fv = _f(fe.get("value") or fe.get("freight"))
        if fv is not None:
            freight_vals.append({"value": fv, "source": fe.get("source_url") or fe.get("source"), "date": fe.get("date")})

    freight = None
    if freight_vals:
        freight = {
            "status": "EVIDENCE_BOUNDED",
            "low": min(x["value"] for x in freight_vals),
            "high": max(x["value"] for x in freight_vals),
            "evidence": freight_vals,
        }

    cost_low = low_c["extended_supplier_cost"]
    cost_high = high_c["extended_supplier_cost"]
    landed_low = cost_low + (freight["low"] if freight else 0) if freight else None
    landed_high = cost_high + (freight["high"] if freight else 0) if freight else None
    # If freight unknown, landed remains unknown (do not treat freight as 0)
    if not freight:
        landed_range = None
        be_range = {"low": cost_low, "high": cost_high, "basis": "supplier_extended_only_freight_unknown"}
        target = min_actual_profit_usd()
        min10_range = {
            "low": cost_low + target,
            "high": cost_high + target,
            "basis": f"supplier_extended_plus_{target}_freight_unknown",
        }
    else:
        landed_range = {"low": landed_low, "high": landed_high, "freight_included": True}
        be_range = {"low": landed_low, "high": landed_high, "basis": "landed"}
        target = min_actual_profit_usd()
        min10_range = {
            "low": landed_low + target,
            "high": landed_high + target,
            "basis": f"landed_plus_{target}",
        }

    return {
        "status": "EVIDENCE_BOUNDED",
        "LOW_EVIDENCE_COST_CASE": low_c,
        "MID_EVIDENCE_COST_CASE": mid_c,
        "HIGH_EVIDENCE_COST_CASE": high_c,
        "estimated_supplier_cost_range": {"low": cost_low, "high": cost_high},
        "estimated_extended_cost_range": {"low": cost_low, "high": cost_high},
        "freight": freight,
        "preliminary_landed_range": landed_range,
        "break_even_bid_range": be_range,
        "minimum_bid_for_10k_profit_range": min10_range,
        "winning_price_claimed": False,
        "disclaimer": (
            "Preliminary evidence-bounded ranges only. Historical/public prices are NOT "
            "current supplier cost. Not a predicted winning bid."
        ),
        "inputs_used": prices,
        "quantity_total": qty,
    }

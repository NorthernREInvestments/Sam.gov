"""Cash-cycle / financing handoff — unknowns stay explicit."""

from __future__ import annotations

from typing import Any


def _unk(v: Any) -> Any:
    if v is None or v == "" or str(v).upper() in {"NONE", "NULL"}:
        return "UNKNOWN"
    return v


def build_cash_cycle_handoff(
    row: dict[str, Any] | None = None,
    *,
    requirements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Derive timing points where possible. Does not assume financing exists.
    """
    row = row if isinstance(row, dict) else {}
    reqs = requirements or []

    delivery_days = "UNKNOWN"
    for r in reqs:
        if r.get("category") == "DELIVERY" and r.get("subtype") == "ARO" and r.get("captured_value"):
            delivery_days = r.get("captured_value")
            break

    net_days = "UNKNOWN"
    for r in reqs:
        if r.get("category") == "PAYMENT" and r.get("subtype") == "NET_TERMS" and r.get("captured_value"):
            net_days = r.get("captured_value")
            break

    fob = "UNKNOWN"
    for r in reqs:
        if r.get("category") == "FOB" and r.get("captured_value"):
            fob = str(r.get("captured_value")).upper()
            break

    award_date = _unk(row.get("award_date"))
    # Supplier payment requirement often UNKNOWN pre-quote
    supplier_pay = _unk(row.get("supplier_payment_terms") or row.get("supplier_prepay_required"))

    return {
        "kind": "CashCycleHandoff",
        "award_date": award_date,
        "supplier_payment_requirement": supplier_pay,
        "production_ship_date": "UNKNOWN",
        "delivery_date": "UNKNOWN",
        "delivery_days_aro": delivery_days,
        "acceptance_date": "UNKNOWN",
        "invoice_date": "UNKNOWN",
        "expected_payment_date": "UNKNOWN",
        "payment_terms_net_days": net_days,
        "fob": fob,
        "pre_delivery_funding_requirement": "UNKNOWN",
        "cash_gap": "UNKNOWN",
        "capital_exposure_duration": "UNKNOWN",
        "post_delivery_receivable_period": net_days if net_days != "UNKNOWN" else "UNKNOWN",
        "financing_assumed": False,
        "note": "Unknowns are explicit. Do not assume financing exists.",
    }

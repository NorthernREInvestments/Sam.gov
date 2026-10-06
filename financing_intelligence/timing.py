"""Deterministic cash-cycle / financing timing model.

Unknown dates → missing information, never false timing risk.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from financing_intelligence.constants import BUILD, UNKNOWN
from financing_intelligence.store import money_str


def _parse_date(v: Any) -> date | None:
    if v in (None, "", UNKNOWN):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def evaluate_timing(
    *,
    dates: dict[str, Any] | None = None,
    supplier_net_days: int | None = None,
    financed_days_override: int | None = None,
) -> dict[str, Any]:
    """Compute financed days and timing cushion when dates are known."""
    dates = dates or {}
    supplier_due = _parse_date(dates.get("supplier_payment_due_date") or dates.get("deposit_due_date"))
    gov_pay = _parse_date(dates.get("expected_government_payment_date") or dates.get("government_payment_date"))
    lender_fund = _parse_date(dates.get("lender_funding_date"))
    ship = _parse_date(dates.get("estimated_ship_date") or dates.get("delivery_date"))
    invoice = _parse_date(dates.get("government_invoice_date"))
    maturity = _parse_date(dates.get("financing_maturity_date"))
    quote = _parse_date(dates.get("supplier_quote_date"))

    missing: list[str] = []
    if supplier_due is None and supplier_net_days is None:
        missing.append("supplier_payment_due_date")
    if gov_pay is None:
        missing.append("expected_government_payment_date")

    # Derive supplier due from ship + net if needed
    if supplier_due is None and ship is not None and supplier_net_days and supplier_net_days > 0:
        from datetime import timedelta

        supplier_due = ship + timedelta(days=int(supplier_net_days))

    financed_days: int | None = None
    if financed_days_override is not None:
        financed_days = int(financed_days_override)
    elif lender_fund is not None and (maturity is not None or gov_pay is not None):
        end = maturity or gov_pay
        financed_days = max(0, (end - lender_fund).days)
    elif gov_pay is not None and supplier_due is not None:
        financed_days = max(0, (gov_pay - supplier_due).days)

    days_until_supplier = None
    days_until_gov = None
    today = date.today()
    if supplier_due is not None:
        days_until_supplier = (supplier_due - today).days
    if gov_pay is not None:
        days_until_gov = (gov_pay - today).days

    timing_cushion = None
    uncovered_timing_gap = None
    timing_risk = False
    if supplier_due is not None and gov_pay is not None:
        cushion = (gov_pay - supplier_due).days
        timing_cushion = cushion
        if cushion < 0:
            uncovered_timing_gap = abs(cushion)
            timing_risk = True
        elif supplier_net_days and financed_days is not None and financed_days < supplier_net_days:
            # Financing duration shorter than supplier net bridge
            uncovered_timing_gap = int(supplier_net_days) - int(financed_days)
            timing_risk = uncovered_timing_gap > 0

    # Spec: do not invent timing risk when dates unknown
    if missing and financed_days_override is None and supplier_due is None:
        timing_risk = False
        uncovered_timing_gap = None

    return {
        "kind": "FinancingTimingAnalysis",
        "build": BUILD,
        "supplier_quote_date": quote.isoformat() if quote else None,
        "supplier_payment_due_date": supplier_due.isoformat() if supplier_due else None,
        "estimated_ship_date": ship.isoformat() if ship else None,
        "government_invoice_date": invoice.isoformat() if invoice else None,
        "expected_government_payment_date": gov_pay.isoformat() if gov_pay else None,
        "lender_funding_date": lender_fund.isoformat() if lender_fund else None,
        "financing_maturity_date": maturity.isoformat() if maturity else None,
        "expected_financed_days": financed_days,
        "days_until_supplier_payment_due": days_until_supplier,
        "days_until_expected_government_payment": days_until_gov,
        "timing_cushion_days": timing_cushion,
        "uncovered_timing_gap_days": uncovered_timing_gap,
        "timing_risk": timing_risk,
        "missing_dates": missing,
        "note": "Unknown dates surface as missing information — never false timing risk.",
    }

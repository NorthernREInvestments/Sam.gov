"""R2 financing / execution gates — reconcile µLab EXECUTION_FAIL, no lender actions."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from response_engine.money import D, as_str, money
from response_engine.r2_constants import DEFAULT_FINANCING_RATE, EXECUTION_FAIL, UNKNOWN


def financing_cost_of(*, amount: Any, rate: Any = DEFAULT_FINANCING_RATE) -> dict[str, Any]:
    amt = D(amount)
    r = D(rate) if rate is not None else D(DEFAULT_FINANCING_RATE)
    if amt is None or r is None:
        return {"financing_cost": None, "evidence_state": UNKNOWN, "amount": None, "rate": None}
    fee = money(amt * r)
    return {
        "financing_cost": as_str(fee),
        "amount_financed": as_str(money(amt)),
        "financing_rate": as_str(r),
        "evidence_state": "SCENARIO",
        "source": "response_engine.financing (DEFAULT_FINANCING_RATE from L6)",
        "double_count_protection": "financing applied once on acquisition amount only",
    }


def evaluate_execution_gates(quote_or_terms: dict[str, Any] | None) -> dict[str, Any]:
    """Flag PG / personal credit / owner prepayment — reconcile µLab EXECUTION_FAIL."""
    q = quote_or_terms or {}
    blob = " ".join(str(v) for v in q.values()).lower()
    flags = []
    if any(x in blob for x in ("personal guarantee", "personal guarantor", "pg required", "individually guarantee")):
        flags.append("PERSONAL_GUARANTEE_REQUIRED")
    if any(x in blob for x in ("personal credit", "personal card", "owner credit card")):
        flags.append("PERSONAL_CREDIT_REQUIRED")
    if any(x in blob for x in ("prepay", "prepayment", "prepaid", "payment in advance", "deposit required", "50% deposit")):
        flags.append("OWNER_PREPAYMENT_REQUIRED")
    owner_cash = D(q.get("owner_cash_required_before_gov_payment"))
    if owner_cash is not None and owner_cash > 0:
        flags.append("OWNER_CASH_REQUIRED")
    execution_fail = bool(
        set(flags) & {"PERSONAL_GUARANTEE_REQUIRED", "PERSONAL_CREDIT_REQUIRED", "OWNER_PREPAYMENT_REQUIRED"}
    )
    return {
        "flags": flags,
        "execution_status": EXECUTION_FAIL if execution_fail else ("REVIEW" if flags else "OK"),
        "owner_company_cash_required_before_gov_payment": as_str(money(owner_cash)) if owner_cash else "0.00",
        "preferred_path": "supplier_terms_or_po_finance_or_factoring",
        "no_lender_contact": True,
        "legacy_alignment": "micro_purchase_lab EXECUTION_FAIL",
    }


def financeability_summary(
    *,
    total_execution_cost: Any,
    owner_cash_required: Any = 0,
    supplier_terms_cover: bool | None = None,
    gates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gates = gates or {}
    cash = D(owner_cash_required) or Decimal("0")
    if gates.get("execution_status") == EXECUTION_FAIL:
        status = "execution_fail"
    elif cash > 0:
        status = "financing_gap"
    elif supplier_terms_cover:
        status = "requires_supplier_terms"
    elif D(total_execution_cost) is None:
        status = "unknown"
    else:
        status = "likely_financeable"
    return {
        "financeability": status,
        "owner_cash_required": as_str(money(cash)),
        "zero_owner_cash_path": cash == 0 and status not in {"execution_fail", "unknown"},
        "claims_lender_approval": False,
    }

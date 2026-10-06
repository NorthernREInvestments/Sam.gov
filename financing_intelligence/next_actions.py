"""Plain-language next actions for financing (name the missing fact)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from financing_intelligence.constants import (
    ST_CAPITAL_CONFIRMATION_REQUIRED,
    ST_CAPITAL_STACK_CLOSED,
    ST_EXECUTION_FAIL,
    ST_FINANCEABLE_BUT_TIMING_RISK,
    ST_FINANCING_GAP,
    ST_FINANCING_UNKNOWN,
    ST_LIKELY_FINANCEABLE,
    ST_NEEDS_LENDER_APPROVAL,
    ST_REQUIRES_SUPPLIER_TERMS,
)
from financing_intelligence.store import money, money_str


def build_next_action(
    *,
    status: str,
    lender_name: str | None = None,
    supplier_name: str | None = None,
    unfunded_gap: Any = 0,
    company_cash_required: Any = 0,
    missing: list[str] | None = None,
    blockers: list[str] | None = None,
    freight_unknown: bool = False,
    eligibility_missing: list[str] | None = None,
) -> str:
    missing = list(missing or [])
    blockers = list(blockers or [])
    eligibility_missing = list(eligibility_missing or [])
    lender = lender_name or "lender"
    supplier = supplier_name or "supplier"
    gap = money(unfunded_gap)
    cash_need = money(company_cash_required)

    if status == ST_EXECUTION_FAIL:
        if any("pg_" in b for b in blockers):
            return f"Reject — {lender} requires personal guarantee"
        if any("personal_credit" in b for b in blockers):
            return f"Reject — {lender} requires personal credit"
        if any("below_lender_minimum" in b for b in blockers):
            return f"Reject — deal below {lender} minimum size"
        return f"Reject — financing incompatible ({', '.join(blockers[:2]) or 'hard rules'})"

    if freight_unknown or "freight_funding_confirmation" in eligibility_missing:
        return f"Call {lender} to confirm freight funding"

    if status == ST_FINANCING_UNKNOWN:
        if "acquisition_cost" in missing or "supplier_cost" in missing:
            return "Need supplier acquisition cost before financing can be evaluated"
        if freight_unknown or "freight_funding_confirmation" in eligibility_missing:
            return f"Call {lender} to confirm freight funding"
        if "expected_government_payment_date" in missing:
            return "Need government payment timing"
        return "Need verified financing rules before this deal can be evaluated"

    if status == ST_CAPITAL_CONFIRMATION_REQUIRED:
        return f"Confirm use of ${money_str(cash_need or gap)} company capital"

    if status == ST_FINANCING_GAP:
        if "BLOCKED_BY_SUPPLIER_TERMS" in blockers or gap > 0:
            if supplier_name:
                return f"Ask {supplier} for terms covering ${money_str(gap)} more"
            return f"Supplier credit needs ${money_str(gap)} more"
        return f"Close financing gap of ${money_str(gap)}"

    if status == ST_REQUIRES_SUPPLIER_TERMS:
        return f"Ask {supplier} for verified Net terms / credit"

    if status == ST_FINANCEABLE_BUT_TIMING_RISK:
        return "Capital stack closed but timing gap remains — confirm repayment bridge"

    if status in {ST_LIKELY_FINANCEABLE, ST_NEEDS_LENDER_APPROVAL}:
        if freight_unknown or "freight_funding_confirmation" in eligibility_missing:
            return f"Call {lender} to confirm freight funding"
        return f"Transaction appears financeable; {lender} approval still required"

    if status == ST_CAPITAL_STACK_CLOSED:
        return "Capital stack closed — proceed to next execution gate"

    return "Check financing status for this opportunity"

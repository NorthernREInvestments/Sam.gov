"""Shared SAM commercial-budget gate for every live api.sam.gov request.

All chargeable SAM HTTP paths must call reserve_sam_call() before the request
and commit_sam_call() after (errors still count unless explicitly released).
"""

from __future__ import annotations

import os
from typing import Any

from buyer_intelligence.commercial_budget_control_v1 import (
    BudgetDenied,
    commit_actual_usage,
    release_unused_reservation,
    reserve_budget,
)


def commercial_budget_enabled() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST") and os.getenv("M3_COMMERCIAL_BUDGET_ENFORCE", "").strip() == "":
        # Pytest default off unless explicitly forced
        return False
    return os.getenv("M3_COMMERCIAL_BUDGET_ENFORCE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def reserve_sam_call(*, purpose: str, opportunity_id: str | None = None) -> str | None:
    """Reserve 1 SAM call. Returns reservation_id or None when gate disabled."""
    if not commercial_budget_enabled():
        return None
    try:
        return str(
            reserve_budget(
                provider="sam",
                sam_calls=1,
                purpose=purpose,
                opportunity_id=opportunity_id,
            )["reservation_id"]
        )
    except BudgetDenied as exc:
        raise BudgetDenied(str(exc.reason), detail=exc.detail) from exc


def commit_sam_call(reservation_id: str | None, *, success: bool = True, count: bool = True) -> dict[str, Any]:
    if not reservation_id:
        return {"status": "SKIPPED"}
    if not count:
        return release_unused_reservation(reservation_id)
    return commit_actual_usage(
        reservation_id,
        actual_sam_calls=1,
        success=success,
        charge_uncertain=False,
    )

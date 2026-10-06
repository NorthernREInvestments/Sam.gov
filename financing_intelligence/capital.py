"""Owner capital account + per-deal reservations (no auto-spend, no double-count)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from financing_intelligence.constants import (
    BUILD,
    RES_CANCELED,
    RES_CONFIRMED,
    RES_PROPOSED,
    RES_RELEASED,
    RES_RETURNED,
    RES_SPENT,
)
from financing_intelligence.store import (
    load_capital,
    load_reservations,
    money,
    money_str,
    new_id,
    save_capital,
    save_reservations,
    _utc,
)


ACTIVE_RESERVATION_STATES = {RES_PROPOSED, RES_CONFIRMED, RES_SPENT}


def update_capital(
    *,
    business_cash: Any | None = None,
    unrestricted_additional_capital: Any | None = None,
    minimum_operating_reserve: Any | None = None,
    max_deploy_per_deal: Any | None = None,
    owner_contribution_allowed: bool | None = None,
    max_owner_contribution: Any | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    cap = load_capital()
    if business_cash is not None:
        cap["business_cash"] = money_str(business_cash)
    if unrestricted_additional_capital is not None:
        cap["unrestricted_additional_capital"] = money_str(unrestricted_additional_capital)
    if minimum_operating_reserve is not None:
        cap["minimum_operating_reserve"] = money_str(minimum_operating_reserve)
    if max_deploy_per_deal is not None:
        cap["max_deploy_per_deal"] = None if max_deploy_per_deal == "" else money_str(max_deploy_per_deal)
    if owner_contribution_allowed is not None:
        cap["owner_contribution_allowed"] = bool(owner_contribution_allowed)
    if max_owner_contribution is not None:
        cap["max_owner_contribution"] = money_str(max_owner_contribution)
    if notes is not None:
        cap["notes"] = notes
    return save_capital(cap)


def active_commitments_total(*, exclude_opportunity_id: str | None = None) -> Decimal:
    total = Decimal("0")
    for r in load_reservations().get("items") or []:
        if r.get("status") not in ACTIVE_RESERVATION_STATES:
            continue
        if exclude_opportunity_id and r.get("opportunity_id") == exclude_opportunity_id:
            continue
        if r.get("status") == RES_PROPOSED:
            continue  # proposed does not reserve until confirmed
        total += money(r.get("amount"))
    return total


def capital_snapshot(*, exclude_opportunity_id: str | None = None) -> dict[str, Any]:
    cap = load_capital()
    business = money(cap.get("business_cash"))
    extra = money(cap.get("unrestricted_additional_capital"))
    reserve = money(cap.get("minimum_operating_reserve"))
    committed = active_commitments_total(exclude_opportunity_id=exclude_opportunity_id)
    deployable = business + extra - reserve - committed
    if deployable < 0:
        deployable = Decimal("0")
    max_per = cap.get("max_deploy_per_deal")
    if max_per not in (None, ""):
        deployable = min(deployable, money(max_per))
    return {
        "kind": "CapitalSnapshot",
        "build": BUILD,
        "business_cash": money_str(business),
        "unrestricted_additional_capital": money_str(extra),
        "minimum_operating_reserve": money_str(reserve),
        "active_commitments": money_str(committed),
        "deployable_capital": money_str(deployable),
        "owner_contribution_allowed": bool(cap.get("owner_contribution_allowed")),
        "max_owner_contribution": money_str(cap.get("max_owner_contribution") or 0),
        "max_deploy_per_deal": cap.get("max_deploy_per_deal"),
        "never_auto_spend": True,
        "note": "Deployable capital is potential only — per-deal confirmation required before use.",
    }


def propose_reservation(
    *,
    opportunity_id: str,
    amount: Any,
    reason: str = "deal_capital_need",
) -> dict[str, Any]:
    doc = load_reservations()
    item = {
        "reservation_id": new_id("CAP"),
        "opportunity_id": opportunity_id,
        "amount": money_str(amount),
        "status": RES_PROPOSED,
        "reason": reason,
        "created_at": _utc(),
        "confirmed_at": None,
        "confirmed_by": None,
        "actual_balance_declared": None,
    }
    doc.setdefault("items", []).append(item)
    save_reservations(doc)
    return item


def confirm_reservation(
    *,
    reservation_id: str,
    confirmed_by: str = "owner",
    actual_available_balance_today: Any | None = None,
) -> dict[str, Any]:
    """Explicit owner confirmation — required before company cash closes a stack."""
    doc = load_reservations()
    found = None
    for r in doc.get("items") or []:
        if r.get("reservation_id") == reservation_id:
            found = r
            break
    if not found:
        return {"ok": False, "error": "reservation_not_found"}
    need = money(found.get("amount"))
    snap = capital_snapshot(exclude_opportunity_id=found.get("opportunity_id"))
    # If owner declares today's balance, use it for this confirmation check only
    if actual_available_balance_today is not None:
        declared = money(actual_available_balance_today)
        reserve = money(snap["minimum_operating_reserve"])
        # commitments already exclude this opp
        committed = money(snap["active_commitments"])
        safe = declared - reserve - committed
        if safe < need:
            return {
                "ok": False,
                "error": "insufficient_declared_balance",
                "safe_deployable": money_str(max(Decimal("0"), safe)),
                "required": money_str(need),
            }
        found["actual_balance_declared"] = money_str(declared)
    else:
        if money(snap["deployable_capital"]) < need:
            return {
                "ok": False,
                "error": "insufficient_deployable_capital",
                "deployable": snap["deployable_capital"],
                "required": money_str(need),
            }
    found["status"] = RES_CONFIRMED
    found["confirmed_at"] = _utc()
    found["confirmed_by"] = confirmed_by
    save_reservations(doc)
    return {"ok": True, "reservation": found, "capital": capital_snapshot()}


def release_reservation(reservation_id: str, *, status: str = RES_RELEASED) -> dict[str, Any]:
    if status not in {RES_RELEASED, RES_RETURNED, RES_CANCELED, RES_SPENT}:
        status = RES_RELEASED
    doc = load_reservations()
    for r in doc.get("items") or []:
        if r.get("reservation_id") == reservation_id:
            r["status"] = status
            r["released_at"] = _utc()
            save_reservations(doc)
            return {"ok": True, "reservation": r, "capital": capital_snapshot()}
    return {"ok": False, "error": "reservation_not_found"}


def confirmed_amount_for_opportunity(opportunity_id: str) -> Decimal:
    total = Decimal("0")
    for r in load_reservations().get("items") or []:
        if r.get("opportunity_id") != opportunity_id:
            continue
        if r.get("status") == RES_CONFIRMED:
            total += money(r.get("amount"))
    return total


def capital_confirmation_card(
    *,
    opportunity_id: str,
    required_amount: Any,
) -> dict[str, Any]:
    need = money(required_amount)
    snap = capital_snapshot(exclude_opportunity_id=opportunity_id)
    remaining_after = money(snap["deployable_capital"]) - need
    return {
        "kind": "CapitalConfirmationRequired",
        "title": "Capital Confirmation Required",
        "opportunity_id": opportunity_id,
        "recorded_business_cash": snap["business_cash"],
        "existing_commitments": snap["active_commitments"],
        "required_reserve": snap["minimum_operating_reserve"],
        "recorded_deployable_capital": snap["deployable_capital"],
        "deal_requires_company_capital": money_str(need),
        "expected_remaining_if_confirmed": money_str(max(Decimal("0"), remaining_after)),
        "action_label": f"Confirm ${money_str(need)} Capital Commitment",
        "never_auto_use": True,
        "build": BUILD,
    }

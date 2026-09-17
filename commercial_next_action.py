"""Commercial next-action helpers — dependency-aware, no Stage jargon."""

from __future__ import annotations
from application_clock import now_utc, today_local

from datetime import date
from typing import Any

from bom_gate import BOM_COMPLETE
from commercial_execution import evaluate_co_clarification_timing
from deal_readiness import BID_READY, DEAL_READY
from quote_validation import QUOTE_VALID
from solicitation_package import PACKAGE_COMPLETE, PACKAGE_INCOMPLETE, PACKAGE_UNRESOLVED


def commercial_primary_action(
    *,
    workspace: dict[str, Any],
    missing_items: list[dict[str, Any]] | None = None,
    today: date | None = None,
) -> dict[str, Any] | None:
    """
    Prefer commercial sequence over CO clarification unless deadline elevates CO.
    Returns a next-action dict or None to fall through to generic engine.
    """
    ws = workspace or {}
    as_of = today or today_local()
    opp = ws.get("opportunity") or {}
    deal_id = opp.get("id")
    due = None
    if opp.get("due_date"):
        try:
            due = date.fromisoformat(str(opp["due_date"])[:10])
        except ValueError:
            due = None

    pkg = (ws.get("solicitation_package") or {}).get("status")
    if pkg in {PACKAGE_INCOMPLETE, PACKAGE_UNRESOLVED}:
        return None  # package still owns priority

    bom_gate = ws.get("bom_gate") or {}
    if bom_gate.get("status") != BOM_COMPLETE:
        return None

    quotes = ws.get("quotes") or []
    commercial = ws.get("commercial") or {}
    has_valid = any(
        (q.get("validation") or {}).get("status") == QUOTE_VALID
        or q.get("validation_status") == QUOTE_VALID
        for q in quotes
    )
    quote_requested = bool(commercial.get("quote_requested")) or any(
        a.get("activity_type") == "QUOTE_REQUESTED" for a in (ws.get("activities") or [])
    )
    channel_ok = str(commercial.get("channel_status") or ws.get("channel_status") or "").upper() in {
        "CHANNEL_RESOLVED",
        "RESOLVED",
        "VERIFIED",
    }
    financing_status = str((ws.get("financing") or {}).get("status") or "").upper()
    financing_pass = financing_status == "FINANCING_PASS"
    proposed = commercial.get("proposed_bid") or {}
    proposed_set = proposed.get("amount") is not None or (
        (ws.get("economics") or {}).get("proposed_bid_price") or {}
    ).get("value") is not None
    profit_status = str(
        ((ws.get("economics") or {}).get("actual_profit_status"))
        or ((ws.get("economics") or {}).get("actual_profit_result") or {}).get("status")
        or ""
    ).upper()
    deal_rd = ws.get("deal_readiness") or {}
    bid_rd = ws.get("bid_readiness") or {}

    # CO timing — may elevate FOB
    fob_item = None
    for m in missing_items or []:
        if "fob" in str(m.get("fact_key") or "").lower() or "freight" in str(m.get("description") or "").lower():
            if m.get("safe_to_ask_co") or "CO_CLARIFICATION" in str(m.get("status") or ""):
                fob_item = m
                break
    co_timing = evaluate_co_clarification_timing(
        co_ready=bool(fob_item),
        co_topic="FOB / freight responsibility",
        has_valid_quote=has_valid,
        bid_deadline=due,
        today=as_of,
        hours_remaining=(ws.get("deadline_urgency") or {}).get("hours_remaining_approx"),
    )

    def act(action: str, why: str, priority: int, kind: str = "COMMERCIAL") -> dict[str, Any]:
        return {
            "priority": priority,
            "deadline": due.isoformat() if due else None,
            "opportunity_id": deal_id,
            "organization": None,
            "contact": None,
            "action": action,
            "why": why,
            "due_at": due.isoformat() if due else None,
            "status": "OPEN",
            "blocking": True,
            "kind": kind,
        }

    if co_timing.get("first_action"):
        return act(
            "Review/send CO clarification: FOB / freight responsibility",
            co_timing.get("reason") or "Deadline risk elevates clarification",
            8,
            "CO_CLARIFICATION",
        )

    if not has_valid:
        if quote_requested:
            return act(
                "FOLLOW UP FOR QUOTE",
                "Quote requested — await supplier response",
                22,
                "SUPPLIER",
            )
        if bom_gate.get("supplier_quote_request_ready"):
            return act(
                "OBTAIN SUPPLIER QUOTE",
                "BOM complete — get firm reseller quote before downstream commercial work",
                11,
                "SUPPLIER",
            )

    if has_valid and not channel_ok:
        return act(
            "VERIFY FEDERAL CHANNEL / OEM LETTER",
            "Valid commercial quote path started but channel/OEM letter unresolved",
            20,
            "SUPPLIER",
        )

    if has_valid and not financing_pass:
        return act(
            "VERIFY FINANCING",
            "Commercial quote present but financing hard requirements unresolved",
            24,
            "FINANCING",
        )

    if has_valid and financing_pass and not proposed_set:
        return act(
            "SET PROPOSED BID PRICE / REVIEW ECONOMICS",
            "Commercial gates progressing — operator must set COMPANY_PROPOSED_BID_PRICE",
            26,
            "ECONOMICS",
        )

    if proposed_set and profit_status not in {"CALCULATED", "ECON_CALCULATED"}:
        return act(
            "COMPLETE REQUIRED COST INPUTS FOR ACTUAL PROFIT",
            "Proposed bid set but actual profit incomplete",
            28,
            "ECONOMICS",
        )

    if deal_rd.get("status") == DEAL_READY and bid_rd.get("status") != BID_READY:
        blockers = bid_rd.get("blockers") or ["required bid items"]
        return act(
            f"COMPLETE BID PACKAGE: {blockers[0]}",
            "Deal Ready — finish bid package",
            16,
            "BID",
        )

    if bid_rd.get("status") == BID_READY:
        return act(
            "Final operator review / submission preparation.",
            "Bid Ready",
            12,
            "SUBMISSION",
        )

    # CO ready but not first — surface as secondary via None so engine can still list it
    return None

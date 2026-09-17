"""OperatorActionQueue + deal-specific call sheets. No automatic communication."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from executable_deal_constants import (
    ACTION_CALL_FINANCIER,
    ACTION_CALL_SUPPLIER,
    ACTION_DOWNLOAD_AUTH,
    ACTION_OBTAIN_FREIGHT,
    ACTION_REVIEW_PG,
)
from operating_mode import classify_action_timing, future_action_phrasing, is_development_no_outreach
from pursuit_qualification_constants import ACTION_TIMING_FUTURE, ACTION_TIMING_NOW


def _utc() -> str:
    return now_utc().isoformat()


def operator_action(
    action_type: str,
    *,
    deal_id: str,
    why: str,
    who_where: str,
    questions: list[str],
    information_expected: list[str],
    unlocks_stage: str,
    priority: int = 50,
    deadline: str | None = None,
    pursuit_state: str | None = None,
    force_timing: str | None = None,
) -> dict[str, Any]:
    timing = force_timing or classify_action_timing(action_type=action_type, pursuit_state=pursuit_state)
    why_out = future_action_phrasing(why) if timing == ACTION_TIMING_FUTURE else why
    return {
        "action_id": f"OA-{uuid4().hex[:10]}",
        "action_type": action_type,
        "priority": priority,
        "deal_id": deal_id,
        "why_needed": why_out,
        "who_where": who_where,
        "questions_tasks": list(questions),
        "information_expected_back": list(information_expected),
        "deadline": deadline,
        "unlocks_pipeline_stage": unlocks_stage,
        "created_at": _utc(),
        "status": "OPEN",
        "auto_communication": False,
        "action_timing": timing,
        "development_no_outreach": is_development_no_outreach(),
        "imperative_now": timing == ACTION_TIMING_NOW,
    }


class OperatorActionQueue:
    def __init__(self) -> None:
        self._actions: list[dict[str, Any]] = []

    def add(self, action: dict[str, Any]) -> dict[str, Any]:
        # Dedupe same type+deal+who
        key = (action["action_type"], action["deal_id"], action.get("who_where"))
        for existing in self._actions:
            if (
                existing["status"] == "OPEN"
                and (existing["action_type"], existing["deal_id"], existing.get("who_where")) == key
            ):
                return existing
        self._actions.append(action)
        return action

    def open_actions(self, deal_id: str | None = None) -> list[dict[str, Any]]:
        rows = [a for a in self._actions if a["status"] == "OPEN"]
        if deal_id:
            rows = [a for a in rows if a["deal_id"] == deal_id]
        return sorted(rows, key=lambda a: a["priority"])

    def export(self) -> list[dict[str, Any]]:
        return deepcopy(self._actions)


def build_supplier_call_sheet(
    *,
    deal: dict[str, Any],
    supplier: dict[str, Any],
    line_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    dest = deal.get("delivery_destination") or deal.get("ship_to") or "delivery location per solicitation"
    deadline = deal.get("bid_deadline") or deal.get("deadline")
    qs = [
        "Quote for specified quantities / line items",
        "Confirm product meets solicitation specification",
        "Confirm manufacturer / part numbers",
        "Confirm lead time",
        f"Confirm direct shipment to {dest}",
        "Confirm freight included or excluded (and amount if excluded)",
        "Confirm deposit / payment terms (do not assume Net-30)",
        f"Confirm quote validity through bid deadline ({deadline})",
    ]
    return {
        "kind": "SupplierCallSheet",
        "deal_id": deal.get("solicitation_number") or deal.get("deal_id"),
        "supplier": supplier.get("name") or supplier.get("supplier"),
        "contact": supplier.get("contact") or supplier.get("url"),
        "questions": qs,
        "line_items_summary": [
            {
                "description": li.get("description"),
                "quantity": li.get("quantity"),
                "unit": li.get("unit"),
            }
            for li in (line_items or [])[:20]
        ],
        "auto_send": False,
        "note": "Operator-controlled communication only",
    }


def build_financier_deal_call_sheet(
    *,
    deal: dict[str, Any],
    provider: dict[str, Any],
    economics: dict[str, Any] | None = None,
    working_capital: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deal-specific financier questions — reuses pre_bid patterns, adds transaction facts."""
    qs = [
        "Would you finance this specific government product transaction if awarded?",
        "Can you provide a conditional indication before bid, subject to award?",
        "How much of supplier cost can be funded?",
        "Is personal cash required?",
        "Is personal credit pulled?",
        "Is personal credit used materially for approval?",
        "Is there a minimum FICO?",
        "Can approximately 480 personal credit be acceptable if transaction economics are strong?",
        "Is a PG required? If so, what type?",
        "What are fees?",
        "When is supplier paid?",
        "What documents are needed?",
        "What must be true before we submit the bid?",
    ]
    return {
        "kind": "FinancierCallSheet",
        "deal_id": deal.get("solicitation_number") or deal.get("deal_id"),
        "provider": provider.get("source_name") or provider.get("provider_id") or provider.get("name"),
        "transaction_summary": {
            "buyer": deal.get("agency"),
            "solicitation": deal.get("solicitation_number"),
            "product": deal.get("title") or deal.get("product"),
            "bid_deadline": deal.get("bid_deadline") or deal.get("deadline"),
            "supplier": deal.get("supplier_name"),
            "supplier_quote": (economics or {}).get("supplier_cost"),
            "funding_amount": (working_capital or {}).get("working_capital_required"),
            "expected_profit": (economics or {}).get("expected_actual_profit"),
            "operator_fico_approx": 480,
            "zero_personal_cash": True,
        },
        "questions": qs,
        "auto_send": False,
        "note": "Operator-controlled communication only",
    }


def enqueue_supplier_quote_action(
    queue: OperatorActionQueue,
    *,
    deal: dict[str, Any],
    supplier: dict[str, Any],
    line_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sheet = build_supplier_call_sheet(deal=deal, supplier=supplier, line_items=line_items)
    action = operator_action(
        ACTION_CALL_SUPPLIER,
        deal_id=str(deal.get("solicitation_number") or deal.get("deal_id")),
        why="Formal supplier quote required before bid economics can mature",
        who_where=str(supplier.get("name") or supplier.get("supplier") or "supplier"),
        questions=sheet["questions"],
        information_expected=[
            "quoted_total",
            "unit_pricing",
            "freight_included",
            "payment_terms",
            "lead_time",
            "direct_ship",
            "product_match",
            "quote_expiration",
        ],
        unlocks_stage="SUPPLIER_COSTING",
        priority=20,
        deadline=deal.get("bid_deadline") or deal.get("deadline"),
    )
    action["call_sheet"] = sheet
    return queue.add(action)


def enqueue_financier_action(
    queue: OperatorActionQueue,
    *,
    deal: dict[str, Any],
    provider: dict[str, Any],
    economics: dict[str, Any] | None = None,
    working_capital: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sheet = build_financier_deal_call_sheet(
        deal=deal, provider=provider, economics=economics, working_capital=working_capital
    )
    action = operator_action(
        ACTION_CALL_FINANCIER,
        deal_id=str(deal.get("solicitation_number") or deal.get("deal_id")),
        why="Deal economics mature enough for meaningful pre-bid financing call",
        who_where=str(sheet["provider"]),
        questions=sheet["questions"],
        information_expected=[
            "conditional_indication",
            "coverage",
            "fees",
            "credit_role",
            "min_fico",
            "pg_requirement",
            "cash_required",
            "timing",
        ],
        unlocks_stage="FUNDING_COMPATIBILITY",
        priority=40,
    )
    action["call_sheet"] = sheet
    return queue.add(action)

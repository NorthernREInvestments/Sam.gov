"""Financing transaction outcomes — learn from actual deals (never auto-rewrite rules)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from financing_intelligence.constants import BUILD, TX_NOT_CONTACTED
from financing_intelligence.store import load_outcomes, money, money_str, new_id, save_outcomes, _utc

MONEY_KEYS = {
    "amount_requested",
    "product_cost",
    "contract_value",
    "quoted_fee",
    "actual_fee",
    "final_financing_cost",
    "estimated_financing_cost",
    "actual_funded_amount",
    "actual_owner_cash_required",
    "supplier_credit_amount_used",
    "actual_final_profit",
    "variance",
}

OUTCOME_FIELDS = (
    "opportunity_id",
    "contract_id",
    "source_id",
    "lender_contacted",
    "contact_date",
    "date_submitted",
    "amount_requested",
    "product_cost",
    "contract_value",
    "margin",
    "requested_advance",
    "quoted_advance",
    "actual_advance",
    "actual_advance_pct",
    "actual_funded_amount",
    "approved",
    "declined",
    "denied",
    "pending",
    "denial_reason",
    "decline_reason",
    "approval_date",
    "funding_date",
    "quoted_fee",
    "actual_fee",
    "quoted_rate",
    "actual_rate",
    "actual_approval_time_days",
    "actual_funding_time_days",
    "actual_owner_cash_required",
    "supplier_terms_actually_used",
    "supplier_credit_amount_used",
    "actual_government_payment_days",
    "actual_financing_duration_days",
    "actual_financing_cost",
    "estimated_financing_cost",
    "final_financing_cost",
    "variance",
    "supplier_payment_date",
    "repayment_date",
    "problems_encountered",
    "completed_successfully",
    "transaction_completed",
    "transaction_failed",
    "failure_reason",
    "capital_returned",
    "actual_final_profit",
    "status",
    "notes",
)


def record_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    doc = load_outcomes()
    oid = payload.get("outcome_id")
    existing = None
    if oid:
        for it in doc.get("items") or []:
            if it.get("outcome_id") == oid:
                existing = it
                break
    if existing is None:
        existing = {
            "outcome_id": new_id("TX"),
            "status": TX_NOT_CONTACTED,
            "created_at": _utc(),
            "history": [],
            "never_auto_promotes_rules": True,
        }
        doc.setdefault("items", []).append(existing)

    for key in OUTCOME_FIELDS:
        if key in payload and payload[key] is not None:
            val = payload[key]
            if key in MONEY_KEYS:
                val = money_str(val)
            existing[key] = val

    # Derived variance when both estimated and actual cost present
    if existing.get("estimated_financing_cost") not in (None, "") and existing.get("actual_financing_cost") not in (
        None,
        "",
    ):
        existing["variance"] = money_str(
            money(existing["actual_financing_cost"]) - money(existing["estimated_financing_cost"])
        )

    existing.setdefault("history", []).append(
        {"at": _utc(), "status": existing.get("status"), "note": payload.get("history_note")}
    )
    existing["updated_at"] = _utc()
    existing["build"] = BUILD
    existing["never_auto_promotes_rules"] = True
    save_outcomes(doc)
    return existing


def list_outcomes(*, source_id: str | None = None, opportunity_id: str | None = None) -> list[dict[str, Any]]:
    items = list(load_outcomes().get("items") or [])
    if source_id:
        items = [i for i in items if i.get("source_id") == source_id]
    if opportunity_id:
        items = [i for i in items if i.get("opportunity_id") == opportunity_id]
    return items


def historical_outcome_patterns(source_id: str | None) -> dict[str, Any]:
    """Surface patterns for owner review — never authoritative lender rules."""
    if not source_id:
        return {
            "kind": "HistoricalOutcomePatterns",
            "authoritative_rule": False,
            "note": "No source",
            "recommended_review": False,
        }
    items = list_outcomes(source_id=source_id)
    decided = [
        i
        for i in items
        if i.get("status") in {"APPROVED", "DENIED", "FUNDED", "REPAID", "COMPLETED"}
        or i.get("approved") is True
        or i.get("declined") is True
        or i.get("denied") is True
    ]
    approved = [
        i
        for i in decided
        if i.get("status") in {"APPROVED", "FUNDED", "REPAID", "COMPLETED"} or i.get("approved") is True
    ]
    advances = []
    approval_days = []
    for i in approved:
        adv = i.get("actual_advance_pct") or i.get("actual_advance")
        if adv not in (None, ""):
            try:
                advances.append(Decimal(str(adv).replace("%", "")))
            except Exception:
                pass
        d = i.get("actual_approval_time_days")
        if d not in (None, ""):
            try:
                approval_days.append(Decimal(str(d)))
            except Exception:
                pass

    avg_adv = None
    avg_days = None
    if advances:
        avg_adv = str((sum(advances) / Decimal(len(advances))).quantize(Decimal("0.1")))
    if approval_days:
        avg_days = str((sum(approval_days) / Decimal(len(approval_days))).quantize(Decimal("0.1")))

    n = len(decided)
    a = len(approved)
    summary = None
    if n > 0:
        summary = (
            f"Observed: lender approved {a} of {n} recorded opportunities in this set. "
            + (f"Average observed advance: {avg_adv}%. " if avg_adv else "")
            + (f"Average observed approval time: {avg_days} business days. " if avg_days else "")
            + "This is historical outcome data, not a verified lender rule."
        )

    return {
        "kind": "HistoricalOutcomePatterns",
        "source_id": source_id,
        "recorded": n,
        "approved_count": a,
        "average_observed_advance_pct": avg_adv,
        "average_observed_approval_days": avg_days,
        "summary": summary,
        "authoritative_rule": False,
        "recommended_review": n >= 2,
        "proposed_facts": [],  # never auto-written; owner may promote separately
        "note": "Historical outcomes must not silently rewrite verified lender rules.",
        "build": BUILD,
    }

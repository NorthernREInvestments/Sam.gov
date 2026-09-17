"""M3 1.0 canonical lifecycle + next-action — derived from subsystem evidence."""

from __future__ import annotations

from typing import Any

# High-level lifecycle (summarizes; does not replace subsystem states)
LC_DISCOVERED = "DISCOVERED"
LC_NORMALIZED = "NORMALIZED"
LC_CHEAP_SCREENED = "CHEAP_SCREENED"
LC_REJECTED_CHEAP = "REJECTED_CHEAP_SCREEN"
LC_RESEARCH_QUEUED = "RESEARCH_QUEUED"
LC_RESEARCH_IN_PROGRESS = "RESEARCH_IN_PROGRESS"
LC_PACKAGE_REQUIRED = "PACKAGE_REQUIRED"
LC_PACKAGE_ACQUIRED = "PACKAGE_ACQUIRED"
LC_PACKAGE_GATED = "PACKAGE_ACCESS_GATED"
LC_REQUIREMENTS_PARSED = "REQUIREMENTS_PARSED"
LC_BOM_READY = "BOM_READY"
LC_ECONOMICS_IN_PROGRESS = "ECONOMICS_IN_PROGRESS"
LC_ECONOMICS_PRELIMINARY = "ECONOMICS_PRELIMINARY"
LC_ECONOMICS_ATTRACTIVE = "ECONOMICS_ATTRACTIVE"
LC_ECONOMICS_UNATTRACTIVE = "ECONOMICS_UNATTRACTIVE"
LC_FUNDING_VERIFICATION = "FUNDING_VERIFICATION_REQUIRED"
LC_COMPLIANCE_IN_PROGRESS = "COMPLIANCE_IN_PROGRESS"
LC_COMPLIANCE_BLOCKED = "COMPLIANCE_BLOCKED"
LC_PRICING_IN_PROGRESS = "PRICING_IN_PROGRESS"
LC_COMMERCIAL_VERIFICATION = "COMMERCIAL_VERIFICATION_REQUIRED"
LC_DRAFT_BID_READY = "DRAFT_BID_READY"
LC_EXECUTION_NOT_READY = "EXECUTION_NOT_READY"
LC_READY_FOR_OPERATOR = "READY_FOR_OPERATOR_ACTION"
LC_CLOSED = "CLOSED"
LC_CANCELLED = "CANCELLED"
LC_AWARDED = "AWARDED"
LC_LOST = "LOST"
LC_ARCHIVED = "ARCHIVED"
LC_REJECTED = "REJECTED"

# Next-action classes
NA_AUTO_CONTINUE = "AUTO_CONTINUE"
NA_QUEUE_RESEARCH = "QUEUE_RESEARCH"
NA_WAIT_BUDGET = "WAIT_BUDGET"
NA_WAIT_PACKAGE = "WAIT_PACKAGE"
NA_WAIT_PUBLIC_EVIDENCE = "WAIT_PUBLIC_EVIDENCE"
NA_WAIT_OPERATOR = "WAIT_OPERATOR"
NA_WAIT_COMMERCIAL = "WAIT_COMMERCIAL_VERIFICATION"
NA_WAIT_FUNDING = "WAIT_FUNDING_VERIFICATION"
NA_WAIT_COMPLIANCE = "WAIT_COMPLIANCE_RESOLUTION"
NA_NO_ACTION_REJECTED = "NO_ACTION_REJECTED"
NA_NO_ACTION_CLOSED = "NO_ACTION_CLOSED"
NA_NO_ACTION_CANCELLED = "NO_ACTION_CANCELLED"
NA_READY_FOR_OPERATOR = "READY_FOR_OPERATOR_ACTION"


def derive_lifecycle(record: dict[str, Any]) -> str:
    """Derive one top-level lifecycle from evidence + subsystem results."""
    status = str(record.get("status") or record.get("current_status") or "").upper()
    if status in {"CANCELLED", "CANCELED"}:
        return LC_CANCELLED
    if status in {"AWARDED"}:
        return LC_AWARDED
    if status in {"CLOSED", "EXPIRED"}:
        return LC_CLOSED
    if record.get("lifecycle_override"):
        return str(record["lifecycle_override"])

    if record.get("rejected_cheap_screen") or record.get("cheap_screen_survive") is False:
        return LC_REJECTED_CHEAP
    if record.get("rejected") or record.get("pipeline_stage") == "DEAL_REJECTED":
        reason = str(record.get("stop_reason") or "")
        if "economics" in reason.lower() or reason == "economics_below_profit_floor":
            return LC_ECONOMICS_UNATTRACTIVE
        return LC_REJECTED

    readiness = str(record.get("operator_readiness") or "")
    stop = str(record.get("stop_reason") or "")
    stage = str(record.get("pipeline_stage") or "")
    pkg = str(record.get("package_access") or record.get("document_access") or "").upper()

    if record.get("draft_bid_ready") or record.get("draft_bid_package"):
        if readiness in {"READY_FOR_BID_PREPARATION"} or record.get("ready_for_operator"):
            return LC_READY_FOR_OPERATOR
        return LC_DRAFT_BID_READY

    if readiness == "READY_FOR_BID_PREPARATION" or stage == "DEAL_READY_FOR_BID_PREPARATION":
        if record.get("commercial_verification_plan"):
            return LC_COMMERCIAL_VERIFICATION
        return LC_READY_FOR_OPERATOR

    if readiness == "COMPLIANCE_REVIEW_REQUIRED" or "COMPLIANCE" in stop.upper():
        return LC_COMPLIANCE_BLOCKED if record.get("compliance_blockers") else LC_COMPLIANCE_IN_PROGRESS

    if readiness in {"FUNDING_VERIFICATION_REQUIRED", "FUNDING_CALL_READY"} or "FUNDING" in stop.upper():
        return LC_FUNDING_VERIFICATION

    if readiness == "QUOTE_REQUIRED" or stop == "QUOTE_REQUIRED":
        return LC_COMMERCIAL_VERIFICATION

    if readiness == "ECONOMICS_REVIEW" or stage == "ECONOMIC_QUALIFICATION":
        profit = record.get("expected_actual_profit")
        if profit is not None and record.get("economics_supported"):
            return LC_ECONOMICS_ATTRACTIVE if float(profit) >= 10000 else LC_ECONOMICS_UNATTRACTIVE
        return LC_ECONOMICS_PRELIMINARY

    if record.get("bom") or record.get("line_items"):
        if record.get("economics") or record.get("transaction_economics"):
            return LC_ECONOMICS_IN_PROGRESS
        return LC_BOM_READY

    if record.get("requirements_parsed") or record.get("governing_documents"):
        return LC_REQUIREMENTS_PARSED

    if pkg in {"AUTH_GATED", "REGISTRATION_REQUIRED"} or stop == "AUTH_REQUIRED":
        return LC_PACKAGE_GATED

    if record.get("package_acquired") or record.get("documents"):
        return LC_PACKAGE_ACQUIRED

    if record.get("research_in_progress"):
        return LC_RESEARCH_IN_PROGRESS

    if record.get("research_queued") or record.get("m3_stage") in {
        "RESEARCH_QUEUED",
        "AUTONOMOUS_RESEARCH",
        "CHEAP_SCREENING",
    }:
        if record.get("cheap_screen_survive"):
            return LC_RESEARCH_QUEUED
        return LC_CHEAP_SCREENED

    if record.get("cheap_screen_survive"):
        return LC_RESEARCH_QUEUED

    if record.get("canonical_id") or record.get("identity_key") or record.get("normalized"):
        return LC_NORMALIZED

    if record.get("external_id") or record.get("title"):
        return LC_DISCOVERED

    return LC_DISCOVERED


def determine_next_action(record: dict[str, Any], *, budget_blocked: bool = False) -> dict[str, Any]:
    """Deterministic WHAT SHOULD HAPPEN NEXT from lifecycle + evidence."""
    lc = derive_lifecycle(record)
    deadline_status = str(
        (record.get("deadline_evaluation") or {}).get("status")
        or record.get("deadline_status")
        or ""
    ).upper()

    if lc in {LC_CANCELLED}:
        return {"next_action": NA_NO_ACTION_CANCELLED, "lifecycle": lc, "reason": "cancelled"}
    if lc in {LC_CLOSED, LC_AWARDED, LC_LOST, LC_ARCHIVED}:
        return {"next_action": NA_NO_ACTION_CLOSED, "lifecycle": lc, "reason": "terminal"}
    if lc in {LC_REJECTED_CHEAP, LC_REJECTED, LC_ECONOMICS_UNATTRACTIVE}:
        return {"next_action": NA_NO_ACTION_REJECTED, "lifecycle": lc, "reason": record.get("stop_reason") or lc}

    if deadline_status in {"EXPIRED", "TOO_LATE"} and not record.get("allow_historical"):
        return {
            "next_action": NA_NO_ACTION_REJECTED,
            "lifecycle": lc,
            "reason": "deadline_too_late",
            "deadline_status": deadline_status,
        }

    if budget_blocked and lc in {
        LC_RESEARCH_QUEUED,
        LC_RESEARCH_IN_PROGRESS,
        LC_ECONOMICS_IN_PROGRESS,
        LC_PRICING_IN_PROGRESS,
    }:
        return {"next_action": NA_WAIT_BUDGET, "lifecycle": lc, "reason": "cost_governor_blocked"}

    if lc == LC_PACKAGE_GATED:
        return {
            "next_action": NA_WAIT_OPERATOR,
            "lifecycle": lc,
            "reason": "package_access_gated",
            "operator_action_type": "ACCESS_REGISTRATION_GATED_PACKAGE",
        }

    if lc in {LC_FUNDING_VERIFICATION}:
        return {
            "next_action": NA_WAIT_FUNDING,
            "lifecycle": lc,
            "reason": "funding_verification_required",
            "note": "UNKNOWN financing ≠ rejection",
        }

    if lc in {LC_COMPLIANCE_BLOCKED}:
        return {
            "next_action": NA_WAIT_COMPLIANCE,
            "lifecycle": lc,
            "reason": "mandatory_compliance_unresolved",
        }

    if lc in {LC_COMMERCIAL_VERIFICATION}:
        return {
            "next_action": NA_WAIT_COMMERCIAL,
            "lifecycle": lc,
            "reason": "commercial_verification_required",
        }

    if lc in {LC_READY_FOR_OPERATOR, LC_DRAFT_BID_READY}:
        return {
            "next_action": NA_READY_FOR_OPERATOR,
            "lifecycle": lc,
            "reason": "human_authorization_or_external_evidence_required",
        }

    if lc in {LC_DISCOVERED, LC_NORMALIZED}:
        return {"next_action": NA_AUTO_CONTINUE, "lifecycle": lc, "reason": "cheap_screen_pending"}

    if lc in {LC_CHEAP_SCREENED, LC_RESEARCH_QUEUED}:
        return {"next_action": NA_QUEUE_RESEARCH, "lifecycle": lc, "reason": "advance_research"}

    if lc in {LC_PACKAGE_REQUIRED, LC_RESEARCH_IN_PROGRESS}:
        if not record.get("package_acquired") and not record.get("documents"):
            return {"next_action": NA_WAIT_PACKAGE, "lifecycle": lc, "reason": "need_public_package"}
        return {"next_action": NA_AUTO_CONTINUE, "lifecycle": lc, "reason": "continue_research"}

    if lc in {
        LC_PACKAGE_ACQUIRED,
        LC_REQUIREMENTS_PARSED,
        LC_BOM_READY,
        LC_ECONOMICS_IN_PROGRESS,
        LC_ECONOMICS_PRELIMINARY,
        LC_ECONOMICS_ATTRACTIVE,
        LC_COMPLIANCE_IN_PROGRESS,
        LC_PRICING_IN_PROGRESS,
    }:
        return {"next_action": NA_AUTO_CONTINUE, "lifecycle": lc, "reason": "free_or_authorized_stage"}

    if lc == LC_EXECUTION_NOT_READY:
        return {"next_action": NA_WAIT_OPERATOR, "lifecycle": lc, "reason": "execution_blockers"}

    return {"next_action": NA_WAIT_PUBLIC_EVIDENCE, "lifecycle": lc, "reason": "insufficient_evidence"}


def readiness_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Operator-facing coherent readiness answers."""
    lc = derive_lifecycle(record)
    nxt = determine_next_action(record)
    economics = record.get("transaction_economics") or record.get("economics") or {}
    profit = economics.get("expected_actual_profit")
    if profit is None:
        profit = record.get("expected_actual_profit")
    capital = (
        (record.get("funding_requirement") or {}).get("capital_amount")
        or record.get("working_capital_required")
    )
    return {
        "kind": "M3ReadinessSummary",
        "lifecycle": lc,
        "worth_pursuing": lc
        not in {LC_REJECTED, LC_REJECTED_CHEAP, LC_CANCELLED, LC_CLOSED, LC_ECONOMICS_UNATTRACTIVE},
        "what_we_know": {
            "title": record.get("title"),
            "buyer": record.get("agency") or record.get("buyer"),
            "deadline": record.get("deadline") or record.get("deadline_raw"),
            "package_access": record.get("package_access"),
            "product_category": record.get("product_category") or record.get("product_classification"),
            "pipeline_stage": record.get("pipeline_stage"),
            "operator_readiness": record.get("operator_readiness"),
        },
        "what_we_dont_know": [
            k
            for k, v in {
                "acquisition_cost": record.get("acquisition_cost"),
                "financing_eligibility": record.get("funding_status"),
                "formal_supplier_quote": record.get("formal_quote"),
                "freight_confirmed": record.get("freight_confirmed"),
            }.items()
            if v in {None, "", "UNKNOWN", "QUOTE_REQUIRED"}
        ],
        "what_would_kill_it": record.get("kill_reasons")
        or ([record.get("stop_reason")] if record.get("rejected") else []),
        "what_must_happen_next": nxt,
        "what_m3_can_do_automatically": nxt["next_action"] == NA_AUTO_CONTINUE
        or nxt["next_action"] == NA_QUEUE_RESEARCH,
        "what_requires_operator": nxt["next_action"]
        in {NA_WAIT_OPERATOR, NA_WAIT_COMMERCIAL, NA_WAIT_FUNDING, NA_READY_FOR_OPERATOR, NA_WAIT_COMPLIANCE},
        "capital_requirement": capital if capital is not None else "UNKNOWN",
        "supported_expected_profit": profit if profit is not None else "UNKNOWN",
        "profit_confidence": economics.get("confidence") or record.get("profit_confidence") or "UNKNOWN",
        "time_left": (record.get("deadline_evaluation") or {}).get("calendar_days_remaining")
        or record.get("calendar_days_remaining")
        or "UNKNOWN",
        "why_not_ready": record.get("stop_reason") or nxt.get("reason"),
        "unknown_financing_is_not_rejection": True,
    }

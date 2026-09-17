"""User-facing deal lifecycle mapping — hides internal Stage0–3 jargon."""

from __future__ import annotations

from typing import Any

# Operator-facing lifecycle states
LIFECYCLE_NEW = "NEW"
LIFECYCLE_QUALIFYING = "QUALIFYING"
LIFECYCLE_RESEARCHING = "RESEARCHING"
LIFECYCLE_SOURCING = "SOURCING"
LIFECYCLE_FUNDING = "FUNDING"
LIFECYCLE_PRICING = "PRICING"
LIFECYCLE_DEAL_READY = "DEAL_READY"
LIFECYCLE_BUILDING_BID = "BUILDING_BID"
LIFECYCLE_BID_READY = "BID_READY"
LIFECYCLE_SUBMITTED = "SUBMITTED"
LIFECYCLE_AWARDED = "AWARDED"
LIFECYCLE_PERFORMING = "PERFORMING"
LIFECYCLE_INVOICED = "INVOICED"
LIFECYCLE_PAID = "PAID"
LIFECYCLE_CLOSED = "CLOSED"
LIFECYCLE_WATCH = "WATCH"
LIFECYCLE_REJECTED = "REJECTED"
LIFECYCLE_LOST = "LOST"

ACTIVE_LIFECYCLES = frozenset(
    {
        LIFECYCLE_QUALIFYING,
        LIFECYCLE_RESEARCHING,
        LIFECYCLE_SOURCING,
        LIFECYCLE_FUNDING,
        LIFECYCLE_PRICING,
        LIFECYCLE_DEAL_READY,
        LIFECYCLE_BUILDING_BID,
        LIFECYCLE_BID_READY,
        LIFECYCLE_SUBMITTED,
        LIFECYCLE_AWARDED,
        LIFECYCLE_PERFORMING,
        LIFECYCLE_INVOICED,
    }
)


def map_internal_to_lifecycle(
    *,
    decision: str | None = None,
    pipeline_stage: str | None = None,
    deal_readiness: dict[str, Any] | None = None,
    bid_readiness: dict[str, Any] | None = None,
    commercial_status: dict[str, Any] | None = None,
    funding_status: str | None = None,
    contract_status: str | None = None,
) -> str:
    """Map persisted internal fields to one operator lifecycle label."""
    decision = str(decision or "").upper()
    stage = str(pipeline_stage or "").lower()
    deal_st = (deal_readiness or {}).get("status")
    bid_st = (bid_readiness or {}).get("status")
    cs = commercial_status or {}

    if decision in {"REJECTED", "NOT_VIABLE"} or stage in {"rejected", "rejected_stage0"}:
        return LIFECYCLE_REJECTED
    if stage == "watch" or decision == "WATCH":
        return LIFECYCLE_WATCH
    if contract_status in {"lost", "LOST"}:
        return LIFECYCLE_LOST
    if contract_status in {"awarded", "AWARDED"}:
        return LIFECYCLE_AWARDED
    if contract_status in {"submitted", "SUBMITTED"}:
        return LIFECYCLE_SUBMITTED
    if contract_status in {"active", "performing"}:
        return LIFECYCLE_PERFORMING
    if bid_st == "BID_READY":
        return LIFECYCLE_BID_READY
    if deal_st == "DEAL_READY":
        return LIFECYCLE_BUILDING_BID if bid_st != "BID_READY" else LIFECYCLE_BID_READY
    if deal_st == "DEAL_READY" or (deal_readiness or {}).get("deal_ready"):
        return LIFECYCLE_DEAL_READY

    fund = str(funding_status or cs.get("financing_status") or "").upper()
    if fund in {"FINANCING_FAIL", "BLOCKED", "FUNDING_BLOCKED"}:
        return LIFECYCLE_FUNDING
    if cs.get("proposed_bid_set"):
        return LIFECYCLE_PRICING
    if cs.get("has_valid_quote") or cs.get("quote_requested"):
        return LIFECYCLE_FUNDING if fund not in {"FINANCING_PASS"} else LIFECYCLE_PRICING
    if cs.get("label") == "NEEDS_SUPPLIER_QUOTES" or stage in {"needs_research", "needs_stage2", "needs_stage3"}:
        return LIFECYCLE_SOURCING if stage.endswith("stage3") or cs else LIFECYCLE_RESEARCHING
    if stage in {"needs_stage1", "new", ""}:
        return LIFECYCLE_QUALIFYING if stage else LIFECYCLE_NEW
    if "research" in stage:
        return LIFECYCLE_RESEARCHING
    if "quote" in stage or "supplier" in stage:
        return LIFECYCLE_SOURCING
    return LIFECYCLE_QUALIFYING


def pipeline_bucket(
    *,
    lifecycle: str,
    workspace: dict[str, Any] | None = None,
) -> str:
    """Active Deals filter bucket for where a deal is stuck."""
    ws = workspace or {}
    pkg = (ws.get("solicitation_package") or {}).get("status") or ""
    bom = (ws.get("bom_gate") or {}).get("status") or ""
    cs = ws.get("commercial_status") or {}
    fund = str((ws.get("funding_plan") or {}).get("pre_bid_status") or cs.get("financing_status") or "")
    deal = (ws.get("deal_readiness") or {}).get("status")
    bid = (ws.get("bid_readiness") or {}).get("status")
    na = (ws.get("next_action") or {}).get("action") or ""

    if pkg not in {"SOLICITATION_PACKAGE_COMPLETE", "PACKAGE_COMPLETE"}:
        return "Needs Documents"
    if bom != "BOM_COMPLETE":
        return "Needs Requirements Review"
    if not cs.get("quote_requested") and not cs.get("has_valid_quote"):
        return "Needs Supplier"
    if cs.get("quote_requested") and not cs.get("has_valid_quote"):
        return "Waiting on Supplier"
    if not cs.get("has_valid_quote"):
        return "Needs Quote"
    if fund.upper() in {"NOT_RESEARCHED", "RESEARCHING", "NEEDS_CONTACT", "INSUFFICIENT_EVIDENCE"}:
        return "Needs Funding Research"
    if fund.upper() == "WAITING_ON_PROVIDER":
        return "Waiting on Financier"
    if fund.upper() in {"BLOCKED", "FINANCING_FAIL", "FUNDING_BLOCKED"}:
        return "Funding Blocked"
    if "CO clarification" in na or "CO CLARIFICATION" in na.upper():
        return "Needs CO Clarification"
    if lifecycle == LIFECYCLE_PRICING or not cs.get("proposed_bid_set"):
        return "Needs Pricing"
    if deal == "DEAL_READY" and bid != "BID_READY":
        return "Building Bid"
    if bid == "BID_READY":
        return "Bid Ready"
    if lifecycle == LIFECYCLE_SUBMITTED:
        return "Submitted"
    if deal == "DEAL_READY":
        return "Deal Ready"
    return "Needs Supplier"

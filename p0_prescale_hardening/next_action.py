"""Canonical next-action engine — exactly one primary next action."""

from __future__ import annotations

from typing import Any

from p0_prescale_hardening.models import (
    BASKET_PARTIAL,
    CANONICAL_STAGES,
    ECONOMICS_NOT_READY,
    ECONOMICS_READY,
    FIX_SOURCE_PROVENANCE,
    LENDER_READY,
    OWNER_CHANNEL_TEST_READY,
    PREPARE_QUOTE_PACKET,
    READY_FOR_ECONOMICS,
    READY_FOR_OWNER_QUOTE_OUTREACH,
    REJECT,
    RESOLVE_IDENTITY,
    RESOLVE_REVENUE,
    RESOLVE_SCOPE,
    REVIEW_FINANCING,
    REVIEW_RECEIVED_QUOTE,
    WAITING_FOR_SUPPLIER_QUOTE,
)


def determine_canonical_next_action(ctx: dict[str, Any]) -> dict[str, Any]:
    """Single primary next action from opportunity context."""
    if ctx.get("hard_reject"):
        return _act(REJECT, "Hard reject", stage="OPPORTUNITY")

    if ctx.get("scope") == "MIXED_SCOPE" or ctx.get("execution") == "EXECUTION_NOT_READY":
        return _act(RESOLVE_SCOPE, "Separate product vs install/service scope", stage="PRODUCTS")

    if ctx.get("missing_provenance"):
        return _act(FIX_SOURCE_PROVENANCE, "Attach page/sheet/cell provenance", stage="PRODUCTS")

    if ctx.get("identity_not_ready"):
        return _act(RESOLVE_IDENTITY, "Resolve weak/ambiguous identity", stage="PRODUCTS")

    if ctx.get("revenue_not_ready") and ctx.get("needs_economics"):
        return _act(RESOLVE_REVENUE, "Obtain usable product-scope revenue", stage="REVENUE")

    if ctx.get("packet_status") == OWNER_CHANNEL_TEST_READY:
        return _act(
            READY_FOR_OWNER_QUOTE_OUTREACH,
            "Owner channel packet ready — do not auto-send",
            stage="QUOTES",
        )

    if ctx.get("packet_status") == READY_FOR_OWNER_QUOTE_OUTREACH:
        return _act(READY_FOR_OWNER_QUOTE_OUTREACH, "Quote packet ready for owner send", stage="QUOTES")

    if not ctx.get("has_quote_packet"):
        return _act(PREPARE_QUOTE_PACKET, "Build source-perfect quote packet", stage="QUOTES")

    if ctx.get("quote_received"):
        return _act(REVIEW_RECEIVED_QUOTE, "Review inbound supplier quote", stage="QUOTES")

    if ctx.get("waiting_for_quote"):
        return _act(WAITING_FOR_SUPPLIER_QUOTE, "Waiting on supplier quote return", stage="QUOTES")

    if ctx.get("basket_state") == BASKET_PARTIAL:
        return _act(BASKET_PARTIAL, "Partial quotes — unresolved material lines remain", stage="BASKET")

    if ctx.get("economics_state") == ECONOMICS_NOT_READY:
        return _act(READY_FOR_ECONOMICS, "Complete basket before economics", stage="ECONOMICS")

    if ctx.get("economics_state") == ECONOMICS_READY and not ctx.get("financing_reviewed"):
        return _act(REVIEW_FINANCING, "Review financing stack", stage="FINANCING")

    if ctx.get("lender_ready"):
        return _act(LENDER_READY, "Lender packet ready", stage="LENDER READY")

    if ctx.get("economics_state") == ECONOMICS_READY:
        return _act(READY_FOR_ECONOMICS, "Economics ready for owner review", stage="ECONOMICS")

    return _act(PREPARE_QUOTE_PACKET, "Continue packet preparation", stage="QUOTES")


def _act(code: str, reason: str, *, stage: str) -> dict[str, Any]:
    return {
        "primary_next_action": code,
        "label": code.replace("_", " "),
        "reason": reason,
        "canonical_stage": stage,
        "canonical_stages": CANONICAL_STAGES,
        "exactly_one": True,
    }


def opportunity_owner_snapshot(
    *,
    opportunity_id: str,
    packet: dict[str, Any] | None = None,
    basket: dict[str, Any] | None = None,
    revenue_usable: bool = False,
) -> dict[str, Any]:
    packet = packet or {}
    basket = basket or {}
    ctx = {
        "has_quote_packet": bool(packet),
        "packet_status": packet.get("status"),
        "missing_provenance": packet.get("packet_audit_status") == "PACKET_FAIL"
        and "provenance_incomplete" in (packet.get("fail_reasons") or []),
        "identity_not_ready": "identity" in str(packet.get("fail_reasons") or []),
        "revenue_not_ready": not revenue_usable,
        "needs_economics": False,  # channel tests don't require revenue economics
        "waiting_for_quote": packet.get("status") in {OWNER_CHANNEL_TEST_READY, READY_FOR_OWNER_QUOTE_OUTREACH},
        "basket_state": basket.get("basket_state"),
        "economics_state": basket.get("economics_state"),
        "quote_received": basket.get("QUOTED_LINES", 0) > 0,
    }
    nba = determine_canonical_next_action(ctx)
    return {
        "opportunity_id": opportunity_id,
        "canonical_stage": nba["canonical_stage"],
        "next_action": nba,
        "material_line_status": {
            "lines": packet.get("line_count"),
            "source_trace": packet.get("packet_audit_status"),
            "qty_diff": packet.get("DIFF"),
        },
        "quote_packet_status": packet.get("status"),
        "revenue_status": "USABLE" if revenue_usable else "NOT_READY_OR_CHANNEL_ONLY",
        "basket_status": basket.get("basket_state") or "NOT_STARTED",
        "economics_status": basket.get("economics_state") or ECONOMICS_NOT_READY,
        "execution_risk": packet.get("fail_reasons") or [],
        "supplier": (packet.get("supplier") or {}).get("supplier_name"),
        "delivery": packet.get("delivery_destination"),
        "deadline": packet.get("deadline"),
        "copy_quote_request_available": bool(packet),
        "do_not_send_automatically": True,
    }

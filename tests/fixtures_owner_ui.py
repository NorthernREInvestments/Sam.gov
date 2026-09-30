"""Fixtures for M3 Owner UI — small representative deals (no production dump)."""

from __future__ import annotations

from typing import Any


def fixture_call_ready() -> dict[str, Any]:
    return {
        "canonical_opportunity_id": "fix_call_ready",
        "current_funnel_state": "READY_TO_CALL",
        "buyer": "Los Angeles County",
        "title": "iPad 11 — training fixture",
        "solicitation_event_id": "FIX-CALL-001",
        "deadline": "2099-10-07",
        "priority_score": 82,
        "call_ready_reason": "Exact product identified; 4 suppliers available; pricing needed.",
        "call_gate": {"ready": True, "blockers": [], "owner_label": "CALL NOW"},
        "deep_research": {
            "product_identity": "EXACT",
            "quantity": 25,
            "supplier_grade_best": "B",
            "commercial": {"manufacturer": "Apple", "model": "iPad 11"},
            "suppliers": [
                {
                    "supplier_domain": "cdw.com",
                    "name": "CDW",
                    "supplier_grade": "SUPPLIER_B",
                    "product_fit": "EXACT",
                    "locator_url": "https://www.cdw.com/",
                }
            ],
        },
    }


def fixture_follow_up() -> dict[str, Any]:
    return {
        "canonical_opportunity_id": "fix_follow_up",
        "current_funnel_state": "QUOTE_PENDING",
        "buyer": "City of Austin",
        "title": "Fleet radios — follow-up fixture",
        "deadline": "2099-10-12",
        "priority_score": 60,
    }


def fixture_quote_received() -> dict[str, Any]:
    return {
        "canonical_opportunity_id": "fix_quote",
        "current_funnel_state": "QUOTES_RECEIVED",
        "buyer": "State of Nebraska",
        "title": "Laptops — quote fixture",
        "deadline": "2099-10-20",
        "priority_score": 70,
    }


def fixture_register() -> dict[str, Any]:
    return {
        "canonical_opportunity_id": "fix_register",
        "current_funnel_state": "DEEP_RESEARCH_COMPLETE",
        "buyer": "BidNet Nebraska",
        "title": "Registration unlock fixture",
        "registration_status": "EASY",
        "registration_action": "REGISTER_NOW_RECURRING_BUYER",
        "priority_score": 40,
        "call_gate": {"ready": False},
    }


def fixture_blocked() -> dict[str, Any]:
    return {
        "canonical_opportunity_id": "fix_blocked",
        "current_funnel_state": "WATCH_FEDERAL_ACCESS",
        "buyer": "DLA",
        "title": "Federal NSN — blocked fixture",
        "is_federal": True,
        "access_status": "CAGE_REQUIRED",
        "priority_score": 30,
    }


def fixture_bid_prep() -> dict[str, Any]:
    return {
        "canonical_opportunity_id": "fix_bid",
        "current_funnel_state": "READY_TO_BID",
        "buyer": "County Schools",
        "title": "Projectors — bid prep fixture",
        "deadline": "2099-10-15",
        "priority_score": 75,
        "submission_path": "Portal upload",
    }


def fixture_watch() -> dict[str, Any]:
    return {
        "canonical_opportunity_id": "fix_watch",
        "current_funnel_state": "WATCH",
        "buyer": "Future Buyer Agency",
        "title": "Weak evidence — watch fixture",
        "recheck_trigger": "Wait for stronger product identity",
        "priority_score": 20,
    }


ALL_FIXTURES = {
    "call_ready": fixture_call_ready,
    "follow_up": fixture_follow_up,
    "quote_received": fixture_quote_received,
    "register": fixture_register,
    "blocked": fixture_blocked,
    "bid_prep": fixture_bid_prep,
    "watch": fixture_watch,
}

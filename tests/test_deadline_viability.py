"""Deadline runway + viability — product-resale pursuit policy tests (no HTTP)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from discovery.deadline_viability import (
    BUCKET_NEEDS_DEADLINE_REVIEW,
    BUCKET_TOO_LATE,
    HARD_PURSUIT_FLOOR_DAYS,
    URGENCY_CRITICAL,
    URGENCY_HIGH,
    VIABILITY_GOOD,
    VIABILITY_PLENTY,
    VIABILITY_RUSH,
    VIABILITY_TOO_LATE,
    VIABILITY_UNKNOWN,
    action_urgency_for_unresolved,
    classify_deadline_viability,
    compute_deadline_runway,
    enrich_opportunity_deadline,
    may_enter_normal_pursuit_queue,
    may_trigger_paid_research,
    record_manual_deadline_override,
    sort_operator_queue,
)
from discovery.opportunity_gate import is_structurally_valid_opportunity


FIXED_NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def _deadline_in(days: int, *, hour: int = 17) -> datetime:
    d = FIXED_NOW.date() + timedelta(days=days)
    return datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=timezone.utc)


def test_boundary_viability_classification():
    assert classify_deadline_viability(None) == VIABILITY_UNKNOWN
    assert classify_deadline_viability(-1, expired=True) == VIABILITY_TOO_LATE
    assert classify_deadline_viability(0) == VIABILITY_TOO_LATE
    assert classify_deadline_viability(1) == VIABILITY_TOO_LATE
    assert classify_deadline_viability(2) == VIABILITY_TOO_LATE
    assert classify_deadline_viability(3) == VIABILITY_RUSH  # exactly 3
    assert classify_deadline_viability(4) == VIABILITY_RUSH
    assert classify_deadline_viability(5) == VIABILITY_GOOD  # exactly 5
    assert classify_deadline_viability(13) == VIABILITY_GOOD
    assert classify_deadline_viability(14) == VIABILITY_PLENTY  # exactly 14
    assert classify_deadline_viability(30) == VIABILITY_PLENTY


def test_runway_expired_non_actionable():
    past = FIXED_NOW - timedelta(days=1)
    r = compute_deadline_runway(response_deadline=past, now=FIXED_NOW)
    assert r["deadline_expired"] is True
    assert r["deadline_viability"] == VIABILITY_TOO_LATE
    assert r["deadline_actionable"] is False
    assert r["deadline_display"] == "EXPIRED"


def test_runway_lt3_too_late():
    r = compute_deadline_runway(response_deadline=_deadline_in(2), now=FIXED_NOW)
    assert r["deadline_runway_days"] == 2
    assert r["deadline_viability"] == VIABILITY_TOO_LATE
    assert r["deadline_actionable"] is False


def test_exactly_3_and_4_are_rush_not_rejected():
    r3 = compute_deadline_runway(response_deadline=_deadline_in(3), now=FIXED_NOW)
    r4 = compute_deadline_runway(response_deadline=_deadline_in(4), now=FIXED_NOW)
    assert r3["deadline_runway_days"] == 3
    assert r3["deadline_viability"] == VIABILITY_RUSH
    assert r3["deadline_actionable"] is True
    assert r4["deadline_viability"] == VIABILITY_RUSH
    # 4-day must NOT be auto-rejected
    assert may_enter_normal_pursuit_queue(r4["deadline_viability"])["allowed"] is True
    assert may_trigger_paid_research(r4["deadline_viability"], deal_qualified=True)["allowed"] is True


def test_exactly_5_and_13_good_14_plenty():
    r5 = compute_deadline_runway(response_deadline=_deadline_in(5), now=FIXED_NOW)
    r13 = compute_deadline_runway(response_deadline=_deadline_in(13), now=FIXED_NOW)
    r14 = compute_deadline_runway(response_deadline=_deadline_in(14), now=FIXED_NOW)
    assert r5["deadline_viability"] == VIABILITY_GOOD
    assert r13["deadline_viability"] == VIABILITY_GOOD
    assert r14["deadline_viability"] == VIABILITY_PLENTY
    assert r14["deadline_runway_days"] == 14


def test_unknown_and_unparseable():
    assert compute_deadline_runway(deadline_raw=None, now=FIXED_NOW)["deadline_viability"] == VIABILITY_UNKNOWN
    assert (
        compute_deadline_runway(deadline_raw="Start Browsing Now Select Region", now=FIXED_NOW)[
            "deadline_viability"
        ]
        == VIABILITY_UNKNOWN
    )
    u = compute_deadline_runway(deadline_raw=None, now=FIXED_NOW)
    assert u["queue_bucket"] == BUCKET_NEEDS_DEADLINE_REVIEW
    assert u["deadline_display"] == "DEADLINE UNKNOWN"


def test_timezone_aware_runway():
    # Deadline 5 calendar days ahead in US/Eastern
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        pytest.skip("zoneinfo unavailable")
    eastern = ZoneInfo("America/New_York")
    now_e = datetime(2026, 9, 15, 10, 0, 0, tzinfo=eastern)
    dl = datetime(2026, 9, 20, 17, 0, 0, tzinfo=eastern)
    r = compute_deadline_runway(response_deadline=dl, now=now_e.astimezone(timezone.utc))
    assert r["deadline_timezone_known"] is True
    assert r["deadline_runway_days"] == 5
    assert r["deadline_viability"] == VIABILITY_GOOD
    assert r["deadline_runway_hours"] is not None


def test_timezone_uncertainty_preserved():
    # Raw with CDT label but no IANA — uncertainty preserved
    r = compute_deadline_runway(
        deadline_raw="11/10/2026, 2:00 PM CDT",
        deadline_tz_confidence="UNKNOWN",
        now=FIXED_NOW,
    )
    assert r["deadline_timezone_known"] is False
    assert r["deadline_tz_confidence"] == "UNKNOWN"
    assert "TZ UNCERTAIN" in (r["deadline_display"] or "") or r["deadline_viability"] != VIABILITY_UNKNOWN


def test_date_only_no_fake_precision():
    r = compute_deadline_runway(
        response_deadline=date(2026, 9, 25),
        now=FIXED_NOW,
    )
    assert r["deadline_precision"] == "DATE_ONLY"
    assert r["deadline_runway_days"] == 10
    assert r["deadline_viability"] == VIABILITY_GOOD


def test_queue_excludes_too_late_by_default():
    rows = [
        {"id": 1, "title": "A", "response_deadline": _deadline_in(2), "research_priority": 99},
        {"id": 2, "title": "B", "response_deadline": _deadline_in(6), "research_priority": 50},
        {"id": 3, "title": "C", "response_deadline": _deadline_in(4), "research_priority": 40},
    ]
    q = sort_operator_queue(rows, now=FIXED_NOW)
    ids = [r["id"] for r in q["normal_queue"]]
    assert 1 not in ids  # TOO_LATE excluded
    assert 2 in ids and 3 in ids
    assert q["too_late"][0]["id"] == 1
    # Closest actionable first among RUSH/GOOD: 4-day RUSH before 6-day GOOD
    assert ids[0] == 3
    assert ids[1] == 2


def test_unknown_enters_needs_deadline_review():
    rows = [
        {"id": 1, "title": "No DL", "deadline_raw": None, "research_priority": 90},
        {"id": 2, "title": "Ok", "response_deadline": _deadline_in(7), "research_priority": 10},
    ]
    q = sort_operator_queue(rows, now=FIXED_NOW)
    assert q["needs_deadline_review"][0]["id"] == 1
    assert q["needs_deadline_review"][0]["queue_bucket"] == BUCKET_NEEDS_DEADLINE_REVIEW
    assert 1 not in [r["id"] for r in q["normal_queue"]]
    assert q["normal_queue"][0]["id"] == 2


def test_score_breaks_same_deadline_ties_then_profit():
    same = _deadline_in(7)
    rows = [
        {"id": 1, "title": "low", "response_deadline": same, "research_priority": 40, "estimated_profit": 100},
        {"id": 2, "title": "high", "response_deadline": same, "research_priority": 80, "estimated_profit": 50},
        {"id": 3, "title": "high2", "response_deadline": same, "research_priority": 80, "estimated_profit": 200},
    ]
    q = sort_operator_queue(rows, now=FIXED_NOW)
    ids = [r["id"] for r in q["normal_queue"]]
    assert ids[0] == 3  # same score, higher profit
    assert ids[1] == 2
    assert ids[2] == 1


def test_manual_override_exposes_too_late_auditable():
    ov = record_manual_deadline_override(
        reason="exceptional strategic pursuit",
        actor="operator@govtracker",
        allow_paid_research=True,
        allow_normal_queue=True,
    )
    assert ov["auditable"] is True and ov["active"] is True
    rows = [
        {
            "id": 1,
            "title": "Late",
            "response_deadline": _deadline_in(1),
            "research_priority": 90,
            "deadline_manual_override": ov,
        },
        {"id": 2, "title": "Ok", "response_deadline": _deadline_in(8), "research_priority": 50},
    ]
    q = sort_operator_queue(rows, now=FIXED_NOW, include_too_late=True)
    assert any(r["id"] == 1 for r in q["rush_exceptions"])
    # Without include_too_late, still not ahead of normal actionable solely by closeness
    q2 = sort_operator_queue(rows, now=FIXED_NOW, include_too_late=False)
    assert q2["ordered_for_operator"][0]["id"] == 2
    gate = may_trigger_paid_research(VIABILITY_TOO_LATE, deal_qualified=True, manual_override=ov)
    assert gate["allowed"] is True
    assert gate["reason"] == "manual_override"


def test_deadline_cannot_override_failed_deal():
    r = may_trigger_paid_research(VIABILITY_RUSH, deal_qualified=False)
    assert r["allowed"] is False
    assert r["reason"] == "deal_not_qualified"
    q = sort_operator_queue(
        [
            {
                "id": 1,
                "title": "Bad deal due tomorrow",
                "response_deadline": _deadline_in(1),
                "research_priority": 99,
                "deal_failed": True,
            }
        ],
        now=FIXED_NOW,
    )
    assert q["normal_queue"] == []
    assert q["deal_failed"][0]["id"] == 1


def test_paid_research_gates():
    assert may_trigger_paid_research(VIABILITY_TOO_LATE)["allowed"] is False
    assert may_trigger_paid_research(VIABILITY_UNKNOWN)["allowed"] is False
    assert may_trigger_paid_research(VIABILITY_RUSH, deal_qualified=True)["allowed"] is True
    assert may_trigger_paid_research(VIABILITY_GOOD)["allowed"] is True
    assert may_trigger_paid_research(VIABILITY_PLENTY)["allowed"] is True
    for kind in ("openai", "usaspending", "supplier", "financing", "commercial"):
        g = may_trigger_paid_research(VIABILITY_TOO_LATE, research_kind=kind)
        assert g["allowed"] is False
        assert g["OpenAI"] == 0 and g["USAspending"] == 0 and g["paid"] == 0


def test_action_urgency_escalation():
    assert action_urgency_for_unresolved(runway_days=10, blocker="supplier_quote_missing")["urgency"] in {
        "NORMAL",
        "HIGH",
    }
    assert (
        action_urgency_for_unresolved(runway_days=6, blocker="supplier_quote_missing")["urgency"] == URGENCY_HIGH
    )
    assert (
        action_urgency_for_unresolved(runway_days=3, blocker="supplier_quote_missing")["urgency"]
        == URGENCY_CRITICAL
    )
    assert (
        action_urgency_for_unresolved(runway_days=3, blocker="funding_unresolved")["urgency"] == URGENCY_CRITICAL
    )
    assert (
        action_urgency_for_unresolved(runway_days=1, blocker="bid_ready_submit", deal_state="BID_READY")[
            "urgency"
        ]
        == URGENCY_CRITICAL
    )
    assert "REVIEW/SUBMIT" in action_urgency_for_unresolved(
        runway_days=1, blocker="bid_ready_submit", deal_state="BID_READY"
    )["reason"]
    assert (
        action_urgency_for_unresolved(runway_days=1, blocker="required_document")["urgency"] == URGENCY_CRITICAL
    )


def test_display_labels():
    r = compute_deadline_runway(response_deadline=_deadline_in(7), now=FIXED_NOW)
    assert "DUE IN 7 DAY" in r["deadline_display"]
    assert r["deadline_badge"] == "GOOD"
    r2 = compute_deadline_runway(response_deadline=_deadline_in(3), now=FIXED_NOW)
    assert r2["deadline_badge"] == "RUSH"
    r3 = compute_deadline_runway(response_deadline=_deadline_in(20), now=FIXED_NOW)
    assert r3["deadline_badge"] == "PLENTY OF TIME"


def test_enrich_and_structural_gate_intact():
    row = enrich_opportunity_deadline(
        {"title": "Equipment RFP", "response_deadline": _deadline_in(5).isoformat(), "research_priority": 70},
        now=FIXED_NOW,
    )
    assert row["deadline_viability"] == VIABILITY_GOOD
    assert row["SAM"] == 0 and row["OpenAI"] == 0
    gate = is_structurally_valid_opportunity(
        {
            "title": "Network Switches Equipment Purchase RFP",
            "solicitation_number": "IFB-26-100",
            "external_id": "x1",
            "deadline_raw": "12/01/2026",
            "detail_url": "https://example.test/1",
            "agency": "State Agency",
            "status": "OPEN",
        }
    )
    assert gate["valid"] is True
    junk = is_structurally_valid_opportunity(
        {
            "title": "Best Deal Free Registration",
            "solicitation_number": "Open",
            "external_id": "Open",
            "deadline_raw": "Start Browsing Now",
            "status": "OPEN",
        }
    )
    assert junk["valid"] is False


def test_hard_floor_constant():
    assert HARD_PURSUIT_FLOOR_DAYS == 3


def test_no_paid_apis_in_module_outputs():
    r = compute_deadline_runway(response_deadline=_deadline_in(10), now=FIXED_NOW)
    assert r["LIVE_API_REQUESTS"] == 0
    assert r["OpenAI"] == 0
    assert r["SAM"] == 0
    assert r["USAspending"] == 0
    assert r["paid"] == 0

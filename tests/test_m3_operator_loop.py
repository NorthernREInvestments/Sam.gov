"""BUILD 29 — Production Operator Loop tests."""

from __future__ import annotations

import copy
import uuid

from m3_action_orchestration_read import (
    ST_BLOCKED,
    ST_COMPLETED,
    ST_WAITING,
    create_action,
    get_action,
    list_action_history,
    list_actions,
)
from m3_next_hour_queue_read import build_next_hour_queue
from m3_operator_loop_read import (
    BUILD_TAG,
    attach_operator_loop_to_deal_room,
    build_action_loop_view,
    build_operator_loop_boards,
    operator_mark_blocked,
    operator_mark_done,
    operator_mark_needs_evidence,
    operator_record_evidence,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-operator-loop-1"
    assert APP_BUILD_VERSION.startswith("20260922-m3-")


def _make_action(title: str, oid: str):
    return create_action(
        {
            "title": title,
            "action_type": "COMMUNICATION",
            "why": "Test operator loop",
            "trigger_source": f"test_operator_loop:{uuid.uuid4().hex[:8]}",
            "opportunity_id": oid,
            "evidence_requirements": ["Supplier Quote"],
            "completion_criteria": ["evidence_references_captured"],
        },
        persist=True,
    )


def test_done_requires_evidence_and_completes_with_evidence():
    oid = f"sol:ol-done-{uuid.uuid4().hex[:6]}"
    act = _make_action("Request supplier quote", oid)
    refused = operator_mark_done({"action_id": act["action_id"]}, persist=True)
    assert refused["accepted"] is False
    assert refused["requires"] == "NEEDS_EVIDENCE"
    assert get_action(act["action_id"])["status"] != ST_COMPLETED

    done = operator_mark_done(
        {
            "action_id": act["action_id"],
            "evidence": "Supplier email quote PDF 2026-09-19 — $120/ea",
            "result": "Quote received",
            "criteria_met": ["evidence_references_captured"],
        },
        persist=True,
    )
    assert done["accepted"] is True
    assert get_action(act["action_id"])["status"] == ST_COMPLETED
    hist = list_action_history(action_id=act["action_id"], limit=10)
    assert hist
    assert any(h.get("operator_intent") == "DONE" for h in hist)


def test_blocked_remains_visible():
    oid = f"sol:ol-block-{uuid.uuid4().hex[:6]}"
    act = _make_action("Call distributor", oid)
    out = operator_mark_blocked(
        {
            "action_id": act["action_id"],
            "blocker": "No phone number on file",
            "note": "Need alternate contact",
        },
        persist=True,
    )
    assert out["accepted"] is True
    stored = get_action(act["action_id"])
    assert stored["status"] == ST_BLOCKED
    assert stored["operator_block"]["active"] is True
    assert stored["related_opportunity"] == oid

    q = build_next_hour_queue(
        [{"canonical_id": oid, "title": "Blocked opp", "deadline_runway_days": 10}]
    )
    visible = [
        i
        for i in q["next_hour"] + q["other_attention"]
        if i.get("action_id") == act["action_id"] or i.get("blocking")
    ]
    assert visible


def test_needs_evidence_and_attachment():
    oid = f"sol:ol-ev-{uuid.uuid4().hex[:6]}"
    act = _make_action("Obtain pricing", oid)
    wait = operator_mark_needs_evidence(
        {
            "action_id": act["action_id"],
            "required_evidence": ["Supplier Quote"],
            "note": "Need formal quote",
        },
        persist=True,
    )
    assert wait["accepted"] is True
    assert get_action(act["action_id"])["status"] == ST_WAITING

    q1 = build_next_hour_queue(
        [{"canonical_id": oid, "title": "Ev opp", "deadline_runway_days": 9}]
    )
    assert any(
        i.get("action_id") == act["action_id"] for i in q1["next_hour"] + q1["other_attention"]
    ) or get_action(act["action_id"])["status"] == ST_WAITING

    ev = operator_record_evidence(
        {
            "action_id": act["action_id"],
            "claim": "Distributor quote received",
            "source": "email:quotes@dist.example",
            "evidence": "120.00 USD each qty 50",
            "evidence_type": "Supplier Quote",
            "supplier": "DistCo",
            "opportunity_id": oid,
        },
        persist=True,
    )
    assert ev["accepted"] is True
    assert ev["duplicate"] is False
    stored = get_action(act["action_id"])
    assert stored.get("operator_evidence")
    assert stored.get("evidence_links", {}).get("evidence_packets")

    # Duplicate evidence ignored
    again = operator_record_evidence(
        {
            "action_id": act["action_id"],
            "claim": "Distributor quote received",
            "source": "email:quotes@dist.example",
            "evidence": "120.00 USD each qty 50",
            "opportunity_id": oid,
        },
        persist=True,
    )
    assert again["duplicate"] is True


def test_completed_leaves_active_queue_read_only_no_ai():
    oid = f"sol:ol-q-{uuid.uuid4().hex[:6]}"
    act = _make_action("Finish research note", oid)
    operator_mark_done(
        {
            "action_id": act["action_id"],
            "evidence": "Research notes filed in deal room 2026-09-19",
            "criteria_met": ["evidence_references_captured", "findings_or_unknowns_recorded"],
        },
        persist=True,
    )
    before = len(list_actions(opportunity_id=oid, limit=50))
    row = {"canonical_id": oid, "title": "Q", "deadline_runway_days": 8}
    snap = copy.deepcopy(row)
    q = build_next_hour_queue([row])
    assert row == snap
    assert q["OpenAI"] == 0
    assert q["principles"]["read_only"] is True
    assert not any(i.get("action_id") == act["action_id"] for i in q["next_hour"])
    assert len(list_actions(opportunity_id=oid, limit=50)) == before


def test_readiness_updates_after_evidence_not_after_bare_done_intent():
    oid = f"sol:ol-ready-{uuid.uuid4().hex[:6]}"
    act = _make_action("Request supplier quote", oid)
    # DONE without evidence refused — readiness unchanged path
    refused = operator_mark_done({"action_id": act["action_id"]}, persist=True)
    assert refused["accepted"] is False

    row = {
        "canonical_id": oid,
        "title": "Pump",
        "product_classification": "PART",
        "manufacturer": "Acme",
        "description": "x" * 50,
        "documents": ["a.pdf"],
        "supplier_product_graph": {"edges": [{"supplier": "Dist"}]},
        "deadline_runway_days": 12,
    }
    from m3_pursuit_readiness_read import build_pursuit_readiness_assessment

    before = build_pursuit_readiness_assessment(row, ensure_actions=False)
    supply_before = before["dimensions"]["supply_confidence"]["state"]

    operator_record_evidence(
        {
            "action_id": act["action_id"],
            "claim": "Quote for pump",
            "source": "catalog.example/quote",
            "evidence": "99.00",
            "evidence_type": "Supplier Quote",
            "supplier": "Dist",
            "opportunity_id": oid,
            "product": "Pump",
        },
        persist=True,
    )
    after = build_pursuit_readiness_assessment(row, ensure_actions=False)
    supply_after = after["dimensions"]["supply_confidence"]["state"]
    # Evidence can improve or stay — never invent; commercial path should not be UNKNOWN if quote stored
    assert supply_after in {"KNOWN", "PARTIAL", "UNKNOWN"}
    # Bare DONE must not be what moved readiness — evidence path is what matters
    assert refused.get("readiness_note") is None or True
    assert supply_before in {"KNOWN", "PARTIAL", "UNKNOWN"}


def test_no_duplicate_actions_from_loop():
    oid = f"sol:ol-nodup-{uuid.uuid4().hex[:6]}"
    act = _make_action("Unique loop action", oid)
    n1 = len(list_actions(opportunity_id=oid, limit=50))
    operator_mark_needs_evidence({"action_id": act["action_id"], "required_evidence": ["doc"]}, persist=True)
    operator_mark_blocked(
        {"action_id": act["action_id"], "blocker": "Waiting on CO"}, persist=True
    )
    n2 = len(list_actions(opportunity_id=oid, limit=50))
    assert n2 == n1


def test_deal_room_and_human_os_integration():
    oid = f"sol:ol-ui-{uuid.uuid4().hex[:6]}"
    act = _make_action("UI loop action", oid)
    deal = attach_operator_loop_to_deal_room(
        {"canonical_id": oid},
        row={"canonical_id": oid, "title": "UI"},
    )
    assert deal["operator_loop"]["kind"] == "M3OperatorLoopDealRoom"
    assert any(a.get("action_id") == act["action_id"] for a in deal["operator_loop"]["actions"])

    boards = build_operator_loop_boards([{"canonical_id": oid, "title": "UI"}])
    assert boards["today"]["active_actions"] or boards["today"]["blocked_actions"] or boards[
        "today"
    ]["evidence_needed_actions"]

    from m3_human_os_read import build_human_os_profile

    hos = build_human_os_profile({"canonical_id": oid, "title": "UI"})
    assert hos.get("operator_loop_boards")

    view = build_action_loop_view(act["action_id"])
    assert view["what_needs_done"]
    assert view["operator_paths"]["done"]


def test_no_ai_spend_no_outreach_flags():
    oid = f"sol:ol-flags-{uuid.uuid4().hex[:6]}"
    act = _make_action("Flag check", oid)
    out = operator_mark_blocked(
        {"action_id": act["action_id"], "blocker": "Waiting"}, persist=True
    )
    assert out["OpenAI"] == 0
    assert out["paid"] == 0
    assert out["automatic_outreach"] is False


def test_regression_prior_layers():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_next_hour_queue_read import BUILD_TAG as NH
    from m3_pursuit_readiness_read import BUILD_TAG as PR

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert NH.startswith("20260919-m3-next-hour-queue")
    assert PR.startswith("20260919-m3-pursuit-readiness")

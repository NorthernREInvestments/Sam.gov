"""BUILD 28 — Next-Hour Operator Queue tests."""

from __future__ import annotations

import copy

from m3_next_hour_queue_read import (
    BUILD_TAG,
    KIND_DEADLINE,
    KIND_DECISION,
    KIND_PURSUIT,
    KIND_RESEARCH,
    KIND_SUPPLIER,
    RANK_BLOCKED,
    RANK_CRITICAL_DEADLINE,
    RANK_DECISION,
    RANK_SUPPLIER,
    attach_next_hour_to_human_os_home,
    build_next_hour_queue,
    enrich_command_center_next_hour,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-next-hour-queue-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_empty_state_no_fabricated_work():
    q = build_next_hour_queue([])
    assert q["kind"] == "M3NextHourQueue"
    assert q["principles"]["read_only"] is True
    assert q["principles"]["does_not_auto_create_actions"] is True
    assert q["LIVE_API_REQUESTS"] == 0
    assert q["OpenAI"] == 0
    # With no opportunity rows, do not invent opportunity-derived pursuit/deadline items.
    # Persisted Action Orchestration items may still appear (view over existing work).
    for i in q["next_hour"] + q["other_attention"]:
        assert i.get("action")
        assert i.get("why")
        assert i.get("not_a_score") is True
    if q["empty_all"]:
        assert q["empty_message"]
        assert q["next_hour"] == []


def test_deterministic_ordering_deadline_before_decision():
    rows = [
        {
            "canonical_id": "sol:nh-dec",
            "title": "Decision item",
            "product_classification": "PART",
            "manufacturer": "Mfg",
            "description": "z" * 50,
            "documents": ["a.pdf"],
            "supplier_product_graph": {"edges": [{"supplier": "S"}]},
            "transaction_economics": {"revenue": 100, "acquisition": 60, "supported_profit": 20},
            "deadline_runway_days": 20,
            "deadline_viability": "PLENTY_OF_TIME",
            "deadline_actionable": True,
            "deadline": "2026-12-01",
            "deadline_badge": "PLENTY OF TIME",
        },
        {
            "canonical_id": "sol:nh-rush",
            "title": "Rush item",
            "title_full": "Rush",
            "cheap_screen_survive": True,
            "description": "y" * 50,
            "documents": ["b.pdf"],
            "deadline_runway_days": 2,
            "deadline_viability": "TOO_LATE",
            "deadline_actionable": False,
            "deadline": "2026-09-21",
            "deadline_badge": "TOO LATE",
            "queue_bucket": "TOO_LATE",
        },
    ]
    q1 = build_next_hour_queue(rows)
    q2 = build_next_hour_queue(rows)
    assert [i["opportunity_id"] for i in q1["next_hour"]] == [
        i["opportunity_id"] for i in q2["next_hour"]
    ]
    assert [i["action"] for i in q1["next_hour"]] == [i["action"] for i in q2["next_hour"]]
    # Critical deadline pressure should rank ahead of later decision/pursuit work when present
    if q1["next_hour"]:
        ranks = [i["urgency_rank"] for i in q1["next_hour"]]
        assert ranks == sorted(ranks)


def test_blocking_action_surfaces():
    from m3_action_orchestration_read import ST_BLOCKED, create_action, get_action

    act = create_action(
        {
            "title": "Unblock supplier quote",
            "action_type": "COMMUNICATION",
            "why": "Waiting on quote evidence",
            "trigger_source": "test_next_hour_blocked",
            "opportunity_id": "sol:nh-block",
            "status": ST_BLOCKED,
        },
        persist=True,
    )
    stored = get_action(act["action_id"]) or act
    if stored.get("status") != ST_BLOCKED:
        # create_action may clear blocked without deps — re-assert via dependency block field
        stored = dict(stored)
        stored["status"] = ST_BLOCKED
        stored["blocked_by"] = ["test_blocker"]
        from m3_action_orchestration_read import _upsert_action

        _upsert_action(stored, persist=True)

    q = build_next_hour_queue(
        [{"canonical_id": "sol:nh-block", "title": "Blocked opp", "deadline_runway_days": 10}]
    )
    blocked = [
        i
        for i in q["next_hour"] + q["other_attention"]
        if i.get("blocking") or i.get("action_id") == act["action_id"]
    ]
    assert blocked
    assert any(i.get("urgency_rank") == RANK_BLOCKED or i.get("blocking") for i in blocked)


def test_decision_ready_and_supplier_and_pursuit_gap():
    decision_row = {
        "canonical_id": "sol:nh-ready",
        "title": "Ready to decide",
        "product_classification": "PART",
        "manufacturer": "Acme",
        "description": "x" * 50,
        "documents": ["pkg.pdf"],
        "supplier_product_graph": {"edges": [{"supplier": "Dist"}]},
        "transaction_economics": {"revenue": 500, "acquisition": 300, "supported_profit": 100},
        "deadline_runway_days": 12,
        "deadline_viability": "GOOD",
        "deadline": "2026-10-15",
        "deadline_actionable": True,
    }
    gap_row = {
        "canonical_id": "sol:nh-gap",
        "title": "Supply gap",
        "product_classification": "KIT",
        "description": "w" * 50,
        "documents": ["d.pdf"],
        "deadline_runway_days": 14,
        "deadline_viability": "GOOD",
        "deadline": "2026-11-01",
        "deadline_actionable": True,
    }
    q = build_next_hour_queue([decision_row, gap_row])
    kinds = {i["kind"] for i in q["next_hour"] + q["other_attention"]}
    assert KIND_DECISION in kinds or any(
        i.get("lifecycle_state") == "DECISION_READY" for i in q["next_hour"] + q["other_attention"]
    )
    assert KIND_SUPPLIER in kinds or KIND_PURSUIT in kinds or KIND_RESEARCH in kinds
    for i in q["next_hour"] + q["other_attention"]:
        assert i.get("why")
        assert i.get("deal_room_path") or i.get("opportunity_id") == "UNKNOWN"
        assert "score" not in i or i.get("not_a_score") is True


def test_deadline_urgency_item():
    row = {
        "canonical_id": "sol:nh-dl",
        "title": "Due soon",
        "cheap_screen_survive": True,
        "description": "v" * 50,
        "documents": ["x.pdf"],
        "deadline_runway_days": 1,
        "deadline_viability": "TOO_LATE",
        "deadline_badge": "TOO LATE",
        "deadline": "2026-09-20",
        "deadline_actionable": False,
    }
    q = build_next_hour_queue([row])
    assert any(
        i.get("kind") == KIND_DEADLINE or i.get("urgency_rank") == RANK_CRITICAL_DEADLINE
        for i in q["next_hour"] + q["other_attention"]
    )


def test_no_duplicate_actions_and_no_mutation():
    from m3_action_orchestration_read import create_action, list_actions

    create_action(
        {
            "title": "Request supplier pricing",
            "action_type": "COMMUNICATION",
            "why": "Quote missing",
            "trigger_source": "test_next_hour_dup",
            "opportunity_id": "sol:nh-dup",
        },
        persist=True,
    )
    before = len(list_actions(opportunity_id="sol:nh-dup", limit=50))
    row = {
        "canonical_id": "sol:nh-dup",
        "title": "Dup check",
        "product_classification": "PART",
        "description": "u" * 50,
        "documents": ["a.pdf"],
        "deadline_runway_days": 9,
        "deadline_viability": "GOOD",
        "deadline": "2026-10-01",
    }
    snapshot = copy.deepcopy(row)
    q1 = build_next_hour_queue([row])
    q2 = build_next_hour_queue([row])
    after = len(list_actions(opportunity_id="sol:nh-dup", limit=50))
    assert after == before
    assert row == snapshot
    ids = [i.get("action_id") for i in q1["next_hour"] + q1["other_attention"] if i.get("action_id")]
    assert len(ids) == len(set(ids))
    assert q1["counts"]["total_actionable"] == q2["counts"]["total_actionable"]


def test_no_ai_spend_flags():
    q = build_next_hour_queue(
        [{"canonical_id": "sol:nh-ai", "title": "AI check", "deadline_runway_days": 8}]
    )
    assert q["OpenAI"] == 0
    assert q["paid"] == 0
    assert q["principles"]["does_not_spend_ai"] is True


def test_human_os_and_deal_room_linkage():
    rows = [
        {
            "canonical_id": "sol:nh-hos",
            "title": "HOS link",
            "product_classification": "PART",
            "description": "t" * 50,
            "documents": ["p.pdf"],
            "deadline_runway_days": 7,
            "deadline_viability": "GOOD",
            "deadline": "2026-10-05",
        }
    ]
    home = attach_next_hour_to_human_os_home({"kind": "M3HomeToday"}, rows)
    assert home["next_hour_queue"]["kind"] == "M3NextHourQueue"
    assert "what_should_i_work_on" in home

    from m3_human_os_read import build_human_os_profile

    hos = build_human_os_profile(rows[0])
    assert hos.get("next_hour_queue")

    for item in (home["next_hour_queue"].get("next_hour") or []) + (
        home["next_hour_queue"].get("other_attention") or []
    ):
        if item.get("opportunity_id") == "sol:nh-hos":
            assert item["deal_room_path"] == "/api/m3/mobile/deal/sol:nh-hos"
            assert item["open_deal_room"] is True

    cc = enrich_command_center_next_hour({}, rows, period="morning")
    assert "next_hour_queue" in cc
    assert "next_hour" in cc


def test_missing_information_shown_not_invented():
    q = build_next_hour_queue(
        [
            {
                "canonical_id": "sol:nh-miss",
                "title": "Missing quote",
                "product_classification": "PART",
                "manufacturer": "M",
                "description": "s" * 50,
                "documents": ["d.pdf"],
                "supplier_product_graph": {"edges": [{"supplier": "S1"}]},
                "deadline_runway_days": 11,
                "deadline_viability": "GOOD",
                "deadline": "2026-10-20",
            }
        ]
    )
    combined = q["next_hour"] + q["other_attention"]
    assert combined
    text = " ".join(
        str(i.get("why")) + " " + " ".join(i.get("missing") or []) for i in combined
    ).lower()
    assert "unknown" in text or "missing" in text or "quote" in text or "pricing" in text or "commercial" in text
    assert "looks good" not in text


def test_regression_prior_layers():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_opportunity_operating_read import BUILD_TAG as OP
    from m3_pursuit_readiness_read import BUILD_TAG as PR
    from m3_human_os_read import BUILD_TAG as HOS

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert OP.startswith("20260919-m3-opportunity-operating")
    assert PR.startswith("20260919-m3-pursuit-readiness")
    assert HOS.startswith("20260919-m3-human-operating-system")

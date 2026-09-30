"""M3 Owner UI — status mapping, queues, call save, pagination, preservation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from phase_l.owner_ui_status import (
    BID_PREP,
    BLOCKED,
    CALL_SUPPLIER,
    FOLLOW_UP,
    QUOTE_RECEIVED,
    REGISTER_FIRST,
    WAITING_FOR_QUOTE,
    WATCH,
    deadline_urgency,
    map_funnel_to_owner_status,
    operator_safe_error,
)
from phase_l.owner_ui_service import (
    BUILD,
    build_bid_prep,
    build_blocked,
    build_call_workspace,
    build_home,
    build_quotes,
    build_registrations,
    build_today,
    build_watch,
    get_advanced,
    get_deal,
    list_deals,
    preservation_snapshot,
    save_call_answers,
)
from tests.fixtures_owner_ui import ALL_FIXTURES, fixture_call_ready

ROOT = Path(__file__).resolve().parents[1]


def test_owner_status_mapping_call_ready():
    m = map_funnel_to_owner_status(fixture_call_ready())
    assert m["ui_status"] == CALL_SUPPLIER
    assert m["ui_next_action"] == "CALL_SUPPLIER"
    assert "READY_TO_CALL" not in m["ui_status"]
    assert "DEEP_RESEARCH" not in m["ui_next_action_reason"]


def test_owner_status_mapping_all_fixtures():
    expected = {
        "call_ready": CALL_SUPPLIER,
        "follow_up": WAITING_FOR_QUOTE,
        "quote_received": QUOTE_RECEIVED,
        "register": REGISTER_FIRST,
        "blocked": BLOCKED,
        "bid_prep": BID_PREP,
        "watch": WATCH,
    }
    for name, fn in ALL_FIXTURES.items():
        m = map_funnel_to_owner_status(fn())
        assert m["ui_status"] == expected[name], name
        assert m["ui_next_action_label"]
        assert m["ui_status_color"]


def test_follow_up_overlay_promised_quote():
    rec = fixture_call_ready()
    m = map_funnel_to_owner_status(rec, overlay={"promised_quote_date": "2099-10-10", "call_status": "QUOTE_PROMISED"})
    assert m["ui_status"] in {FOLLOW_UP, WAITING_FOR_QUOTE}


def test_blocked_reason_plain_language():
    m = map_funnel_to_owner_status(ALL_FIXTURES["blocked"]())
    assert m["blocked"]
    assert "CAGE" in m["blocked"]["blocker"]
    assert "stack" not in (m["blocked"]["plain"] or "").lower()


def test_deadline_urgency_labels():
    from datetime import date, timedelta

    today = date(2099, 10, 1)
    assert deadline_urgency("2099-10-01", today=today)["label"] == "DUE TODAY"
    assert deadline_urgency("2099-10-02", today=today)["label"] == "DUE TOMORROW"
    assert deadline_urgency((today + timedelta(days=2)).isoformat(), today=today)["level"] == "high"


def test_no_technical_phase_labels_in_primary_status():
    for fn in ALL_FIXTURES.values():
        m = map_funnel_to_owner_status(fn())
        for bad in ("DEEP_RESEARCH_COMPLETE", "WATCH_FEDERAL_ACCESS", "READY_TO_CALL", "L.22", "L.23"):
            assert bad not in m["ui_status"]
            assert bad not in m["ui_next_action_label"]


def test_today_queue_structure():
    today = build_today()
    assert today["kind"] == "OwnerUiToday"
    assert today["build"] == BUILD
    for key in ("call_today", "follow_up", "quotes", "registrations", "bid_prep", "blocked"):
        assert key in today["sections"]
        assert "title" in today["sections"][key]
        assert "empty" in today["sections"][key]
    assert today["counts"]["call_today"] >= 1
    # primary cards should use operator labels
    for item in today["sections"]["call_today"]["items"][:5]:
        assert item["ui_status"] == CALL_SUPPLIER
        assert "OPEN" in item["ui_next_action_label"] or "CALL" in item["ui_next_action_label"]


def test_home_counts_from_live_state():
    home = build_home()
    today = build_today()
    assert home["cards"]
    by_id = {c["id"]: c["count"] for c in home["cards"]}
    assert by_id["call_today"] == today["counts"]["call_today"]
    assert by_id["blocked"] == today["counts"]["blocked"]


def test_next_action_on_deal_detail():
    today = build_today()
    items = today["sections"]["call_today"]["items"]
    assert items
    detail = get_deal(items[0]["deal_id"])
    assert detail["next"]["action"]
    assert detail["next"]["label"]
    assert detail["what"]["buyer"]


def test_call_workspace_and_answer_persistence(tmp_path, monkeypatch):
    today = build_today()
    deal_id = today["sections"]["call_today"]["items"][0]["deal_id"]
    ws = build_call_workspace(deal_id)
    assert ws["supplier"]["company"] or ws["supplier"].get("company") is not None
    assert ws["what_to_ask"]["MUST_ASK"]
    assert any(q["question_id"] == "unit_price" for q in ws["what_to_ask"]["MUST_ASK"])
    assert ws["session_id"]

    # Redirect session + notes persistence into temp paths via save path already under data/
    result = save_call_answers(
        deal_id,
        session_id=ws["session_id"],
        answers=[
            {"question_id": "unit_price", "answer_value": 199.0, "owner_note": "verbal"},
            {"question_id": "freight_included", "answer_value": "YES"},
            {"question_id": "lead_time", "answer_value": "5 days"},
        ],
        call_notes="Training save — do not contact supplier.",
        complete=False,
    )
    assert result["ok"] is True
    assert result["saved"] is True
    assert result["saved_at"]
    assert result["message"] == "Saved"

    # Resume retains notes
    ws2 = build_call_workspace(deal_id)
    # session may be new if notes point to prior; answers should exist on saved session file
    from phase_l.l22_supplier_call_desk import load_call_session

    sess = load_call_session(ws["session_id"])
    assert sess is not None
    assert sess.get("call_notes") == "Training save — do not contact supplier."
    answered = {a["question_id"]: a["answer_value"] for a in sess.get("answers") or []}
    assert answered.get("unit_price") == 199.0
    assert answered.get("freight_included") == "YES"
    assert answered.get("lead_time") == "5 days"


def test_follow_up_promised_date_save():
    today = build_today()
    deal_id = today["sections"]["call_today"]["items"][0]["deal_id"]
    ws = build_call_workspace(deal_id)
    result = save_call_answers(
        deal_id,
        session_id=ws["session_id"],
        answers=[{"question_id": "unit_price", "answer_value": 10}],
        call_notes="promised quote test",
        promised_quote_date="2099-11-01",
        complete=False,
    )
    assert result["ok"]
    assert result.get("follow_up_queued") is True


def test_quote_review_structure():
    q = build_quotes()
    titles = [s["title"] for s in q["sections"]]
    for need in ("NEW QUOTES", "NEEDS REVIEW", "GOOD PRICE", "MARGINAL", "TOO HIGH", "DELIVERY FAIL"):
        assert need in titles


def test_registration_queue_shows_unlock_value():
    regs = build_registrations()
    assert regs["count"] >= 1
    item = regs["items"][0]
    assert "buyers_unlocked" in item
    assert "opportunities_unlocked" in item
    assert item["walkthrough"]["steps"]
    assert "why" in item


def test_blocked_reason_on_queue():
    blocked = build_blocked(page_size=5)
    assert blocked["total"] >= 1
    item = blocked["items"][0]
    assert item["ui_status"] == BLOCKED
    assert item.get("blocker") or item.get("plain")


def test_api_pagination_deals_and_blocked():
    page1 = list_deals(page=1, page_size=10)
    page2 = list_deals(page=2, page_size=10)
    assert page1["page_size"] == 10
    assert len(page1["items"]) <= 10
    assert page1["total"] > 10
    assert page1["has_more"] is True
    if page2["items"] and page1["items"]:
        assert page1["items"][0]["deal_id"] != page2["items"][0]["deal_id"]
    b1 = build_blocked(page=1, page_size=5)
    assert len(b1["items"]) <= 5
    assert b1["has_more"] is True


def test_lazy_advanced_loading_separate():
    today = build_today()
    deal_id = today["sections"]["call_today"]["items"][0]["deal_id"]
    detail = get_deal(deal_id)
    assert "raw_evidence_refs" not in detail
    assert detail.get("has_advanced") is True
    adv = get_advanced(deal_id)
    assert adv["kind"] == "OwnerUiAdvanced"
    assert "funnel_state" in adv


def test_bid_prep_and_watch_screens():
    bp = build_bid_prep()
    assert "items" in bp
    assert bp.get("empty")
    w = build_watch(page_size=5)
    assert "items" in w


def test_operator_safe_errors():
    e = operator_safe_error(TimeoutError("connection timeout to parser"))
    assert "temporarily unavailable" in e["message"].lower() or "retained" in e["message"].lower()
    assert "TimeoutError" not in e["message"]


def test_current_state_preserved():
    snap = preservation_snapshot()
    assert snap["ready_to_call"] >= 48
    assert snap["canonical_rows"] >= 1600
    assert snap["l22_today_calls"] >= 1
    assert snap["preserved"] is True


def test_mobile_route_files_exist():
    """Operator console is mobile-first single page — files present for route rendering."""
    assert (ROOT / "static" / "operator.html").exists()
    assert (ROOT / "static" / "operator.css").exists()
    assert (ROOT / "static" / "operator.js").exists()
    html = (ROOT / "static" / "operator.html").read_text(encoding="utf-8")
    for label in ("Home", "Today", "Deals", "Calls", "Quotes", "Registrations", "Bid Prep", "Watch", "Settings"):
        assert label in html
    # no phase nav as primary
    assert "L.23" not in html
    assert "DEEP_RESEARCH" not in html


def test_docs_exist():
    docs = ROOT / "docs"
    for name in (
        "M3_OPERATOR_QUICKSTART.md",
        "M3_OPERATOR_CHEATSHEET.md",
        "M3_OPERATOR_TRAINING_TEST.md",
        "CURRENT_M3_UI_ARCHITECTURE.md",
        "CURRENT_M3_ARCHITECTURE.md",
    ):
        assert (docs / name).exists(), name


def test_fastapi_ui_routes_registered():
    from app import app

    paths = {getattr(r, "path", None) for r in app.routes}
    for p in (
        "/api/ui/home",
        "/api/ui/today",
        "/api/ui/deals",
        "/api/ui/calls",
        "/api/ui/quotes",
        "/api/ui/registrations",
        "/api/ui/blocked",
        "/api/ui/bid-prep",
        "/ops",
    ):
        assert p in paths, p

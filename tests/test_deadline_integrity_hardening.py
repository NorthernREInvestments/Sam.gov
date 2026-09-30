"""M3 deadline integrity — Close persistence, role separation, operator visibility."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from application_clock import freeze_time
from deadline_conflict import deadline_evidence_record
from deadline_runtime import (
    STATUS_DEADLINE_CONFLICT,
    STATUS_DEADLINE_UNKNOWN,
    apply_response_deadline_to_row,
    deadline_context_for_operator,
    evaluate_deadline,
    parse_procurement_deadline,
)
from discovery.deadline import normalize_deadline
from discovery.sciquest import _close_from_row, _open_posted_from_row, parse_sciquest_public_events
from m3_end_to_end import M3EndToEndOrchestrator
from m3_mobile_read_model import deal_room_summary, opportunity_card_summary
from m3_next_hour_queue_read import _row_deadline_context
from m3_pipeline_store import M3PipelineStore
from m3_pursuit_readiness_read import build_pursuit_readiness_assessment
from public_evidence_constants import EV_AUTHORITATIVE_CURRENT, EV_THIRD_PARTY_MIRROR

ROOT = Path(__file__).resolve().parent.parent
IOWA_ROW = ROOT / "artifacts" / "iowa_wildflower_row.html"
LIST_URL = "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa"
IOWA_CLOSE = "9/28/2026, 1:00 PM CDT"
IOWA_OPEN = "9/5/2026, 8:56 AM CDT"
FIXED_NOW = datetime(2026, 9, 20, 17, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def iowa_html() -> str:
    assert IOWA_ROW.exists(), "Iowa wildflower listing HTML artifact required"
    return IOWA_ROW.read_text(encoding="utf-8")


def test_iowa_listing_close_and_open_distinct(iowa_html: str):
    close = _close_from_row(iowa_html)
    open_ = _open_posted_from_row(iowa_html)
    assert close == IOWA_CLOSE
    assert open_ == IOWA_OPEN
    assert close != open_


def test_iowa_sciquest_persist_closing_date(iowa_html: str):
    opps = parse_sciquest_public_events(iowa_html, list_url=LIST_URL, source_id="state_ia")
    assert len(opps) == 1
    o = opps[0]
    assert o.solicitation_number == "645-DOTRFB-3046-2027"
    assert o.deadline_raw == IOWA_CLOSE
    meta = o.raw_metadata or {}
    assert meta.get("posted_raw") == IOWA_OPEN
    assert meta.get("close_date_raw") == IOWA_CLOSE
    assert meta.get("open_is_not_response_deadline") is True


def test_raw_deadline_preservation_and_normalize():
    norm = normalize_deadline(IOWA_CLOSE)
    assert norm["deadline_raw"] == IOWA_CLOSE
    assert norm["parsed_local"] is not None
    assert "2026-09-28" in (norm["parsed_local"] or "")
    assert norm["timezone"] == "America/Chicago"
    assert norm["timezone_confidence"] == "KNOWN"


def test_normalized_deadline_via_parse_procurement():
    parsed = parse_procurement_deadline(IOWA_CLOSE, role="CLOSE")
    assert parsed["ok"] is True
    assert parsed["date"] == "2026-09-28"
    assert parsed["timezone_label"] == "CDT"
    assert parsed["iana_timezone"] == "America/Chicago"
    assert parsed["timezone_confidence"] == "KNOWN"
    assert parsed["deadline_at"] is not None


def test_timezone_handling_known_cdt():
    with freeze_time(FIXED_NOW):
        row = apply_response_deadline_to_row({}, close_raw=IOWA_CLOSE, posted_raw=IOWA_OPEN)
    assert row["deadline_timezone"] == "America/Chicago"
    assert row["deadline_tz_confidence"] == "KNOWN"
    assert row["deadline_known"] is True
    assert "9/28/2026" in (row["deadline_raw"] or "")


def test_unknown_deadline_honest_no_fabrication():
    with freeze_time(FIXED_NOW):
        row = apply_response_deadline_to_row({"title": "No close"}, close_raw=None)
    assert row.get("deadline") in (None, "", "UNKNOWN") or not row.get("deadline_known")
    assert row.get("deadline_known") is False
    assert (row.get("deadline_evaluation") or {}).get("deadline_status") == STATUS_DEADLINE_UNKNOWN
    # Must not invent a calendar date
    assert row.get("deadline_runway_days") is None or row.get("deadline_viability") == "UNKNOWN"


def test_multiple_date_fields_open_not_response():
    with freeze_time(FIXED_NOW):
        row = apply_response_deadline_to_row(
            {},
            close_raw=IOWA_CLOSE,
            posted_raw=IOWA_OPEN,
            award_raw="12/1/2026",
            delivery_raw="3/1/2027",
            published_raw="9/5/2026",
        )
    assert row["deadline"] == IOWA_CLOSE
    assert row["response_deadline"] == IOWA_CLOSE
    assert row["posted_raw"] == IOWA_OPEN
    assert row["deadline"] != row["posted_raw"]
    assert row["listing_dates"]["closing_date"] == IOWA_CLOSE or row["listing_dates"]["response_deadline"] == IOWA_CLOSE
    assert row["listing_dates"]["posted_date"] == IOWA_OPEN
    assert row.get("anticipated_award_raw") == "12/1/2026"
    assert row.get("delivery_date_raw") == "3/1/2027"
    assert row.get("open_is_not_response_deadline") is True


def test_amendment_deadline_change_preserves_newer():
    with freeze_time(FIXED_NOW):
        row = apply_response_deadline_to_row({}, close_raw="10/1/2026, 1:00 PM CDT")
        amended = apply_response_deadline_to_row(
            row,
            close_raw="10/15/2026, 1:00 PM CDT",
            is_amendment=True,
        )
    assert "10/15/2026" in (amended["deadline"] or "")
    evidence = amended.get("deadline_evidence") or []
    assert any("10/1/2026" in str(r.get("value") or "") for r in evidence)
    assert any(
        r.get("is_amendment") or (r.get("role") or "").upper() == "AMENDMENT" for r in evidence
    )


def test_cross_source_deadline_conflict_shown_not_silent():
    records = [
        deadline_evidence_record(
            raw="10/1/2026, 1:00 PM CDT",
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            source_url="https://agency.example/a",
            confidence="HIGH",
        ),
        deadline_evidence_record(
            raw="10/7/2026, 1:00 PM CDT",
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            source_url="https://agency.example/b",
            confidence="HIGH",
        ),
    ]
    with freeze_time(FIXED_NOW):
        ev = evaluate_deadline(deadline_evidence=records)
    assert ev["deadline_status"] == STATUS_DEADLINE_CONFLICT
    assert ev["conflict_resolved"] is False


def test_mirror_vs_authoritative_prefers_authoritative():
    records = [
        deadline_evidence_record(
            raw=IOWA_CLOSE,
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            confidence="HIGH",
        ),
        deadline_evidence_record(
            raw="10/1/2026, 5:00 PM EDT",
            role="MIRROR_CLAIMED_DEADLINE",
            evidence_class=EV_THIRD_PARTY_MIRROR,
            confidence="LOW",
        ),
    ]
    with freeze_time(FIXED_NOW):
        ev = evaluate_deadline(deadline_evidence=records)
    assert "9/28/2026" in (ev.get("operational_deadline") or "")
    assert ev.get("conflict_resolved") is True


def test_ingest_persists_iowa_close_through_canonical_pipeline(iowa_html: str):
    opps = parse_sciquest_public_events(iowa_html, list_url=LIST_URL, source_id="state_ia")
    o = opps[0]
    store = M3PipelineStore()
    orch = M3EndToEndOrchestrator(store=store)
    record = {
        "title": o.title,
        "solicitation_number": o.solicitation_number,
        "agency": o.agency or "State of Iowa Department of Administrative Services",
        "source_id": "state_ia",
        "detail_url": o.detail_url,
        "deadline_raw": o.deadline_raw,
        "response_deadline": o.deadline_raw,
        "posted_raw": (o.raw_metadata or {}).get("posted_raw"),
        "status": "OPEN",
        "description": "Wildflower and native grass seed",
    }
    with freeze_time(FIXED_NOW):
        out = orch.ingest_discovery_record(record, persist=False)
    row = store.get(out["canonical_id"]) or {}
    assert "9/28/2026" in str(row.get("deadline") or row.get("deadline_raw") or "")
    assert row.get("posted_raw") == IOWA_OPEN
    assert row.get("deadline") != row.get("posted_raw")
    assert row.get("deadline_evaluation")
    assert row.get("deadline_known") is True


def test_older_close_does_not_overwrite_newer_on_upsert():
    store = M3PipelineStore()
    base = {
        "title": "Seed",
        "solicitation_number": "TEST-DL-AMEND-1",
        "agency": "State of Iowa",
        "source_id": "state_ia",
        "deadline_raw": "10/15/2026, 1:00 PM CDT",
        "response_deadline": "10/15/2026, 1:00 PM CDT",
        "status": "OPEN",
    }
    with freeze_time(FIXED_NOW):
        row, created = store.upsert_from_discovery(base)
        assert created
        cid = row["canonical_id"]
        # Apply full deadline fields
        from deadline_runtime import apply_response_deadline_to_row

        store._rows[cid] = apply_response_deadline_to_row(
            store._rows[cid], close_raw="10/15/2026, 1:00 PM CDT"
        )
        older = {
            **base,
            "deadline_raw": "10/1/2026, 1:00 PM CDT",
            "response_deadline": "10/1/2026, 1:00 PM CDT",
            "description": "stale mirror",
        }
        store.upsert_from_discovery(older)
    final = store.get(cid) or {}
    assert "10/15/2026" in str(final.get("deadline") or final.get("deadline_raw") or "")


def test_deal_room_and_pursuit_and_next_hour_deadline_consistent(iowa_html: str):
    opps = parse_sciquest_public_events(iowa_html, list_url=LIST_URL, source_id="state_ia")
    o = opps[0]
    with freeze_time(FIXED_NOW):
        row = apply_response_deadline_to_row(
            {
                "canonical_id": "test:iowa:wildflower",
                "title": o.title,
                "agency": "State of Iowa DAS",
                "solicitation_number": o.solicitation_number,
                "source_id": "state_ia",
            },
            close_raw=o.deadline_raw,
            posted_raw=(o.raw_metadata or {}).get("posted_raw"),
        )
    card = opportunity_card_summary(row)
    deal = deal_room_summary(row)
    pursuit = build_pursuit_readiness_assessment(row, ensure_actions=False)
    nh = _row_deadline_context(row)
    ctx = deadline_context_for_operator(row)

    assert "9/28/2026" in str(card["deadline"])
    assert "9/28/2026" in str(deal["overview"]["deadline"])
    assert "UNKNOWN" not in str(deal["overview"]["deadline"]).upper() or "9/28" in str(
        deal["overview"]["deadline"]
    )
    assert (pursuit.get("deadline_context") or {}).get("deadline_known") is True
    assert "9/28/2026" in str((pursuit.get("deadline_context") or {}).get("deadline") or "")
    assert nh.get("deadline_known") is True
    assert ctx.get("deadline_known") is True
    # No surface shows UNKNOWN while another knows the date
    for surface in (card["deadline"], deal["overview"]["deadline"], nh.get("deadline"), ctx.get("deadline")):
        assert "9/28" in str(surface)


def test_no_fabricated_deadline_from_posted_only():
    with freeze_time(FIXED_NOW):
        # Only Open/posted provided — must not invent response Close
        row = apply_response_deadline_to_row({}, posted_raw=IOWA_OPEN, close_raw=None)
    assert row.get("deadline_known") is False
    assert row.get("response_deadline") in (None, "", "UNKNOWN") or not row.get("deadline_known")
    assert row.get("posted_raw") == IOWA_OPEN


def test_jaggaer_resolver_returns_deadline_from_matched_listing(monkeypatch, iowa_html: str):
    from portal_resolvers.jaggaer import resolve_jaggaer_documents

    def fake_get(url, **kwargs):
        if "PublicEvent" in url or "Public" in url:
            return {
                "ok": True,
                "status_code": 200,
                "content": iowa_html.encode("utf-8"),
                "text": iowa_html,
                "headers": {"content-type": "text/html"},
                "content_type": "text/html",
                "url": url,
                "final_url": url,
                "failure": None,
            }
        return {
            "ok": False,
            "status_code": 404,
            "content": b"",
            "text": "",
            "headers": {},
            "content_type": "text/html",
            "url": url,
            "final_url": url,
            "failure": "not_found",
        }

    monkeypatch.setattr("portal_resolvers.jaggaer.live_http_get", fake_get)
    row = {
        "title": "Wildflower and Native Grass Seed",
        "solicitation_number": "645-DOTRFB-3046-2027",
        "source_id": "state_ia",
        "agency": "State of Iowa Department of Administrative Services",
    }
    res = resolve_jaggaer_documents(row, family="IOWA")
    assert res.get("deadline_raw") == IOWA_CLOSE
    assert res.get("posted_raw") == IOWA_OPEN
    assert res.get("deadline") != res.get("posted_raw")

"""Focused tests for ApplicationClock + deadline_runtime intelligence."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from application_clock import (
    CLOCK_FROZEN_TEST,
    CLOCK_HISTORICAL_SIMULATION,
    CLOCK_SYSTEM,
    FrozenClock,
    SystemClock,
    clock_mode,
    freeze_time,
    get_clock,
    knowledge_cutoff_at,
    now_in_timezone,
    now_utc,
    reset_clock,
    set_clock,
    start_run_metadata,
    complete_run_metadata,
)
from deadline_conflict import deadline_evidence_record, reconcile_deadline_evidence
from deadline_runtime import (
    STATUS_DEADLINE_CONFLICT,
    STATUS_DEADLINE_UNKNOWN,
    STATUS_DUE_TODAY,
    STATUS_DUE_WITHIN_24_HOURS,
    STATUS_EXPIRED,
    STATUS_OPEN,
    apply_amendments,
    classify_queue_opportunities,
    evaluate_deadline,
    parse_procurement_deadline,
    resolve_iana_timezone,
)
from discovery.deadline_viability import VIABILITY_TOO_LATE, sort_operator_queue
from public_evidence_constants import EV_AUTHORITATIVE_CURRENT, EV_THIRD_PARTY_MIRROR


@pytest.fixture(autouse=True)
def _restore_system_clock():
    reset_clock()
    yield
    reset_clock()


def test_system_clock_timezone_aware_utc():
    reset_clock()
    assert isinstance(get_clock(), SystemClock)
    n = now_utc()
    assert n.tzinfo is not None
    assert n.utcoffset() == timedelta(0)
    assert clock_mode() == CLOCK_SYSTEM


def test_frozen_clock_deterministic():
    as_of = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    with freeze_time(as_of):
        assert clock_mode() == CLOCK_FROZEN_TEST
        assert now_utc() == as_of
        assert now_utc() == as_of  # stable
    assert clock_mode() == CLOCK_SYSTEM


def test_historical_simulation_clock_deterministic():
    as_of = datetime(2025, 1, 10, 18, 0, 0, tzinfo=timezone.utc)
    set_clock(FrozenClock(as_of, mode=CLOCK_HISTORICAL_SIMULATION))
    assert clock_mode() == CLOCK_HISTORICAL_SIMULATION
    assert now_utc() == as_of
    assert knowledge_cutoff_at() == as_of


def test_future_deadline_open():
    now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    with freeze_time(now):
        ev = evaluate_deadline(
            response_deadline="10/7/2026, 1:00 PM CDT",
            local_timezone="America/Chicago",
        )
    assert ev["deadline_status"] == STATUS_OPEN
    assert ev["actionable"] is True


def test_deadline_due_today():
    chicago = ZoneInfo("America/Chicago")
    now_local = datetime(2026, 10, 7, 9, 0, 0, tzinfo=chicago)
    with freeze_time(now_local.astimezone(timezone.utc)):
        ev = evaluate_deadline(response_deadline="10/7/2026, 1:00 PM CDT")
    assert ev["deadline_status"] == STATUS_DUE_TODAY


def test_deadline_within_24_hours():
    chicago = ZoneInfo("America/Chicago")
    # Oct 6 2pm → deadline Oct 7 1pm = 23h
    now_local = datetime(2026, 10, 6, 14, 0, 0, tzinfo=chicago)
    with freeze_time(now_local.astimezone(timezone.utc)):
        ev = evaluate_deadline(response_deadline="10/7/2026, 1:00 PM CDT")
    assert ev["deadline_status"] == STATUS_DUE_WITHIN_24_HOURS


def test_deadline_exactly_passed():
    chicago = ZoneInfo("America/Chicago")
    now_local = datetime(2026, 10, 7, 13, 0, 1, tzinfo=chicago)
    with freeze_time(now_local.astimezone(timezone.utc)):
        ev = evaluate_deadline(response_deadline="10/7/2026, 1:00 PM CDT")
    assert ev["deadline_status"] == STATUS_EXPIRED
    assert ev["actionable"] is False


def test_deadline_expired_yesterday():
    chicago = ZoneInfo("America/Chicago")
    now_local = datetime(2026, 10, 8, 10, 0, 0, tzinfo=chicago)
    with freeze_time(now_local.astimezone(timezone.utc)):
        ev = evaluate_deadline(response_deadline="10/7/2026, 1:00 PM CDT")
    assert ev["deadline_status"] == STATUS_EXPIRED


def test_timezone_conversion():
    as_of = datetime(2026, 9, 15, 17, 0, 0, tzinfo=timezone.utc)
    with freeze_time(as_of):
        local = now_in_timezone("America/Chicago")
    assert local.tzinfo is not None
    assert local.hour == 12  # CDT = UTC-5 in September


def test_timezone_boundary_around_midnight():
    chicago = ZoneInfo("America/Chicago")
    # 11pm CDT Oct 6 → still not due today for Oct 7 close
    now_local = datetime(2026, 10, 6, 23, 0, 0, tzinfo=chicago)
    with freeze_time(now_local.astimezone(timezone.utc)):
        ev = evaluate_deadline(response_deadline="10/7/2026, 1:00 PM CDT")
    assert ev["deadline_status"] == STATUS_DUE_WITHIN_24_HOURS
    assert ev["current_date_local"] == "2026-10-06"


def test_dst_transition_mapping():
    iana, conf = resolve_iana_timezone("CDT")
    assert iana == "America/Chicago" and conf == "KNOWN"
    # Spring forward 2026-03-08: 1:30 AM CST exists; 2:30 does not
    before = datetime(2026, 3, 8, 1, 0, 0, tzinfo=ZoneInfo("America/Chicago"))
    after = datetime(2026, 3, 8, 3, 0, 0, tzinfo=ZoneInfo("America/Chicago"))
    assert before.dst() != after.dst() or before.utcoffset() != after.utcoffset()


def test_date_only_deadline_no_invented_midnight():
    parsed = parse_procurement_deadline("October 7, 2026")
    assert parsed["date_only"] is True
    assert parsed["time_unknown"] is True
    assert parsed["deadline_at"] is None  # must not invent 00:00


def test_unknown_deadline_time():
    now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
    with freeze_time(now):
        ev = evaluate_deadline(response_deadline="10/7/2026", local_timezone="America/Chicago")
    assert ev["time_unknown"] is True
    assert ev["deadline_status"] == STATUS_DUE_TODAY
    assert ev["deadline_confidence"] in {"LOW", "MEDIUM"}


def test_unknown_timezone():
    parsed = parse_procurement_deadline("10/7/2026, 1:00 PM")
    assert parsed["timezone_confidence"] == "UNKNOWN"
    assert parsed["deadline_at"] is None  # no invented TZ


def test_open_vs_close_not_false_conflict():
    records = [
        deadline_evidence_record(
            raw="9/15/2026, 1:00 PM CDT",
            role="OPEN",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            confidence="HIGH",
        ),
        deadline_evidence_record(
            raw="10/7/2026, 1:00 PM CDT",
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            confidence="HIGH",
        ),
    ]
    recon = reconcile_deadline_evidence(records)
    assert recon.get("conflict_resolved") is True or recon.get("conflict") is False or recon["status"] in {
        "DEADLINE_RESOLVED",
        "DEADLINE_STALE_MIRROR",
        "DEADLINE_OPEN_VS_CLOSE_CLARIFIED",
    }
    with freeze_time(datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)):
        ev = evaluate_deadline(deadline_evidence=records)
    assert ev["deadline_status"] != STATUS_DEADLINE_CONFLICT
    assert "10/7/2026" in (ev["operational_deadline"] or "")
    assert ev["open_date"] and "9/15/2026" in ev["open_date"]


def test_iowa_open_not_response_deadline():
    records = [
        deadline_evidence_record(
            raw="9/15/2026, 1:00 PM CDT",
            role="OPEN",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            confidence="HIGH",
        ),
        deadline_evidence_record(
            raw="10/7/2026, 1:00 PM CDT",
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            confidence="HIGH",
        ),
        deadline_evidence_record(
            raw="9/15/2026",
            role="MIRROR_CLAIMED_DEADLINE",
            evidence_class=EV_THIRD_PARTY_MIRROR,
            confidence="LOW",
        ),
    ]
    with freeze_time(datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)):
        ev = evaluate_deadline(deadline_evidence=records)
    assert "10/7/2026" in (ev["operational_deadline"] or "")
    assert ev["open_is_not_response_deadline"] is True


def test_authoritative_amendment_supersedes_original():
    records = [
        deadline_evidence_record(
            raw="10/1/2026, 1:00 PM CDT",
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            confidence="HIGH",
        ),
        {
            **deadline_evidence_record(
                raw="10/7/2026, 1:00 PM CDT",
                role="AMENDMENT",
                evidence_class=EV_AUTHORITATIVE_CURRENT,
                confidence="HIGH",
            ),
            "is_amendment": True,
        },
    ]
    amended = apply_amendments(records)
    assert any(r.get("superseded") for r in amended if "10/1/2026" in (r.get("value") or ""))
    with freeze_time(datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)):
        ev = evaluate_deadline(deadline_evidence=records)
    assert "10/7/2026" in (ev["operational_deadline"] or "")


def test_unresolved_competing_closes_conflict_uses_earliest():
    records = [
        deadline_evidence_record(
            raw="10/1/2026, 1:00 PM CDT",
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            confidence="HIGH",
        ),
        deadline_evidence_record(
            raw="10/7/2026, 1:00 PM CDT",
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            confidence="HIGH",
        ),
    ]
    with freeze_time(datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)):
        ev = evaluate_deadline(deadline_evidence=records)
    assert ev["deadline_status"] == STATUS_DEADLINE_CONFLICT
    assert ev["conflict_resolved"] is False
    assert "10/1/2026" in (ev["operational_deadline"] or "")


def test_stale_portal_open_cannot_override_expired():
    chicago = ZoneInfo("America/Chicago")
    now_local = datetime(2026, 9, 16, 12, 0, 0, tzinfo=chicago)
    with freeze_time(now_local.astimezone(timezone.utc)):
        ev = evaluate_deadline(
            response_deadline="9/10/2026, 1:00 PM CDT",
            portal_status="OPEN",
        )
    assert ev["deadline_status"] == STATUS_EXPIRED
    assert ev["actionable"] is False
    assert ev["portal_status_overridden_by_clock"] is True


def test_historical_fixture_with_frozen_clock():
    fixture_now = datetime(2024, 6, 1, 15, 0, 0, tzinfo=timezone.utc)
    with freeze_time(fixture_now):
        ev = evaluate_deadline(response_deadline="6/15/2024, 1:00 PM EDT")
    assert ev["clock_mode"] == CLOCK_FROZEN_TEST
    assert ev["deadline_status"] == STATUS_OPEN


def test_same_opportunity_two_runtime_dates_changes_state():
    dl = "10/7/2026, 1:00 PM CDT"
    with freeze_time(datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)):
        early = evaluate_deadline(response_deadline=dl)
    with freeze_time(datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)):
        late = evaluate_deadline(response_deadline=dl)
    assert early["deadline_status"] == STATUS_OPEN
    assert late["deadline_status"] == STATUS_EXPIRED


def test_expired_removed_from_actionable_queue():
    chicago = ZoneInfo("America/Chicago")
    now_local = datetime(2026, 9, 16, 12, 0, 0, tzinfo=chicago)
    with freeze_time(now_local.astimezone(timezone.utc)):
        q = classify_queue_opportunities(
            [
                {
                    "id": "exp",
                    "portal_status": "ACTIVE",
                    "response_deadline": "9/10/2026, 1:00 PM CDT",
                },
                {
                    "id": "ok",
                    "response_deadline": "10/7/2026, 1:00 PM CDT",
                },
            ]
        )
    assert not any(r["id"] == "exp" for r in q["actionable_queue"])
    assert any(r["id"] == "exp" for r in q["expired_historical"])
    assert any(r["id"] == "ok" for r in q["actionable_queue"])


def test_due_today_and_within_24h_surfaced():
    chicago = ZoneInfo("America/Chicago")
    with freeze_time(datetime(2026, 10, 7, 9, 0, 0, tzinfo=chicago).astimezone(timezone.utc)):
        q = classify_queue_opportunities([{"id": "today", "response_deadline": "10/7/2026, 1:00 PM CDT"}])
    assert any(r["id"] == "today" for r in q["urgent_surface"])
    assert q["urgent_surface"][0]["deadline_status"] == STATUS_DUE_TODAY

    with freeze_time(datetime(2026, 10, 6, 14, 0, 0, tzinfo=chicago).astimezone(timezone.utc)):
        q2 = classify_queue_opportunities([{"id": "soon", "response_deadline": "10/7/2026, 1:00 PM CDT"}])
    assert q2["urgent_surface"][0]["deadline_status"] == STATUS_DUE_WITHIN_24_HOURS


def test_unknown_deadline_routed_for_review():
    with freeze_time(datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)):
        q = classify_queue_opportunities([{"id": "unk", "response_deadline": None}])
    assert any(r["id"] == "unk" for r in q["needs_deadline_review"])
    assert not any(r["id"] == "unk" for r in q["actionable_queue"])


def test_deadline_conflict_prominently_flagged():
    records = [
        deadline_evidence_record(
            raw="10/1/2026, 1:00 PM CDT",
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            confidence="HIGH",
        ),
        deadline_evidence_record(
            raw="10/7/2026, 1:00 PM CDT",
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            confidence="HIGH",
        ),
    ]
    with freeze_time(datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)):
        q = classify_queue_opportunities([{"id": "c", "deadline_evidence": records}])
    assert any(r["id"] == "c" for r in q["deadline_conflict_flagged"])


def test_no_production_hardcoded_current_date_in_clock_module():
    import application_clock as ac
    import inspect

    src = inspect.getsource(ac)
    assert "2026-09-15" not in src
    assert "2026-09-16" not in src
    assert "datetime(2026" not in src


def test_no_fixture_clock_leakage_after_context():
    with freeze_time(datetime(2020, 1, 1, tzinfo=timezone.utc)):
        assert clock_mode() == CLOCK_FROZEN_TEST
    assert clock_mode() == CLOCK_SYSTEM
    assert isinstance(get_clock(), SystemClock)


def test_run_metadata_clock_mode():
    with freeze_time(datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)):
        meta = start_run_metadata()
        meta = complete_run_metadata(meta)
    assert meta["clock_mode"] == CLOCK_FROZEN_TEST
    assert meta["run_started_at"]
    assert meta["run_completed_at"]


def test_sort_operator_queue_excludes_expired():
    chicago = ZoneInfo("America/Chicago")
    with freeze_time(datetime(2026, 9, 16, 12, 0, 0, tzinfo=chicago).astimezone(timezone.utc)):
        q = sort_operator_queue(
            [
                {"id": "exp", "response_deadline": "9/10/2026, 1:00 PM CDT", "research_priority": 99},
                {"id": "ok", "response_deadline": "10/7/2026, 1:00 PM CDT", "research_priority": 50},
            ]
        )
    assert not any(r.get("id") == "exp" for r in q["normal_queue"])
    assert any(r.get("id") == "exp" for r in q["expired_historical"])

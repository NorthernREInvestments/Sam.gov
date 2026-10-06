"""M3 daily discovery refresh + canonical population integrity (Cases A–J)."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def iso_root(tmp_path, monkeypatch):
    root = tmp_path / "m3data"
    root.mkdir()
    monkeypatch.setenv("M3_DATA_ROOT", str(root))
    from m3_data_root import set_data_root

    set_data_root(root)
    yield root
    set_data_root(None)


def _seed_store(n_available: int, n_dead: int = 0) -> dict:
    from phase_l.l23_full_population_funnel import save_store

    store = {}
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    for i in range(n_available):
        cid = f"avail{i:04d}"
        store[cid] = {
            "kind": "CanonicalOpportunityPopulation",
            "canonical_opportunity_id": cid,
            "title": f"Available Deal {i}",
            "buyer": "Test Agency",
            "deadline": future,
            "freshness": "LIVE_FRESH",
            "source_status": "ACTIVE_STATIC",
            "current_funnel_state": "ACCESSIBLE_PRODUCT",
            "source_provenance": [{"source_id": "seed"}],
            "row_ref": {"economics": {"expected_net_profit": 100 + i}},
            "owner_notes": f"note-{i}" if i == 0 else None,
            "supplier_research": {"vendors": ["Acme"]} if i == 0 else None,
            "pricing_history": [{"price": 10}] if i == 0 else None,
            "financing_assessment": {"stack": "OK"} if i == 0 else None,
        }
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for i in range(n_dead):
        cid = f"dead{i:04d}"
        store[cid] = {
            "kind": "CanonicalOpportunityPopulation",
            "canonical_opportunity_id": cid,
            "title": f"Dead Deal {i}",
            "buyer": "Test Agency",
            "deadline": past,
            "freshness": "EXPIRED",
            "source_status": "ACTIVE_STATIC",
            "current_funnel_state": "EXPIRED",
            "source_provenance": [{"source_id": "seed"}],
            "row_ref": {},
        }
    save_store(store)
    return store


def test_case_a_daily_run_adds_new(iso_root):
    from m3_canonical_discovery_bridge import available_count, merge_discovery_into_canonical
    from phase_l.l23_full_population_funnel import load_store

    _seed_store(80, 20)
    before = load_store()
    assert len(before) == 100
    assert available_count(before) == 80

    future = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    records = []
    # 5 new
    for i in range(5):
        records.append(
            {
                "title": f"Brand New Widget {i}",
                "agency": "City of Test",
                "solicitation_number": f"NEW-2026-{i}",
                "deadline": future,
                "detail_url": f"https://example.com/opp/new-{i}",
                "source_id": "state_test",
                "status": "OPEN",
                "cheap_screen_survive": True,
            }
        )
    # 10 updates to existing
    for i in range(10):
        records.append(
            {
                "title": f"Available Deal {i}",
                "agency": "Test Agency",
                "solicitation_number": f"UPD-{i}",
                "deadline": future,
                "detail_url": f"https://example.com/updated/{i}",
                "source_id": "state_test",
                "status": "OPEN",
                "canonical_opportunity_id": f"avail{i:04d}",
                "kind": "CanonicalOpportunityPopulation",
                "authoritative_url": f"https://example.com/updated/{i}",
                "freshness": "LIVE_FRESH",
                "source_status": "ACTIVE_STATIC",
                "current_funnel_state": "ACCESSIBLE_PRODUCT",
                "source_provenance": [{"source_id": "state_test", "url": f"https://example.com/updated/{i}"}],
            }
        )

    result = merge_discovery_into_canonical(
        run_id="TEST-A",
        trigger="manual",
        records=records,
        sources_attempted=["state_test"],
        sources_succeeded=["state_test"],
        sources_failed=[],
        raw_opportunities_found=15,
        records_normalized=15,
    )
    after = load_store()
    assert len(after) == 105
    assert result["new_canonical_opportunities_added"] == 5
    assert result["existing_opportunities_updated"] == 10
    assert result["run_status"] == "SUCCESS"
    hist = json.loads((iso_root / "m3_discovery_run_history.json").read_text(encoding="utf-8"))
    assert hist["runs"][0]["new_canonical_opportunities_added"] == 5
    assert hist["runs"][0]["existing_opportunities_updated"] == 10


def test_case_b_expiry_refresh(iso_root):
    from m3_canonical_discovery_bridge import available_count, merge_discovery_into_canonical
    from phase_l.l23_full_population_funnel import load_store, save_store

    store = _seed_store(5, 0)
    cid = "avail0000"
    store[cid]["deadline"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    store[cid]["freshness"] = "LIVE_FRESH"
    store[cid]["current_funnel_state"] = "ACCESSIBLE_PRODUCT"
    save_store(store)
    before_avail = available_count(load_store())
    assert before_avail == 5

    result = merge_discovery_into_canonical(
        run_id="TEST-B",
        trigger="scheduled",
        records=[],
        sources_attempted=["noop"],
        sources_succeeded=["noop"],
        sources_failed=[],
    )
    after = load_store()
    assert cid in after  # preserved
    assert after[cid]["freshness"] == "EXPIRED"
    assert available_count(after) == 4
    assert result["expired_canceled_opportunities_changed"] >= 1


def test_case_c_duplicate_two_sources(iso_root):
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical
    from phase_l.l23_full_population_funnel import load_store

    _seed_store(0, 0)
    future = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    base = {
        "title": "Shared Solicitation Dup",
        "agency": "Dup Agency",
        "solicitation_number": "SOL-DUP-1",
        "deadline": future,
        "status": "OPEN",
        "cheap_screen_survive": True,
    }
    records = [
        {**base, "source_id": "sam_federal", "detail_url": "https://sam.gov/opp/dup1"},
        {**base, "source_id": "state_local", "detail_url": "https://sam.gov/opp/dup1"},
    ]
    result = merge_discovery_into_canonical(
        run_id="TEST-C",
        trigger="manual",
        records=records,
        sources_attempted=["sam_federal", "state_local"],
        sources_succeeded=["sam_federal", "state_local"],
        sources_failed=[],
    )
    after = load_store()
    assert len(after) == 1
    assert result["duplicates_detected"] >= 1
    only = next(iter(after.values()))
    sources = {p.get("source_id") for p in (only.get("source_provenance") or [])}
    assert "sam_federal" in sources or "state_local" in sources
    assert len(only.get("source_provenance") or []) >= 2


def test_case_d_source_failure_partial(iso_root):
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical
    from phase_l.l23_full_population_funnel import load_store

    _seed_store(10, 0)
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    records = [
        {
            "title": f"State Local New {i}",
            "agency": "State Buyer",
            "solicitation_number": f"SL-{i}",
            "deadline": future,
            "detail_url": f"https://example.com/sl/{i}",
            "source_id": "state_local",
            "status": "OPEN",
            "cheap_screen_survive": True,
        }
        for i in range(8)
    ]
    result = merge_discovery_into_canonical(
        run_id="TEST-D",
        trigger="scheduled",
        records=records,
        sources_attempted=["sam_federal", "state_local"],
        sources_succeeded=["state_local"],
        sources_failed=["sam_federal"],
        error_summary="SAM failed",
    )
    assert result["run_status"] == "PARTIAL"
    assert result["new_canonical_opportunities_added"] == 8
    assert len(load_store()) == 18
    assert "sam_federal" in result["sources_failed"]


def test_case_e_sam_budget_does_not_block_nonsam(iso_root, monkeypatch):
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical
    from phase_l.l23_full_population_funnel import load_store

    _seed_store(3, 0)

    def _fake_sam():
        return {"used": 9, "limit": 10, "remaining": 1}

    monkeypatch.setattr(
        "m3_canonical_discovery_bridge._sam_usage_today",
        _fake_sam,
    )
    # Simulate non-SAM discovery continuing after near-budget
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    records = [
        {
            "title": "Non-SAM Coop Deal",
            "agency": "Coop",
            "solicitation_number": "COOP-1",
            "deadline": future,
            "detail_url": "https://example.com/coop/1",
            "source_id": "cooperative_source",
            "status": "OPEN",
            "cheap_screen_survive": True,
        }
    ]
    result = merge_discovery_into_canonical(
        run_id="TEST-E",
        trigger="scheduled",
        records=records,
        sources_attempted=["sam_federal", "cooperative_source"],
        sources_succeeded=["cooperative_source"],
        sources_failed=["sam_federal"],
        api_usage={"SAM": 9},
    )
    assert result["sam_calls_used"] == 9
    assert result["sam_calls_limit"] == 10
    assert result["new_canonical_opportunities_added"] == 1
    assert len(load_store()) == 4
    # Hard constraint preserved in reported limit
    assert int(result["sam_calls_limit"]) <= 10


def test_case_f_restart_persistence(iso_root):
    from m3_canonical_discovery_bridge import load_run_history, merge_discovery_into_canonical
    from m3_data_root import set_data_root
    from phase_l.l23_full_population_funnel import load_store

    _seed_store(2, 0)
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    merge_discovery_into_canonical(
        run_id="TEST-F",
        trigger="manual",
        records=[
            {
                "title": "Persist Me",
                "agency": "Agency",
                "solicitation_number": "PERSIST-1",
                "deadline": future,
                "detail_url": "https://example.com/p/1",
                "source_id": "state_tx",
                "status": "OPEN",
                "cheap_screen_survive": True,
            }
        ],
        sources_attempted=["state_tx"],
        sources_succeeded=["state_tx"],
        sources_failed=[],
    )
    # Simulate restart: clear in-memory data root cache, re-point
    set_data_root(None)
    set_data_root(iso_root)
    assert len(load_store()) == 3
    hist = load_run_history(limit=5)
    assert hist and hist[0]["run_id"] == "TEST-F"


def test_case_g_overlapping_run_rejected(iso_root, monkeypatch):
    import m3_discovery_service as mds

    state = mds._empty_state()
    state["current_run"] = {
        "run_id": "MDR-active",
        "status": mds.STATUS_RUNNING,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    state["lock"] = {"held": True, "run_id": "MDR-active", "since": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(mds, "_load_state", lambda: deepcopy(state))
    monkeypatch.setattr(mds, "recover_stale_runs", lambda: deepcopy(state))
    monkeypatch.setattr(mds, "_save_state", lambda s: None)

    result = mds.request_discovery_run(trigger_type=mds.TRIGGER_MANUAL)
    assert result["accepted"] is False
    assert result["already_running"] is True


def test_case_h_enrichment_preservation(iso_root):
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical
    from phase_l.l23_full_population_funnel import load_store

    _seed_store(1, 0)
    future = (datetime.now(timezone.utc) + timedelta(days=40)).isoformat()
    merge_discovery_into_canonical(
        run_id="TEST-H",
        trigger="scheduled",
        records=[
            {
                "canonical_opportunity_id": "avail0000",
                "kind": "CanonicalOpportunityPopulation",
                "title": "Available Deal 0",
                "buyer": "Test Agency",
                "deadline": future,
                "authoritative_url": "https://example.com/new-url",
                "freshness": "LIVE_FRESH",
                "source_status": "ACTIVE_STATIC",
                "current_funnel_state": "RAW",
                "source_provenance": [{"source_id": "rediscovery"}],
            }
        ],
        sources_attempted=["state_test"],
        sources_succeeded=["state_test"],
        sources_failed=[],
    )
    rec = load_store()["avail0000"]
    assert rec["deadline"] == future
    assert rec["supplier_research"] == {"vendors": ["Acme"]}
    assert rec["pricing_history"] == [{"price": 10}]
    assert rec["financing_assessment"] == {"stack": "OK"}
    assert rec["owner_notes"] == "note-0"
    assert rec["row_ref"]["economics"]["expected_net_profit"] == 100


def test_case_i_path_parity(iso_root, monkeypatch):
    from m3_canonical_discovery_bridge import discovery_diagnostics, path_parity_report

    ok = path_parity_report()
    assert ok["ok"] is True
    assert ok["canonical_read_path"] == ok["canonical_write_path"]

    monkeypatch.setattr(
        "m3_canonical_discovery_bridge.canonical_write_path",
        lambda: Path("/tmp/wrong-canonical-store.json"),
    )
    bad = path_parity_report()
    assert bad["ok"] is False
    assert bad["unhealthy_reason"] == "CANONICAL_READ_WRITE_PATH_MISMATCH"
    diag = discovery_diagnostics()
    assert diag["path_parity_ok"] is False


def test_case_j_daily_history(iso_root, monkeypatch):
    from m3_canonical_discovery_bridge import load_daily_history, merge_discovery_into_canonical, upsert_daily_history

    _seed_store(2, 0)
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    merge_discovery_into_canonical(
        run_id="TEST-J1",
        trigger="scheduled",
        records=[
            {
                "title": "Day One New",
                "agency": "A",
                "solicitation_number": "D1",
                "deadline": future,
                "detail_url": "https://example.com/d1",
                "source_id": "s1",
                "status": "OPEN",
                "cheap_screen_survive": True,
            }
        ],
        sources_attempted=["s1"],
        sources_succeeded=["s1"],
        sources_failed=[],
    )
    # Second day — insert prior day row then today's second run
    upsert_daily_history(
        {
            "date": "2026-09-30",
            "canonical": 2,
            "available": 2,
            "new": 0,
            "updated": 0,
            "expired": 0,
            "run_id": "PRIOR",
            "run_status": "SUCCESS",
        }
    )
    merge_discovery_into_canonical(
        run_id="TEST-J2",
        trigger="scheduled",
        records=[
            {
                "title": "Day Two New",
                "agency": "B",
                "solicitation_number": "D2",
                "deadline": future,
                "detail_url": "https://example.com/d2",
                "source_id": "s1",
                "status": "OPEN",
                "cheap_screen_survive": True,
            }
        ],
        sources_attempted=["s1"],
        sources_succeeded=["s1"],
        sources_failed=[],
    )
    days = load_daily_history()
    assert len(days) >= 2
    dates = {d["date"] for d in days}
    assert "2026-09-30" in dates
    today = datetime.now(timezone.utc).date().isoformat()
    assert today in dates
    today_row = next(d for d in days if d["date"] == today)
    assert today_row["new"] >= 1
    assert today_row["canonical"] >= 3

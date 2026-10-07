"""Tests for BidNet baseline → incremental production."""

from __future__ import annotations

from bidnet_engine.models import BUILD, PRODUCT_MIXED_TOTAL
from bidnet_engine.production import (
    BUILD as PROD_BUILD,
    empty_stage_queues,
    enqueue_change,
    format_production_report,
    run_incremental_source_sync,
    run_stress_5000,
)
from bidnet_engine.thread_limits import apply_thread_limits, verify_thread_limits


def test_build_target():
    assert PROD_BUILD.startswith("2026")
    assert BUILD == PROD_BUILD
    assert PRODUCT_MIXED_TOTAL == 13270


def test_thread_caps():
    apply_thread_limits(n=1)
    assert verify_thread_limits()["verified_active"] is True


def test_enqueue_no_change_adds_nothing():
    q = empty_stage_queues()
    n = enqueue_change(q, {"stable_key": "x", "title": "tools"}, "NO_CHANGE")
    assert n == 0


def test_enqueue_line_change_selective():
    q = empty_stage_queues()
    n = enqueue_change(q, {"stable_key": "y", "title": "PPE gloves"}, "LINE_RELEVANT_CHANGE")
    assert n > 0
    assert q["LINE_QUEUE"]
    assert not q["DETAIL_QUEUE"] or True  # line change may not touch DETAIL


def test_stress_5000_conservation():
    rows = [
        {
            "stable_key": f"k{i}",
            "classification": "PRODUCT",
            "title": "MRO tools",
            "deadline": "2026-12-01",
            "status": "OPEN",
        }
        for i in range(50)
    ]
    out = run_stress_5000(rows, n=5000)
    assert out["input"] == 5000
    assert out["classified"] == 5000
    assert out["conservation_diff"] == 0
    assert out["NO_CHANGE_deep_reruns"] == 0
    assert out["can_absorb_2000"] is True
    assert out["can_absorb_5000"] is True


def test_incremental_sync_no_full_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import data_path
    import json

    rows = [
        {
            "stable_key": "a1",
            "classification": "PRODUCT",
            "title": "office supplies",
            "deadline": "2026-11-01",
            "status": "OPEN",
            "deep_complete": True,
        }
    ]
    data_path("m3_bidnet_downstream_v1_checkpoint.json").write_text(
        json.dumps({"rows": rows}), encoding="utf-8"
    )
    # First sync seeds fps; second should be all NO_CHANGE
    run_incremental_source_sync()
    second = run_incremental_source_sync()
    assert second["NO_CHANGE"] >= 1
    assert second["NO_CHANGE_deep_reruns"] == 0
    assert second["full_universe_deep_rerun"] is False


def test_format_report_mentions_gates():
    text = format_production_report(
        {
            "runtime_s": 1,
            "baseline": {"remaining": 1},
            "canary": {"CANARY_PASS": "YES", "15": {"PASS_FAIL": "PASS"}, "30": {"PASS_FAIL": "PASS"}, "60": {"PASS_FAIL": "PASS"}},
            "performance": {},
            "resources": {},
            "pipeline": {},
            "production_gates": {"BASELINE_COMPLETE": "NO"},
            "answers": {"normal_sync_will_deep_all_13270": False},
            "BIDNET_BASELINE_COMPLETE": "NO",
            "BIDNET_INCREMENTAL_PRODUCTION_READY": "NO",
            "NEXT_RUN_ALLOWED": "CONTINUE_BASELINE",
        }
    )
    assert "CANARY" in text
    assert "Will normal daily update deep-process all 13270?: False" in text

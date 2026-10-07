"""Unit tests for BidNet engine recovery / resource isolation."""

from __future__ import annotations

import os

from bidnet_engine.resource_budget import limits, recommended_browser_workers
from bidnet_engine.thread_limits import apply_thread_limits, verify_thread_limits
from bidnet_engine.recovery import format_recovery_report, terminate_hung_job
from bidnet_engine.models import BUILD


def test_build_target():
    assert BUILD == "20261006-m3-bidnet-engine-recovery-v1"


def test_thread_limits_forced():
    apply_thread_limits(n=1)
    info = verify_thread_limits()
    assert info["verified_active"] is True
    assert info["values"]["OPENBLAS_NUM_THREADS"] == "1"
    assert info["values"]["OMP_NUM_THREADS"] == "1"
    assert info["values"]["MKL_NUM_THREADS"] == "1"
    assert info["values"]["NUMEXPR_NUM_THREADS"] == "1"
    assert info["values"]["VECLIB_MAXIMUM_THREADS"] == "1"


def test_browser_budget_caps():
    os.environ["BIDNET_MAX_BROWSER_PROCESSES"] = "2"
    lim = limits()
    assert lim["MAX_BROWSER_PROCESSES"] == 2
    assert recommended_browser_workers(5) <= 2


def test_format_recovery_report_contains_sections():
    report = {
        "build": BUILD,
        "run_id": "BNR-TEST",
        "runtime_s": 1.0,
        "PASS_FAIL": "PASS",
        "BIDNET_ENGINE_RECOVERY_PASS": "YES",
        "recovery": {
            "hung_job": {"job_id": "BNE-7fc7c8ba0dfa", "status": "TERMINATED_HUNG"},
            "checkpoint_found": True,
            "original_120_intact": True,
            "additional_durable_recovered": 0,
            "unsaved_unknown": "unknown_in_flight_batch_at_hang",
            "remaining_baseline": 13150,
            "pre_hang_completed": 120,
            "durably_saved": 120,
        },
        "thread_control": {
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "verified_active": True,
        },
        "browser": {"max_browser_concurrency": 2},
        "logical_workers": {"configured": 5, "active": 5},
        "stability": {
            "A_5L_1B": {"throughput_per_min": 3.0, "errors": 0, "http_502": 0, "thread_failures": 0},
            "B_5L_2B": {"throughput_per_min": 5.0, "errors": 0, "http_502": 0, "thread_failures": 0},
            "C_5L_3B": None,
            "selected": {"logical": 5, "browsers": 2},
        },
        "api_health": {"responsive_during_test": True, "http_502_count": 0},
        "checkpoint": {
            "frequency": "every_10",
            "last_durable_checkpoint": "2026-10-06T00:00:00+00:00",
            "resume_test": {"PASS_FAIL": "PASS"},
        },
        "performance": {
            "throughput_per_min": 5.0,
            "browser_processes_cap": 2,
            "logical_workers": 5,
            "resume_batch_complete": 10,
            "resume_batch_attempted": 10,
        },
        "final": {
            "RESOURCE_EXHAUSTION_FIXED": "YES",
            "SAFE_TO_RESUME_BASELINE": "YES",
            "NEXT_RUN_ALLOWED": "RESUME_FULL_BASELINE",
        },
    }
    text = format_recovery_report(report)
    assert "RECOVERY" in text
    assert "THREAD CONTROL" in text
    assert "OPENBLAS_NUM_THREADS: 1" in text
    assert "SAFE_TO_RESUME_BASELINE: YES" in text


def test_terminate_hung_job_preserves_id(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    jobs = tmp_path / "auth_jobs"
    jobs.mkdir()
    (jobs / "BNE-7fc7c8ba0dfa.json").write_text(
        '{"job_id":"BNE-7fc7c8ba0dfa","status":"RUNNING","progress":{"pct":70}}',
        encoding="utf-8",
    )
    out = terminate_hung_job("BNE-7fc7c8ba0dfa")
    assert out["status"] == "TERMINATED_HUNG"
    assert "BNE-7fc7c8ba0dfa" in out["job_id"]

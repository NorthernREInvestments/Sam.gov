"""Failure-injection tests for FSPF stall detection and recovery fencing."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_run_with_timeout_does_not_hang_on_stuck_thread():
    """Hung worker thread must not block the caller after timeout (root hang)."""
    from buyer_intelligence.full_scale_production_funnel_v1 import (
        _run_with_timeout_no_hang,
    )

    started = threading.Event()
    release = threading.Event()

    def hung():
        started.set()
        release.wait(30)
        return "done"

    t0 = time.time()
    out = _run_with_timeout_no_hang(
        hung, timeout_s=0.3, on_timeout=lambda: {"timed_out": True}
    )
    elapsed = time.time() - t0
    release.set()
    assert out == {"timed_out": True}
    assert elapsed < 2.0, f"caller hung for {elapsed:.1f}s awaiting stuck thread"
    assert started.is_set()


def test_lease_generation_fences_stale_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root
    from m3_jobs.store import JobLeaseError, JobStore

    set_data_root(tmp_path)
    store = JobStore("test_family", root=tmp_path / "jobs" / "test_family")
    job = store.create_job(total=2, opportunity_ids=["a", "b"])
    jid = job["JOB_ID"]
    lease1 = store.acquire_lease(jid, "worker-old", stale_seconds=30, force=True)
    gen1 = int((store.load_job(jid) or {}).get("LEASE_GENERATION") or 0)
    assert gen1 >= 1
    lease2 = store.acquire_lease(jid, "worker-new", stale_seconds=30, force=True)
    gen2 = int((store.load_job(jid) or {}).get("LEASE_GENERATION") or 0)
    assert gen2 == gen1 + 1
    # Old generation heartbeat must fail
    try:
        store.heartbeat(jid, worker_id="worker-old", lease_generation=gen1)
        raised = False
    except JobLeaseError:
        raised = True
    assert raised
    store.heartbeat(jid, worker_id="worker-new", lease_generation=gen2)
    assert (store.load_job(jid) or {}).get("WORKER_ID") == "worker-new"


def test_atomic_write_preserves_bak_on_replace_failure(tmp_path):
    from m3_jobs.store import _atomic_write
    import os

    target = tmp_path / "job.json"
    _atomic_write(target, {"JOB_ID": "x", "STATUS": "RUNNING", "v": 1})
    with patch.object(os, "replace", side_effect=OSError(2, "missing")):
        _atomic_write(target, {"JOB_ID": "x", "STATUS": "RUNNING", "v": 2})
    assert target.exists()
    bak = target.with_suffix(".json.bak")
    assert bak.exists()
    assert "RUNNING" in target.read_text(encoding="utf-8")


def test_supervisor_recovers_stale_job(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("M3_FSPF_SUPERVISOR", "1")
    monkeypatch.setenv("M3_FSPF_SUPERVISOR_POLL_S", "0.2")
    monkeypatch.setenv("M3_FSPF_STALL_DETECT_S", "0.4")
    monkeypatch.setenv("M3_FSPF_STALL_RECOVER_S", "0.6")
    monkeypatch.setenv("M3_FSPF_SUPERVISOR_COOLDOWN_S", "0.1")

    from m3_data_root import set_data_root
    from m3_jobs.store import JobStore
    import buyer_intelligence.fspf_supervisor_v1 as sup
    import buyer_intelligence.full_scale_production_funnel_v1 as fspf

    set_data_root(tmp_path)
    # Reload module thresholds from env
    import importlib

    importlib.reload(sup)

    store = JobStore(fspf.FAMILY, root=tmp_path / "jobs" / fspf.FAMILY)
    job = store.create_job(total=10, opportunity_ids=[f"o{i}" for i in range(10)])
    jid = job["JOB_ID"]
    store.acquire_lease(jid, "worker-dead", stale_seconds=1, force=True)
    job = store.load_job(jid) or {}
    job["STATUS"] = "STALE_RELEASED"
    job["COMPLETED"] = 3
    job["RESUME_CURSOR"] = 5
    job["TOTAL"] = 10
    job["HEARTBEAT_AT"] = "2020-01-01T00:00:00+00:00"
    store.save_job(job)
    store.root = tmp_path / "jobs" / fspf.FAMILY

    out_dir = tmp_path / "_og_commercial_research_pipeline_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Point FSPF loaders at tmp
    monkeypatch.setattr(fspf, "OUT", str(out_dir).replace("\\", "/"))
    monkeypatch.setattr(
        fspf,
        "PROGRESS_FILE",
        f"{fspf.OUT}/m3_fspf_v1_progress.json",
    )
    monkeypatch.setattr(
        fspf,
        "QUEUE_FILE",
        f"{fspf.OUT}/m3_fspf_v1_eligible_queue.json",
    )
    (out_dir / "m3_fspf_v1_progress.json").write_text(
        '{"status":"RUNNING","job_id":"%s","completed":3,"resume_cursor":5,"heartbeat_at":"2020-01-01T00:00:00+00:00"}'
        % jid,
        encoding="utf-8",
    )
    (out_dir / "m3_fspf_v1_eligible_queue.json").write_text(
        '{"eligible_total":10,"rows":[]}',
        encoding="utf-8",
    )

    started = {}

    def fake_start_job(**kwargs):
        started.update(kwargs)
        store.acquire_lease(jid, "worker-recovered", stale_seconds=30, force=True)
        return {
            "ok": True,
            "status": "STARTED",
            "job_id": jid,
            "worker_id": "worker-recovered",
        }

    # Patch JobStore used inside supervisor to use our root
    class _Store(JobStore):
        def __init__(self, family, root=None):
            super().__init__(family, root=tmp_path / "jobs" / family)

    with patch.object(sup, "POLL_S", 0.2), patch.object(
        sup, "STALL_DETECT_S", 0.4
    ), patch.object(sup, "STALL_RECOVER_S", 0.6), patch.object(
        sup, "RECOVERY_COOLDOWN_S", 0.1
    ):
        with patch(
            "buyer_intelligence.full_scale_production_funnel_v1.start_job",
            side_effect=fake_start_job,
        ):
            with patch("m3_jobs.store.JobStore", _Store):
                with patch.object(fspf, "_load") as load_mock:

                    def _load(rel):
                        from pathlib import Path as P
                        import json

                        name = P(rel).name
                        p = out_dir / name
                        if p.exists():
                            return json.loads(p.read_text(encoding="utf-8"))
                        return None

                    load_mock.side_effect = _load
                    # Directly invoke recovery path for deterministic unit proof
                    result = sup._recover_job(
                        store=_Store(fspf.FAMILY),
                        job_id=jid,
                        supervisor_id="test-sup",
                        start_job=fake_start_job,
                        reason="unit_test_stall",
                    )
    assert result["ok"] is True
    assert result["cursor_before"] == 5
    assert started.get("job_id") == jid
    assert started.get("force") is True
    assert started.get("rebuild_queue") is False
    assert started.get("new_run") is False
    assert (store.load_job(jid) or {}).get("WORKER_ID") == "worker-recovered"

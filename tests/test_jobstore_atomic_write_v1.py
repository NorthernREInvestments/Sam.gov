"""JobStore atomic write must survive volume mounts that reject *.json.tmp rename."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def test_atomic_write_falls_back_to_direct_write(tmp_path):
    from m3_jobs.store import _atomic_write

    target = tmp_path / "job.json"
    real_replace = Path.replace

    def boom(self, other):
        raise OSError(2, "No such file or directory")

    with patch.object(Path, "replace", boom):
        # Also make os.replace fail if used
        import os

        with patch.object(os, "replace", side_effect=OSError(2, "missing")):
            _atomic_write(target, {"JOB_ID": "x", "STATUS": "RUNNING"})
    assert target.exists()
    data = target.read_text(encoding="utf-8")
    assert "RUNNING" in data


def test_jobstore_heartbeat_survives_replace_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    from m3_jobs.store import JobStore
    import os

    store = JobStore("test_family", root=tmp_path / "jobs" / "test_family")
    job = store.create_job(total=3, opportunity_ids=["a", "b", "c"])
    jid = job["JOB_ID"]
    store.acquire_lease(jid, "worker-1", stale_seconds=600, force=True)

    with patch.object(os, "replace", side_effect=OSError(2, "missing")):
        store.heartbeat(jid, worker_id="worker-1", current_index=1, current_opportunity_id="b")
    reloaded = store.load_job(jid)
    assert reloaded is not None
    assert reloaded.get("WORKER_ID") == "worker-1"
    assert reloaded.get("HEARTBEAT_AT")

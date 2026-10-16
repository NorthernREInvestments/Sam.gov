"""v4e27: browser/auth taxonomy, circuit breaker, fenced session, no cross-thread close."""

from __future__ import annotations

import json
from pathlib import Path


def test_classify_playwright_error_as_context_invalid():
    from bidnet_auth.auth_lifecycle_v1 import (
        BROWSER_CONTEXT_INVALID,
        BROWSER_STARTUP_FAILED,
        classify_browser_exception,
        format_browser_unavailable,
    )

    class Error(Exception):
        pass

    assert classify_browser_exception(Error()) == BROWSER_CONTEXT_INVALID
    assert classify_browser_exception(Error("Target closed")) == BROWSER_CONTEXT_INVALID
    assert (
        classify_browser_exception(RuntimeError("Executable doesn't exist for chromium"))
        == BROWSER_STARTUP_FAILED
    )
    msg = format_browser_unavailable(Error("Browser has been closed"))
    assert msg.startswith("browser_unavailable:BROWSER_CONTEXT_INVALID:")
    assert "BROWSER_CONTEXT_INVALID" in msg


def test_circuit_opens_after_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("M3_BIDNET_BROWSER_CIRCUIT_FAILS", "2")
    monkeypatch.setenv("M3_BIDNET_BROWSER_CIRCUIT_COOLDOWN_S", "60")
    from m3_data_root import set_data_root
    import bidnet_auth.auth_lifecycle_v1 as life

    set_data_root(tmp_path)
    monkeypatch.setattr(life, "CIRCUIT_FAILURE_THRESHOLD", 2)
    monkeypatch.setattr(life, "CIRCUIT_COOLDOWN_S", 60.0)
    life.record_browser_failure(klass="BROWSER_STARTUP_FAILED", detail="t1")
    st = life.record_browser_failure(klass="BROWSER_STARTUP_FAILED", detail="t2")
    assert st["open"] is True
    assert st["remaining_s"] > 0
    life.record_browser_success()
    assert life.browser_circuit_status()["open"] is False


def test_session_fence_rejects_stale_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root
    import bidnet_auth.auth_lifecycle_v1 as life

    set_data_root(tmp_path)
    (tmp_path / "bidnet_auth").mkdir(parents=True)
    state = {"cookies": [{"name": "a", "value": "1", "domain": ".x", "path": "/"}], "origins": []}
    r1 = life.save_storage_state_fenced(state, writer="w1", min_generation=0)
    assert r1["ok"] is True
    gen = life.session_write_generation()
    assert gen >= 1
    # Stale writer with old min_generation must not clobber
    r2 = life.save_storage_state_fenced(
        {"cookies": [], "origins": []}, writer="stale", min_generation=0
    )
    assert r2.get("skipped") is True
    assert life.session_write_generation() == gen


def test_auth_failure_requeue_preserves_paid(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root
    import buyer_intelligence.fspf_auth_failure_recovery_v1 as rec

    set_data_root(tmp_path)
    out = tmp_path / "_og_commercial_research_pipeline_out"
    out.mkdir(parents=True)
    monkeypatch.setattr(rec, "OUT", str(out).replace("\\", "/"))
    monkeypatch.setattr(rec, "DEFERRED_QUEUE_FILE", f"{rec.OUT}/m3_fspf_v1_deferred_queue.json")

    def _reg(pg):
        return pg == "8407551"

    monkeypatch.setattr(
        "bidnet_auth.purchasing_group_tracker.is_registered", _reg, raising=False
    )
    # Patch the import used inside the function
    import bidnet_auth.purchasing_group_tracker as pgt

    monkeypatch.setattr(pgt, "is_registered", _reg)

    deferred = {
        "items": [
            {
                "opportunity_id": "auth1",
                "terminal_blocker": "BROWSER_CONTEXT_INVALID",
                "software_defect_key": "browser_auth_transient",
                "pg_unlocked": "8407551",
                "ready_fail_count": 2,
                "recovery_eligible": False,
            },
            {
                "opportunity_id": "paid1",
                # Browser outage coincided with a paid wall — must not re-open READY.
                "terminal_blocker": "BROWSER_CONTEXT_INVALID",
                "software_defect_key": "browser_auth_transient",
                "additional_access_dependency": "PAID_ENTITLEMENT:X",
                "pg_unlocked": "8407551",
                "recovery_eligible": False,
            },
            {
                # Pure browser slot busy — no PG required to requeue.
                "opportunity_id": "slot1",
                "terminal_blocker": "BROWSER_SLOT_BUSY",
                "software_defect_key": "browser_slot_busy",
                "fetch_via": "browser_unavailable_deferred",
                "recovery_eligible": False,
            },
        ],
        "count": 3,
    }
    Path(rec.DEFERRED_QUEUE_FILE).write_text(json.dumps(deferred), encoding="utf-8")
    report = rec.requeue_browser_auth_failures(job_id="job1")
    assert report["requeued"] == 2
    assert report["skipped_paid"] == 1
    dq = json.loads(Path(rec.DEFERRED_QUEUE_FILE).read_text(encoding="utf-8"))
    auth_row = next(x for x in dq["items"] if x["opportunity_id"] == "auth1")
    assert auth_row["recovery_eligible"] is True
    assert auth_row["ready_fail_count"] == 0
    slot = next(x for x in dq["items"] if x["opportunity_id"] == "slot1")
    assert slot["recovery_eligible"] is True
    paid = next(x for x in dq["items"] if x["opportunity_id"] == "paid1")
    assert paid.get("recovery_eligible") is not True


def test_recover_bidnet_browser_resets_circuit(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root
    import bidnet_auth.auth_lifecycle_v1 as life

    set_data_root(tmp_path)
    monkeypatch.setattr(life, "CIRCUIT_FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(life, "CIRCUIT_COOLDOWN_S", 120.0)
    life.record_browser_failure(klass="BROWSER_STARTUP_FAILED", detail="BlockingIOError")
    assert life.browser_circuit_status()["open"] is True
    monkeypatch.setattr(
        life,
        "force_cleanup_orphaned_chromium",
        lambda **kw: {"killed": 0, "examined": 0, "errors": [], "aggressive": True},
    )
    monkeypatch.setattr(life.time, "sleep", lambda *_a, **_k: None)
    out = life.recover_bidnet_browser(max_kill=4)
    assert out["ok"] is True
    assert life.browser_circuit_status()["open"] is False
    assert life.browser_circuit_status()["failures"] == 0


def test_solicitation_resolution_not_assigned_on_auth_failure():
    """Browser failure must not become SOLICITATION_RESOLUTION_FAILED."""
    from bidnet_auth.auth_lifecycle_v1 import classify_auth_result_message

    assert (
        classify_auth_result_message(
            status="AUTH_FAILED",
            message="browser_unavailable:BROWSER_CONTEXT_INVALID:Error:Target closed",
        )
        == "BROWSER_CONTEXT_INVALID"
    )

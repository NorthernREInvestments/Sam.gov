"""Background auth job dispatcher — accepts immediately."""

from __future__ import annotations

import time

import pytest


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    yield tmp_path
    set_data_root(None)


def test_bidnet_job_accepts_and_completes(monkeypatch, data_root):
    from m3_auth_jobs import get_job, start_bidnet_harvest_job

    def fake_harvest(**kwargs):
        return {
            "auth": {"status": "SESSION_REUSED"},
            "harvest": {"retrieved_total": 3, "reported_total": 10},
            "canonical_merge": {"new": 1},
            "blocker": None,
            "records_sample": [],
        }

    monkeypatch.setattr("bidnet_discovery.run_bidnet_authenticated_harvest", fake_harvest)
    out = start_bidnet_harvest_job(max_results=3, open_details=False)
    assert out["accepted"] is True
    job_id = out["job_id"]
    for _ in range(40):
        job = get_job(job_id)
        if job and job.get("status") in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.05)
    job = get_job(job_id)
    assert job is not None
    assert job["status"] == "COMPLETED"
    assert (job.get("result") or {}).get("harvest", {}).get("retrieved_total") == 3


def test_opengov_job_accepts(monkeypatch, data_root):
    from m3_auth_jobs import get_job, start_opengov_discovery_job

    monkeypatch.setattr(
        "opengov_discovery.run_opengov_authenticated_discovery",
        lambda **kwargs: {
            "auth": {"status": "LOGIN_SUCCESS"},
            "entities_attempted": 1,
            "entities_successful": 0,
            "portal_status_counts": {},
            "raw_opportunities": 0,
            "net_new": 0,
            "canonical_merge": {},
            "per_entity": {},
        },
    )
    out = start_opengov_discovery_job(max_entities=1)
    assert out["accepted"] is True
    for _ in range(40):
        job = get_job(out["job_id"])
        if job and job.get("status") in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.05)
    assert get_job(out["job_id"])["status"] == "COMPLETED"

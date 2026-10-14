"""Group-level purchasing-group registration queue."""

from __future__ import annotations


def test_enqueue_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from bidnet_auth.purchasing_group_queue import enqueue_group_registration, load_queue
    from bidnet_auth.purchasing_group_tracker import upsert_group

    a = enqueue_group_registration("88022651", opportunity_id="opp-1", name="Test")
    assert a["status"] == "ENQUEUED"
    b = enqueue_group_registration("88022651", opportunity_id="opp-2")
    assert b["status"] == "ALREADY_QUEUED"
    q = load_queue()
    assert len(q["pending"]) == 1
    assert set(q["pending"][0]["opportunity_ids"]) == {"opp-1", "opp-2"}

    upsert_group("88022651", status="REGISTERED")
    c = enqueue_group_registration("88022651", opportunity_id="opp-3")
    assert c["status"] == "ALREADY_REGISTERED"

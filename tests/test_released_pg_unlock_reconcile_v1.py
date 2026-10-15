"""Released PG-unlock reconciliation — no orphaned IDs after repair."""

from __future__ import annotations


def test_reconcile_repairs_orphaned_unlock_into_deferred(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    from bidnet_auth.purchasing_group_tracker import STATUS_REGISTERED, upsert_group
    from buyer_intelligence import full_scale_production_funnel_v1 as fspf
    from m3_jobs.store import JobStore

    upsert_group("8407551", status=STATUS_REGISTERED, relevant=True, blocked_opportunity_ids=["a", "b"])
    store = JobStore(fspf.FAMILY)
    job = store.create_job(total=3, opportunity_ids=["a", "b", "c"], meta={"build": fspf.BUILD})
    jid = job["JOB_ID"]
    # Orphan: unlock-eligible but not in deferred queue
    store.write_item_checkpoint(
        jid,
        {
            "OPPORTUNITY_ID": "a",
            "opportunity_id": "a",
            "RESULT": "PENDING_RETRY",
            "PG_UNLOCK_ELIGIBLE": True,
            "purchasing_group_id": "8407551",
            "shared_access_key": "pg:8407551",
            "stage_reached": "ACCESS_DOCS",
            "NOTE": "Released after purchasing group 8407551 REGISTERED",
        },
    )
    # Still deferred-external on registered PG — should be released
    store.write_item_checkpoint(
        jid,
        {
            "OPPORTUNITY_ID": "b",
            "opportunity_id": "b",
            "RESULT": "DEFERRED_EXTERNAL",
            "terminal_blocker": "AWAITING_PURCHASING_GROUP_REGISTRATION",
            "purchasing_group_id": "8407551",
            "shared_access_key": "pg:8407551",
            "stage_reached": "ACCESS_DOCS",
        },
    )
    # Completed — must not be requeued
    store.write_item_checkpoint(
        jid,
        {
            "OPPORTUNITY_ID": "c",
            "opportunity_id": "c",
            "RESULT": "SUCCESS",
            "PG_UNLOCK_ELIGIBLE": True,
            "purchasing_group_id": "8407551",
        },
    )

    out = fspf.reconcile_released_pg_unlocks(jid, repair=True)
    assert out["ok"] is True
    assert out["fully_accounted"] is True
    assert out["missing_or_orphaned"] == 0
    assert out["summary"]["completed"] >= 1
    assert out["summary"]["recovery_scheduled"] >= 2
    dq = fspf._load_deferred_queue()
    oids = {str(x.get("opportunity_id")) for x in (dq.get("items") or [])}
    assert "a" in oids
    assert "b" in oids
    assert "c" not in oids
    # Unlock-eligible sorted to front
    assert (dq.get("items") or [])[0].get("recovery_eligible") is True


def test_release_enqueues_deferred_without_resetting_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    from bidnet_auth.purchasing_group_tracker import STATUS_REGISTERED, upsert_group
    from buyer_intelligence import full_scale_production_funnel_v1 as fspf
    from m3_jobs.store import JobStore

    upsert_group("6605801", status=STATUS_REGISTERED, relevant=True)
    store = JobStore(fspf.FAMILY)
    job = store.create_job(total=1, opportunity_ids=["x"], meta={"build": fspf.BUILD})
    jid = job["JOB_ID"]
    job["RESUME_CURSOR"] = 77
    store.save_job(job)
    store.write_item_checkpoint(
        jid,
        {
            "OPPORTUNITY_ID": "x",
            "opportunity_id": "x",
            "RESULT": "DEFERRED_EXTERNAL",
            "terminal_blocker": "AWAITING_PURCHASING_GROUP_REGISTRATION",
            "purchasing_group_id": "6605801",
            "shared_access_key": "pg:6605801",
            "stage_reached": "ACCESS_DOCS",
            "INDEX": 5,
        },
    )
    out = fspf.release_pg_blocked_for_registered_groups(jid)
    assert out["released"] >= 1
    assert out["deferred_enqueued"] >= 1
    job2 = store.load_job(jid)
    assert int(job2.get("RESUME_CURSOR") or 0) == 77
    item = store.load_item(jid, "x") or {}
    assert item.get("RESULT") == "PENDING_RETRY"
    assert item.get("PG_UNLOCK_ELIGIBLE") is True
    dq = fspf._load_deferred_queue()
    assert any(str(x.get("opportunity_id")) == "x" for x in (dq.get("items") or []))

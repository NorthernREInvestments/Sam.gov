"""READY priority lane — atomic promotion, fairness, demotion."""

from __future__ import annotations

import json
from pathlib import Path


def test_interleave_fair_75_25():
    from buyer_intelligence.fspf_ready_lane_v1 import interleave_fair

    fp = [(i, {"opportunity_id": f"fp-{i}", "_fp": True}) for i in range(20)]
    rd = [(100 + i, {"opportunity_id": f"rd-{i}", "_ready_lane": True}) for i in range(10)]
    out, sched = interleave_fair(fp, rd, slice_size=20)
    assert len(out) == 20
    ready_n = sum(1 for _, it in out if it.get("_ready_lane"))
    fp_n = sum(1 for _, it in out if it.get("_fp"))
    assert ready_n == 5  # 1 of every 4
    assert fp_n == 15
    assert sched["ready"] == 5
    assert sched["first_pass"] == 15


def test_interleave_empty_ready_gives_all_to_first_pass():
    from buyer_intelligence.fspf_ready_lane_v1 import interleave_fair

    fp = [(i, {"opportunity_id": f"fp-{i}"}) for i in range(10)]
    out, sched = interleave_fair(fp, [], slice_size=8)
    assert len(out) == 8
    assert sched["ready"] == 0
    assert sched["first_pass"] == 8


def test_atomic_promote_ready_before_deferred_removal(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root
    from m3_jobs.store import JobStore
    import buyer_intelligence.fspf_ready_lane_v1 as lane
    import buyer_intelligence.full_scale_production_funnel_v1 as fspf

    set_data_root(tmp_path)
    out = tmp_path / "_og_commercial_research_pipeline_out"
    out.mkdir(parents=True)

    monkeypatch.setattr(lane, "OUT", str(out).replace("\\", "/"))
    monkeypatch.setattr(lane, "READY_QUEUE_FILE", f"{lane.OUT}/m3_fspf_v1_ready_queue.json")
    monkeypatch.setattr(
        lane, "DEFERRED_QUEUE_FILE", f"{lane.OUT}/m3_fspf_v1_deferred_queue.json"
    )
    monkeypatch.setattr(fspf, "OUT", lane.OUT)
    monkeypatch.setattr(fspf, "DEFERRED_QUEUE_FILE", lane.DEFERRED_QUEUE_FILE)
    monkeypatch.setattr(fspf, "QUEUE_FILE", f"{lane.OUT}/m3_fspf_v1_eligible_queue.json")

    store = JobStore(fspf.FAMILY, root=tmp_path / "jobs" / fspf.FAMILY)
    job = store.create_job(total=3, opportunity_ids=["a", "b", "c"])
    jid = job["JOB_ID"]
    # Checkpoint for unlock-eligible deferred item
    store.write_item_checkpoint(
        jid,
        {
            "OPPORTUNITY_ID": "a",
            "RESULT": "DEFERRED_EXTERNAL",
            "terminal_blocker": "PURCHASING_GROUP_REGISTRATION_REQUIRED",
            "stage_reached": "ACCESS_DOCS",
            "PG_UNLOCK_ELIGIBLE": True,
            "purchasing_group_id": "pg1",
            "INDEX": 0,
        },
    )
    deferred = {
        "kind": "FullScaleDeferredQueue",
        "items": [
            {
                "opportunity_id": "a",
                "recovery_eligible": True,
                "pg_unlocked": "pg1",
                "funnel_outcome": "PG_UNLOCK_RECOVERY",
                "queue_index": 0,
                "stage_reached": "ACCESS_DOCS",
            },
            {
                "opportunity_id": "b",
                "recovery_eligible": False,
                "funnel_outcome": "DEFERRED_ERROR",
                "software_defect_key": "TIMEOUT",
            },
        ],
        "count": 2,
    }
    Path(tmp_path / "_og_commercial_research_pipeline_out" / "m3_fspf_v1_deferred_queue.json").write_text(
        json.dumps(deferred), encoding="utf-8"
    )
    Path(tmp_path / "_og_commercial_research_pipeline_out" / "m3_fspf_v1_eligible_queue.json").write_text(
        json.dumps(
            {
                "eligible_total": 3,
                "rows": [
                    {"opportunity_id": "a", "title": "office supplies tools", "days_left": 10},
                    {"opportunity_id": "b", "title": "x"},
                    {"opportunity_id": "c", "title": "y"},
                ],
            }
        ),
        encoding="utf-8",
    )

    report = lane.promote_unlock_eligible_to_ready(job_id=jid)
    assert report["promoted"] == 1
    ready = lane.load_ready_queue()
    assert ready["count"] == 1
    assert ready["items"][0]["opportunity_id"] == "a"
    # Deferred should no longer hold `a` (moved to READY) but still hold error `b`
    dq = json.loads(
        (tmp_path / "_og_commercial_research_pipeline_out" / "m3_fspf_v1_deferred_queue.json").read_text(
            encoding="utf-8"
        )
    )
    dq_oids = {str(x.get("opportunity_id")) for x in dq["items"]}
    assert "a" not in dq_oids
    assert "b" in dq_oids


def test_demote_increments_fail_count_and_blocks_repromote(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root
    import buyer_intelligence.fspf_ready_lane_v1 as lane
    import buyer_intelligence.full_scale_production_funnel_v1 as fspf

    set_data_root(tmp_path)
    out = tmp_path / "_og_commercial_research_pipeline_out"
    out.mkdir(parents=True)
    monkeypatch.setattr(lane, "OUT", str(out).replace("\\", "/"))
    monkeypatch.setattr(lane, "READY_QUEUE_FILE", f"{lane.OUT}/m3_fspf_v1_ready_queue.json")
    monkeypatch.setattr(
        lane, "DEFERRED_QUEUE_FILE", f"{lane.OUT}/m3_fspf_v1_deferred_queue.json"
    )
    monkeypatch.setattr(fspf, "OUT", lane.OUT)
    monkeypatch.setattr(fspf, "DEFERRED_QUEUE_FILE", lane.DEFERRED_QUEUE_FILE)

    lane._persist_ready(
        {
            "items": [
                {
                    "opportunity_id": "x1",
                    "pg_unlocked": "pg9",
                    "queue_index": 1,
                    "stage_reached": "ACCESS_DOCS",
                }
            ]
        }
    )
    lane.demote_ready_to_deferred(
        opportunity_id="x1",
        reason="PER_OPPORTUNITY_TIMEOUT",
        defect_key="TIMEOUT",
        keep_recovery_eligible=True,
        pg_unlocked="pg9",
    )
    assert lane.ready_count() == 0
    dq = json.loads(
        (out / "m3_fspf_v1_deferred_queue.json").read_text(encoding="utf-8")
    )
    row = next(x for x in dq["items"] if x["opportunity_id"] == "x1")
    assert row["ready_fail_count"] == 1
    assert row["recovery_eligible"] is True
    # Second demote path via fail stamp
    row["ready_fail_count"] = 2
    row["ready_promoted"] = False
    dq["items"] = [row]
    (out / "m3_fspf_v1_deferred_queue.json").write_text(json.dumps(dq), encoding="utf-8")
    report = lane.promote_unlock_eligible_to_ready(job_id=None)
    assert report["promoted"] == 0
    assert report["skipped_retry_cap"] >= 1

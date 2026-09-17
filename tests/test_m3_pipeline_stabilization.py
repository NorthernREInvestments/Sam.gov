"""Pipeline handoff durability + source recovery prioritization tests."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_durable_handoff_reconciles_all_survivors(tmp_path, monkeypatch):
    from m3_end_to_end import M3EndToEndOrchestrator
    from m3_pipeline_handoff import (
        clear_handoff_checkpoint,
        reconcile_handoff,
        run_durable_handoff,
        save_handoff_checkpoint,
    )
    from m3_pipeline_store import M3PipelineStore

    monkeypatch.setattr("m3_pipeline_handoff._write_checkpoint_db", lambda p: None)
    monkeypatch.setattr("m3_pipeline_handoff._read_checkpoint_db", lambda: None)
    ckpt_path = tmp_path / "handoff.json"
    monkeypatch.setattr("m3_pipeline_handoff.DEFAULT_CHECKPOINT_PATH", ckpt_path)

    store = M3PipelineStore(path=tmp_path / "pipe.json", durable=False)
    orch = M3EndToEndOrchestrator(store=store)
    survivors = [
        {
            "title": f"Purchase of {i} laptops Dell Latitude",
            "external_id": f"HAND-TEST-{i}",
            "solicitation_number": f"HAND-TEST-{i}",
            "status": "OPEN",
            "source_id": "demo_source",
            "agency": "Demo Agency",
            "description": "Firm fixed price supply of computers delivery only",
            "deadline": "2099-12-01T17:00:00",
        }
        for i in range(12)
    ]
    heartbeats = []

    def on_progress(p):
        heartbeats.append(p)

    out = run_durable_handoff(
        run_id="MDR-test-handoff",
        survivors=survivors,
        store=store,
        orch=orch,
        on_progress=on_progress,
        checkpoint_every=5,
        resume=False,
    )
    assert out["status"] == "COMPLETE"
    recon = out["reconciliation"]
    assert recon["match"] is True
    assert recon["DISCOVERY_COUNT"] == 12
    assert recon["PIPELINE_COUNT"] == 12
    assert recon["MISSING_FROM_PIPELINE"] == 0
    assert len(store.all()) == 12
    assert heartbeats, "handoff must emit progress heartbeats"
    clear_handoff_checkpoint(path=ckpt_path)


def test_handoff_resume_after_partial(tmp_path, monkeypatch):
    from m3_end_to_end import M3EndToEndOrchestrator
    from m3_pipeline_handoff import begin_handoff_checkpoint, run_durable_handoff
    from m3_pipeline_store import M3PipelineStore
    from solicitation_identity import identity_key

    monkeypatch.setattr("m3_pipeline_handoff._write_checkpoint_db", lambda p: None)
    monkeypatch.setattr("m3_pipeline_handoff._read_checkpoint_db", lambda: None)
    ckpt_path = tmp_path / "handoff.json"
    monkeypatch.setattr("m3_pipeline_handoff.DEFAULT_CHECKPOINT_PATH", ckpt_path)

    store = M3PipelineStore(path=tmp_path / "pipe.json", durable=False)
    orch = M3EndToEndOrchestrator(store=store)
    survivors = [
        {
            "title": f"Buy widgets model {i}",
            "external_id": f"RESUME-{i}",
            "solicitation_number": f"RESUME-{i}",
            "status": "OPEN",
            "source_id": "demo",
            "description": "Product supply computers monitors",
            "deadline": "2099-06-01",
        }
        for i in range(6)
    ]
    ckpt = begin_handoff_checkpoint(run_id="MDR-resume", survivors=survivors)
    # Simulate partial progress: first 3 already processed
    for r in survivors[:3]:
        orch.ingest_discovery_record(r, persist=False)
    store.save()
    processed = [identity_key(r) for r in survivors[:3]]
    ckpt["status"] = "IN_PROGRESS"
    ckpt["processed_keys"] = processed
    ckpt["cursor"] = 3
    from m3_pipeline_handoff import save_handoff_checkpoint

    save_handoff_checkpoint(ckpt, path=ckpt_path)

    out = run_durable_handoff(
        run_id="MDR-resume",
        survivors=survivors,
        store=store,
        orch=orch,
        resume=True,
        checkpoint_every=2,
    )
    assert out["status"] == "COMPLETE"
    assert out["reconciliation"]["PIPELINE_COUNT"] == 6
    assert len(store.all()) == 6


def test_ingest_batch_does_not_advance_by_default(tmp_path):
    from m3_end_to_end import M3EndToEndOrchestrator
    from m3_pipeline_store import M3PipelineStore

    store = M3PipelineStore(path=tmp_path / "pipe.json", durable=False)
    orch = M3EndToEndOrchestrator(store=store)
    recs = [
        {
            "title": "Purchase 10 Dell laptops",
            "external_id": "BATCH-1",
            "status": "OPEN",
            "source_id": "x",
            "description": "computers supply delivery",
            "deadline": "2099-01-01",
        }
    ]
    out = orch.run_from_discovery_batch(recs, advance=False)
    assert out.get("advance") is False
    assert "advance" not in (out["results"][0] or {})
    row = store.all()[0]
    assert row.get("research_queued") is True


def test_source_recovery_queue_scores_and_buckets():
    from source_recovery_priority import build_source_recovery_queue, score_source

    cand = {
        "source_id": "state_xx_jaggaer",
        "name": "Example State SciQuest",
        "kind": "STATE",
        "platform_family": "JAGGAER_SCIQUEST",
        "list_url": "https://example.invalid",
    }
    scored = score_source(
        cand,
        metrics={"ok": False, "raw": 0, "source_stop_reason": "PARSER_FAILURE", "error": "parser"},
        family_size=18,
        peer_raw_avg=12.0,
    )
    assert scored["SOURCE_RECOVERY_SCORE"] > 0
    assert scored["SOURCES_UNLOCKED_PER_FIX"] == 18
    assert scored["portal_family"] == "JAGGAER_SCIQUEST"
    assert scored["current_state"] in {
        "PARSER_FAILURE",
        "TECHNICAL_FAILURE",
        "STALE_ROUTE",
        "BOT_PROTECTED",
        "AUTH_REQUIRED",
        "REGISTRATION_REQUIRED",
    }

    q = build_source_recovery_queue(per_source_metrics={}, limit=10)
    assert q["kind"] == "TOP_SOURCE_RECOVERY_QUEUE"
    assert "TOP_10" in q
    assert "family_leverage" in q
    assert "operator_action_queue" in q
    assert q.get("DEVELOPMENT_NO_OUTREACH") is True


def test_coverage_dashboard_shape():
    from source_recovery_priority import coverage_dashboard_payload

    dash = coverage_dashboard_payload(
        discovery_status={
            "last_successful_completion": {
                "sources_attempted": 77,
                "sources_successful": 17,
                "records_retrieved": 275,
                "unique_records": 223,
                "product_screen_survivors": 223,
                "deep_research_queued": 50,
            },
            "PIPELINE_HANDOFF": {
                "discovered": 223,
                "transferred": 223,
                "failed": 0,
                "status": "COMPLETE",
            },
            "DISCOVERY": {
                "sources_attempted": 77,
                "records_fetched": 275,
                "unique_records": 223,
                "product_survivors": 223,
            },
            "RESEARCH": {"queued": 50},
        }
    )
    assert dash["kind"] == "M3CoverageDashboard"
    assert "TOTAL_SOURCES" in dash
    assert "DISCOVERY" in dash
    assert "PIPELINE" in dash
    assert "SOURCE_RECOVERY" in dash
    assert dash["PIPELINE"]["Discovered"] == 223
    assert dash["PIPELINE"]["Stored"] == 223


def test_discovery_status_includes_handoff_blocks(monkeypatch):
    from m3_discovery_service import discovery_status, _empty_state

    monkeypatch.setattr(
        "m3_discovery_service._load_state",
        lambda: {
            **_empty_state(),
            "last_successful_completion": {
                "run_id": "MDR-x",
                "status": "COMPLETED",
                "completed_at": "2099-01-01T00:00:00+00:00",
                "sources_attempted": 10,
                "sources_successful": 5,
                "product_screen_survivors": 20,
                "handoff_status": "COMPLETE",
                "handoff_discovered": 20,
                "handoff_transferred": 20,
                "reconciliation": {
                    "match": True,
                    "DISCOVERY_COUNT": 20,
                    "PIPELINE_COUNT": 20,
                    "MISSING_FROM_PIPELINE": 0,
                    "RESEARCH_QUEUE_COUNT": 8,
                },
            },
        },
    )
    monkeypatch.setattr("m3_discovery_service.recover_stale_runs", lambda state=None: _load_patched())

    def _load_patched():
        return {
            **_empty_state(),
            "last_successful_completion": {
                "run_id": "MDR-x",
                "status": "COMPLETED",
                "completed_at": "2099-01-01T00:00:00+00:00",
                "sources_attempted": 10,
                "sources_successful": 5,
                "product_screen_survivors": 20,
                "handoff_status": "COMPLETE",
                "handoff_discovered": 20,
                "handoff_transferred": 20,
                "reconciliation": {
                    "match": True,
                    "DISCOVERY_COUNT": 20,
                    "PIPELINE_COUNT": 20,
                    "MISSING_FROM_PIPELINE": 0,
                    "RESEARCH_QUEUE_COUNT": 8,
                },
            },
        }

    monkeypatch.setattr("m3_discovery_service.recover_stale_runs", lambda state=None: _load_patched())
    monkeypatch.setattr("m3_discovery_service.is_data_fresh", lambda state=None: True)
    st = discovery_status()
    assert "DISCOVERY" in st
    assert "PIPELINE_HANDOFF" in st
    assert "RESEARCH" in st
    assert st["PIPELINE_HANDOFF"]["status"] == "COMPLETE"
    assert st["PIPELINE_HANDOFF"]["discovered"] == 20


def test_dla_fallback_tracks_metrics_shape():
    from discovery.dla_fallback import search_sam_dla_product_opportunities

    # Without authorize_live — structured empty result
    out = search_sam_dla_product_opportunities(authorize_live=False)
    assert out["executed"] is False
    assert out["opportunities"] == []

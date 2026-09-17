"""Pipeline + discovery status must survive ephemeral filesystem (Railway)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from m3_pipeline_store import M3PipelineStore, PIPELINE_SETTINGS_KEY, _payload_from_rows


def test_pipeline_prefers_durable_over_empty_file(tmp_path, monkeypatch):
    file_path = tmp_path / "pipe.json"
    # Ephemeral empty/stale file (what Railway leaves after restart if something touches the path)
    file_path.write_text(
        json.dumps({"kind": "M3PipelineStore", "opportunities": [], "audit": []}),
        encoding="utf-8",
    )
    durable = {
        "kind": "M3PipelineStore",
        "updated_at": "2026-09-17T00:00:00+00:00",
        "opportunity_count": 2,
        "opportunities": [
            {
                "canonical_id": "sol:a",
                "title": "Durable A",
                "lifecycle": "RESEARCH_QUEUED",
                "deadline": "2099-01-01",
            },
            {
                "canonical_id": "sol:b",
                "title": "Durable B",
                "lifecycle": "RESEARCH_QUEUED",
                "deadline": "2099-01-02",
            },
        ],
        "audit": [],
    }

    with patch("m3_pipeline_store._read_durable_payload", return_value=durable):
        store = M3PipelineStore(path=file_path, durable=True)
    assert len(store.all()) == 2
    assert store.get("sol:a")["title"] == "Durable A"


def test_pipeline_save_writes_durable(tmp_path):
    file_path = tmp_path / "pipe.json"
    written = {}

    def fake_write(payload):
        written["payload"] = payload

    with patch("m3_pipeline_store._write_durable_payload", side_effect=fake_write):
        with patch("m3_pipeline_store._read_durable_payload", return_value=None):
            store = M3PipelineStore(path=file_path, durable=True)
            store.upsert_from_discovery(
                {
                    "title": "Laptop buy",
                    "solicitation_number": "X-1",
                    "external_id": "X-1",
                    "source_id": "demo",
                    "status": "OPEN",
                    "product_classification": "CORE_PRODUCT",
                }
            )
            store.save()
    assert file_path.exists()
    assert written["payload"]["opportunity_count"] >= 1
    assert any(o.get("solicitation_number") == "X-1" or "X-1" in str(o.get("canonical_id")) for o in written["payload"]["opportunities"])


def test_reload_from_durable_refreshes_memory(tmp_path):
    file_path = tmp_path / "pipe.json"
    file_path.write_text(json.dumps({"opportunities": [], "audit": []}), encoding="utf-8")
    durable = {
        "opportunities": [
            {"canonical_id": "sol:z", "title": "From DB", "lifecycle": "RESEARCH_QUEUED"},
        ],
        "audit": [],
    }
    with patch("m3_pipeline_store._read_durable_payload", return_value=durable):
        store = M3PipelineStore(path=file_path, durable=True)
        assert len(store.all()) == 1
        # Wipe memory/file as if ephemeral vanished
        store._rows = {}
        file_path.write_text(json.dumps({"opportunities": []}), encoding="utf-8")
        n = store.reload_from_durable()
    assert n == 1
    assert store.get("sol:z")["title"] == "From DB"


def test_mobile_dashboard_uses_durable_pipeline(tmp_path):
    from m3_mobile_read_model import mobile_dashboard_summary

    file_path = tmp_path / "dash.json"
    file_path.write_text(json.dumps({"opportunities": []}), encoding="utf-8")
    durable = {
        "opportunities": [
            {
                "canonical_id": "sol:home1",
                "title": "Home Visible Deal",
                "lifecycle": "RESEARCH_QUEUED",
                "deadline": "2099-06-01",
                "agency": "Test",
            }
        ],
        "audit": [],
    }
    with patch("m3_pipeline_store._read_durable_payload", return_value=durable):
        store = M3PipelineStore(path=file_path, durable=True)
        dash = mobile_dashboard_summary(store)
    assert dash["active_count"] >= 1
    assert any(o.get("title") == "Home Visible Deal" for o in dash["active_opportunities"])


def test_ui_has_discovery_status_placeholder():
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    js = (Path(__file__).resolve().parents[1] / "static" / "m3-mobile.js").read_text(encoding="utf-8")
    assert 'id="m3-discovery-status"' in html
    assert "Loading discovery status" in html
    assert 'id="m3-research-status"' in html
    assert "Loading research status" in html
    assert "credentials: \"same-origin\"" in js or "credentials: 'same-origin'" in js
    assert "reload_from_durable" in (Path(__file__).resolve().parents[1] / "m3_pipeline_store.py").read_text(encoding="utf-8")

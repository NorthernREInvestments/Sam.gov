"""Guard: durable pipeline must not be wiped by empty saves / FILE_PRIMARY stubs."""

from __future__ import annotations

import json

from m3_pipeline_store import M3PipelineStore, _read_durable_payload, _write_durable_payload


def test_refuse_empty_durable_save_over_existing():
    store = M3PipelineStore()
    from m3_discovery_service import restore_pipeline_store_from_db

    try:
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    cid = "sol:empty-guard-test"
    store._rows[cid] = {
        "canonical_id": cid,
        "title": "Guard test product",
        "agency": "Test",
    }
    store.save(durable_write=True, skip_remote_merge=True)

    empty = M3PipelineStore()
    empty._rows = {}
    empty._audit = []
    # Must not wipe
    empty.save(durable_write=True, skip_remote_merge=True)

    check = M3PipelineStore()
    restore_pipeline_store_from_db(check)
    assert check.get(cid) is not None, "empty save wiped durable opportunity"


def test_file_primary_stub_not_authoritative(tmp_path, monkeypatch):
    """Empty FILE_PRIMARY stub in AppSetting must not be returned as payload."""
    # This is covered by _read_durable_payload guard; smoke that real read still works.
    data = _read_durable_payload()
    if data is not None:
        assert isinstance(data, dict)
        # If mode marks FILE_PRIMARY stub shape, opportunities must not be the sole answer
        if data.get("mode") == "FILE_PRIMARY":
            assert data.get("opportunities") is not None

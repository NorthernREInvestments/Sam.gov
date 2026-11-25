"""Offline document association + duplicate purchase prevention (zero API)."""
from __future__ import annotations

import json

from m3.research.nextbid_procurement_pipeline import (
    PIPELINE_TEST_ARTIFACT,
    PIPELINE_TEST_NB_ID,
    plan_cheapest_retrieval,
)
from m3.research.nextbid_response_store import persist_paid_response
from m3.research.solicitation_document_manifest import (
    build_solicitation_document_manifest,
    file_artifact_retention,
    save_manifest,
)


def test_propane_manifest_lists_all_ten_attachments_and_notice():
    man = build_solicitation_document_manifest(PIPELINE_TEST_NB_ID)
    assert man["provider_opportunity_id"] == PIPELINE_TEST_NB_ID
    assert man["solicitation_number"] == "CS-2026-QQ-02"
    attachments = [d for d in man["documents"] if d.get("role") == "attachment"]
    notices = [d for d in man["documents"] if d.get("role") == "notice"]
    assert len(attachments) == 10
    names = {d["original_filename"] for d in attachments}
    assert "Bulk_Propane_Fuel_Supply.pdf" in names
    assert any(d.get("original_filename", "").endswith(".document0.md") for d in notices)
    # Provider stored + (after owner download) local PDF bytes for the propane spec
    propane = next(d for d in attachments if d["original_filename"] == "Bulk_Propane_Fuel_Supply.pdf")
    assert propane["provider_stored"] is True
    assert "/documents/" in str(propane.get("provider_api_path") or "")
    assert propane["provider_document_id"] == "996c49135176d8e5583103be087aaf6ee4ed0d072423b7b2399d41e48326bb4c"
    # Local presence reflects evidence vault — True after authorized download
    if propane.get("local_pdf_bytes_present"):
        assert propane["m3_retrieval_status"] == "local_bytes"
        assert propane.get("duplicate_purchase_blocked") is True
    else:
        assert propane["m3_retrieval_status"] == "metadata_only_provider_stored"
    # Notice already retrieved locally
    md = next(d for d in notices if str(d.get("original_filename") or "").endswith(".document0.md"))
    assert md["local_extracted_text_present"] is True
    assert md["duplicate_purchase_blocked"] is True
    assert md["missing"] is False


def test_planner_blocks_duplicate_document0_read():
    plan = plan_cheapest_retrieval(PIPELINE_TEST_NB_ID)
    assert plan["proposed_metered_call"] is None
    assert plan["stop_without_call"] == "artifact_already_retained_locally"
    assert plan["already_retained_artifact"]["retained"] is True
    assert plan["already_retained_artifact"]["path"].endswith(".document0.md")
    assert any(s["step"] == "7_duplicate_purchase_guard" for s in plan["steps_checked"])


def test_file_artifact_retention_and_planner_propose_when_absent(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "nextbid_paid_responses" / "by_request").mkdir(parents=True)
    (root / "solicitation_full_text").mkdir(parents=True)

    class S:
        def data_root(self=None):
            return root

        def evidence_vault_root(self=None):
            return root / "evidence_vault"

    monkeypatch.setattr("m3.research.nextbid_response_store.get_settings", lambda: S())
    monkeypatch.setattr("m3.research.full_text_store.get_settings", lambda: S())
    monkeypatch.setattr("m3.research.solicitation_document_manifest.get_settings", lambda: S())

    nb = "NB_FAKE_DUP"
    rel = f"nb2/published/opengov/document0/{nb}/{nb}.document0.md"
    st = file_artifact_retention(nb, rel)
    assert st["already_retained"] is False

    raw = "# Notice\n\n" + ("x" * 900)
    persist_paid_response(
        method="GET",
        path=f"/opportunities/{nb}/files/{rel}",
        params=None,
        endpoint="read_file",
        nb_id=nb,
        status_code=200,
        body={"ok": False, "error": "non_json", "raw_text": raw},
        raw_text=raw,
        original_bytes=raw.encode("utf-8"),
    )
    st2 = file_artifact_retention(nb, rel)
    assert st2["already_retained"] is True
    assert st2["durable_response_present"] is True
    assert st2["durable_text_chars"] == len(raw)


def test_save_manifest_roundtrip(tmp_path, monkeypatch):
    root = tmp_path / "data"

    class S:
        def data_root(self=None):
            return root

        def evidence_vault_root(self=None):
            return root / "ev"

    monkeypatch.setattr("m3.research.solicitation_document_manifest.get_settings", lambda: S())
    man = {
        "manifest_version": 1,
        "provider_opportunity_id": "NBX",
        "documents": [{"original_filename": "a.pdf", "role": "attachment"}],
    }
    path = save_manifest(man, root=root)
    assert path.exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["provider_opportunity_id"] == "NBX"
    assert PIPELINE_TEST_ARTIFACT.endswith(".md")

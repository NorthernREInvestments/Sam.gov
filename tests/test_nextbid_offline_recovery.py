"""Offline recovery of paid NextBid responses — raw_text + source_documents (bulk)."""
from __future__ import annotations

import json
from pathlib import Path

from m3.research.nextbid_acquire import nextbid_file_text
from m3.research.nextbid_offline_recovery import (
    _enrichment_row,
    _is_enrichment_worthy,
    apply_recovery_to_evidence,
    enrichment_path,
    recover_nb_package,
)


def test_nextbid_file_text_reads_ignored_raw_text():
    body = {"ok": False, "error": "non_json", "raw_text": "# Notice\n\nQty: 12 EA widget"}
    assert "Qty: 12 EA widget" in nextbid_file_text(body)


def test_recover_nb_package_merges_source_documents_and_raw_text():
    rows = [
        {
            "endpoint": "search",
            "nb_id": "NB99990001",
            "body": {
                "results": [
                    {
                        "nb_id": "NB99990001",
                        "title": "Widgets",
                        "description": "Short listing",
                        "agency": "Test",
                    }
                ]
            },
        },
        {
            "endpoint": "list_documents",
            "nb_id": "NB99990001",
            "body": {
                "documents": [],
                "source_documents": [
                    {"name": "spec.pdf", "stored": True, "sha256": "abc", "path": "/x/spec.pdf"}
                ],
                "nextbid_documents": [{"path": "/files/document0.md", "kind": "document0"}],
            },
        },
        {
            "endpoint": "read_file",
            "nb_id": "NB99990001",
            "path": "/files/document0.md",
            "body": {
                "ok": False,
                "error": "non_json",
                "raw_text": "Line item 1: Widget Model A\nQuantity: 25 EA\nBrand name or equal\nSubmit electronically",
            },
        },
    ]
    pkg = recover_nb_package("NB99990001", rows)
    assert pkg["markdown_raw_text_recovered_count"] == 1
    assert pkg["source_documents_count"] == 1
    assert "Quantity: 25 EA" in (pkg["document_text"] or "")
    assert pkg["local_original_pdf_bytes_available"] == 0
    assert pkg["stored_original_refs_metadata_only"] == 1
    assert _is_enrichment_worthy(pkg)
    row = _enrichment_row(pkg, vault_sha="deadbeef")
    assert "document_text" not in row or row.get("document_text") is None
    assert row["evidence_vault_sha256"] == "deadbeef"
    assert row["source_documents_count"] == 1


def test_search_only_not_enrichment_worthy():
    pkg = recover_nb_package(
        "NB99990003",
        [
            {
                "endpoint": "search",
                "nb_id": "NB99990003",
                "body": {"results": [{"nb_id": "NB99990003", "title": "X", "description": "y" * 50}]},
            }
        ],
    )
    assert not _is_enrichment_worthy(pkg)


def test_apply_recovery_from_enrichment_jsonl(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "nextbid_offline_recovery").mkdir(parents=True)
    (root / "solicitation_full_text").mkdir(parents=True)
    row = {
        "nb_id": "NB99990002",
        "description": "Recovered longer description that was ignored before",
        "document_text_chars": 80,
        "source_documents": [{"name": "a.pdf"}],
        "nextbid_documents": [],
        "documents_legacy": [],
        "file_index": [],
        "notice_fields": {},
        "markdown_raw_text_recovered_count": 1,
        "source_documents_count": 1,
        "endpoints_present": ["read_file"],
        "local_original_text_bodies": 1,
        "stored_original_refs_metadata_only": 0,
        "local_original_pdf_bytes_available": 0,
        "full_text_source": "nextbid_offline_recovery_raw_text",
    }
    enrichment_path(root).write_text(json.dumps(row) + "\n", encoding="utf-8")
    (root / "solicitation_full_text" / "NB99990002.json").write_text(
        json.dumps({
            "record_key": "NB99990002",
            "full_text": "Full recovered notice with Qty 10 EA and specifications shall meet ASTM",
            "full_text_chars": 80,
            "source": "nextbid_offline_recovery_raw_text",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "m3.research.nextbid_offline_recovery.get_settings",
        lambda: type("S", (), {"data_root": lambda self=None: root})(),
    )
    monkeypatch.setattr(
        "m3.research.full_text_store.get_settings",
        lambda: type("S", (), {"data_root": lambda self=None: root})(),
    )
    # clear cache
    import m3.research.nextbid_offline_recovery as mod

    mod._ENRICHMENT_CACHE = None
    original = {"description": "short"}
    sources: list[str] = []
    refs: list[str] = []
    original, doc = apply_recovery_to_evidence(
        "NB99990002",
        original=original,
        doc_text="thin",
        sources_used=sources,
        cache_refs=refs,
        data_root=root,
    )
    assert "Recovered longer" in original["description"]
    assert "Qty 10 EA" in doc
    assert original["source_documents"]
    assert "nextbid_offline_recovery" in sources

"""Evidence chain preservation + source traceability tests."""

from __future__ import annotations

from m3_discovery_service import _records_from_live_result
from m3_evidence_chain import (
    FOUND_AT_DISCOVERY,
    NOT_FOUND,
    analyze_evidence_chain_top,
    build_discovery_evidence_record,
    build_evidence_chain,
    detect_data_loss_across_modules,
    enrich_discovery_record_for_pipeline,
    ensure_evidence_chain_on_row,
    merge_preserve_evidence,
    validate_pipeline_handoff_preservation,
)
from m3_pipeline_store import M3PipelineStore


def _raw_discovery_item(**kwargs):
    base = {
        "title": "Cisco Catalyst Switches",
        "solicitation_number": "IFB-99",
        "external_id": "IFB-99",
        "agency": "State DoIT",
        "source_id": "illinois_bidbuy",
        "preferred_source_url": "https://example.test/opp/ifb-99",
        "detail_url": "https://example.test/opp/ifb-99",
        "description": "Network switches",
        "documents": [
            {
                "name": "Pricing Schedule.pdf",
                "url": "https://example.test/docs/pricing.pdf",
                "document_type": "pricing_schedule",
            }
        ],
        "attachment_links": ["https://example.test/docs/spec.pdf"],
        "raw_metadata": {"noticeId": "N-123", "postedDate": "2026-09-01", "extra": "keep-me"},
        "api_endpoint": "https://example.test/api/opportunities",
    }
    base.update(kwargs)
    return base


def test_evidence_record_creation_and_source_preservation():
    der = build_discovery_evidence_record(_raw_discovery_item(), discovery_run_id="run-1")
    assert der["kind"] == "DISCOVERY_EVIDENCE_RECORD"
    assert der["Source_URL"] == "https://example.test/opp/ifb-99"
    assert der["Discovery_run_ID"] == "run-1"
    assert der["Solicitation_ID"] == "IFB-99"
    assert der["Evidence_confidence"] in {"HIGH", "MEDIUM"}
    assert any("pricing" in str(d.get("url") or "").lower() or "pricing" in str(d.get("name") or "").lower() for d in der["Document_references"])
    assert "noticeId" in (der.get("Raw_discovery_metadata") or {})


def test_document_reference_retention_through_enrich():
    enriched = enrich_discovery_record_for_pipeline(_raw_discovery_item(), discovery_run_id="run-2")
    assert enriched["discovery_evidence"]["Discovery_run_ID"] == "run-2"
    assert enriched.get("documents")
    assert enriched.get("raw_metadata")
    records = _records_from_live_result(
        {"run_id": "run-live", "handoff_records": [_raw_discovery_item()]},
        discovery_run_id="run-live",
    )
    assert len(records) == 1
    assert records[0]["discovery_evidence"]["Source_URL"].startswith("http")
    assert records[0].get("documents") or records[0]["discovery_evidence"].get("Document_references")


def test_pipeline_handoff_preserves_evidence(tmp_path):
    store = M3PipelineStore(path=tmp_path / "chain.json", durable=False)
    rec = enrich_discovery_record_for_pipeline(_raw_discovery_item(), discovery_run_id="run-3")
    row, created = store.upsert_from_discovery(rec)
    assert created is True
    assert row.get("discovery_evidence")
    assert row.get("detail_url")
    assert row.get("evidence_chain")
    assert row.get("evidence_handoff_validation", {}).get("Discovery_to_Pipeline") == "PASS"

    # Update without documents must not wipe prior docs/url
    thin = {
        "title": "Cisco Catalyst Switches",
        "solicitation_number": "IFB-99",
        "external_id": "IFB-99",
        "agency": "State DoIT",
        "source_id": "illinois_bidbuy",
        "detail_url": "https://example.test/opp/ifb-99-alt",
        "description": "Updated description",
    }
    row2, created2 = store.upsert_from_discovery(thin)
    assert created2 is False
    assert row2.get("detail_url") == "https://example.test/opp/ifb-99"  # original kept
    assert row2.get("documents") or row2.get("discovery_evidence", {}).get("Document_references")
    assert "Updated description" in str(row2.get("description") or "")


def test_merge_preserve_no_data_loss():
    existing = ensure_evidence_chain_on_row(
        enrich_discovery_record_for_pipeline(_raw_discovery_item(), discovery_run_id="r1")
    )
    incoming = {"title": "Cisco Catalyst Switches", "description": "x", "documents": []}
    merged = merge_preserve_evidence(existing, incoming)
    loss = detect_data_loss_across_modules(merged)
    assert loss["ok"] is True
    assert merged.get("detail_url")
    assert merged.get("discovery_evidence", {}).get("Document_references")


def test_evidence_status_and_chain_steps():
    row = ensure_evidence_chain_on_row(
        enrich_discovery_record_for_pipeline(_raw_discovery_item(), discovery_run_id="r4")
    )
    chain = build_evidence_chain(row)
    assert chain["kind"] == "EVIDENCE_CHAIN"
    assert len(chain["steps"]) >= 6
    assert chain["Evidence_status"] in {FOUND_AT_DISCOVERY, NOT_FOUND, "PROCESSED", "EXTRACTED"}
    v = validate_pipeline_handoff_preservation(row)
    assert "source_url" in v["checks"]


def test_analyze_evidence_chain_validation(tmp_path):
    store = M3PipelineStore(path=tmp_path / "v.json", durable=False)
    store.upsert_from_discovery(enrich_discovery_record_for_pipeline(_raw_discovery_item(), discovery_run_id="r5"))
    store.upsert_from_discovery(
        enrich_discovery_record_for_pipeline(
            _raw_discovery_item(
                title="No URL item",
                solicitation_number="X-1",
                external_id="X-1",
                preferred_source_url=None,
                detail_url=None,
                documents=[],
                attachment_links=[],
            ),
            discovery_run_id="r5",
        )
    )
    store.save()
    out = analyze_evidence_chain_top(store, limit=10)
    assert out["analyzed"] >= 1
    assert out["evidence_records_created"] >= 1
    assert out["source_urls_preserved"] >= 1
    assert out["va_readiness"]["evidence_visibility"] is True
    assert out["paid"] == 0

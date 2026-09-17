"""Document recovery + procurement evidence — inventory, extraction, queues, VA."""

from __future__ import annotations

from m3_document_evidence import (
    ATT_FOUND,
    COMPLETE_PURCHASE_PACKAGE,
    DOCUMENT_RECOVERY_REQUIRED,
    HEALTH_AUTH_REQUIRED,
    PARTIAL,
    Q_NEEDS_DOCUMENT_RECOVERY,
    Q_NEEDS_PRODUCT_IDENTITY,
    Q_NEEDS_VALUE,
    READY_FOR_PRICING,
    READY_FOR_SUPPLIER_RESEARCH,
    VAL_ESTIMATED,
    VAL_UNKNOWN,
    VAL_VERIFIED,
    analyze_document_evidence_top,
    apply_va_evidence_update,
    attachment_discovery_status,
    build_document_evidence_intelligence,
    build_document_evidence_profile,
    classify_document_type,
    commercial_readiness_score,
    deal_room_evidence_recovery_section,
    extract_product_evidence,
    extract_quantity_evidence,
    extract_value_evidence,
    procurement_completeness_score,
)
from m3_pipeline_store import M3PipelineStore


def _opp(**kwargs):
    base = {
        "canonical_id": kwargs.get("canonical_id", "ev-1"),
        "title": "Cisco Catalyst 9300 Switches",
        "description": "P/N C9300-48P quantity QTY: 12 EA estimated value $120000",
        "agency": "State of Illinois",
        "solicitation_number": "IFB-2026-0199",
        "estimated_value": 120000,
        "detail_url": "https://example.test/solicitations/ifb-2026-0199",
        "line_items": [
            {
                "description": "Cisco Catalyst 9300-48P",
                "part_number": "C9300-48P",
                "manufacturer": "Cisco",
                "quantity": 12,
                "unit": "EA",
                "unit_price": 8000,
            }
        ],
        "documents": [
            {
                "name": "Pricing Schedule.pdf",
                "url": "https://example.test/pricing-schedule.pdf",
                "source": "official_portal",
                "bytes_recovered": True,
                "extracted_text": "Unit price $8000 QTY: 12 estimated value $120000",
                "retrieved_at": "2026-09-17T12:00:00+00:00",
            },
            {
                "name": "Technical Specs.pdf",
                "url": "https://example.test/tech-specs.pdf",
                "source": "official_portal",
                "bytes_recovered": True,
                "document_type": "technical_specification",
            },
        ],
        "commercial_pricing": {"lowest_public_new_unit": 7500, "public_source": "https://ex.test/cisco"},
    }
    base.update(kwargs)
    return base


def test_document_discovery_and_classification():
    assert classify_document_type("Pricing Schedule.pdf", "https://x/pricing-schedule.pdf") == "pricing_schedule"
    assert classify_document_type("line_items.xlsx") == "spreadsheet"
    inv = build_document_evidence_profile(_opp())
    assert inv["documents_with_bytes"] >= 2
    types = {d["Document_type"] for d in inv["Available_documents"]}
    assert "pricing_schedule" in types or "bom" in types
    disc = attachment_discovery_status(_opp(), inv)
    assert disc["status"] == ATT_FOUND


def test_value_and_quantity_extraction():
    values = extract_value_evidence(_opp())
    assert values["confidence"] in {VAL_VERIFIED, VAL_ESTIMATED}
    assert values["primary_value"] == 120000 or values["primary_value"] == 96000
    qty = extract_quantity_evidence(_opp())
    assert qty["Quantity"] == 12 or qty["Quantity"] == 12.0
    assert qty["Confidence"] == VAL_VERIFIED


def test_product_extraction_rejects_portal_chrome():
    product = extract_product_evidence(_opp())
    assert product["Manufacturer"] == "Cisco"
    assert product["Part_number"] == "C9300-48P"
    bad = extract_product_evidence(
        _opp(
            title="Misc supplies",
            description="See https://sam.gov/opportunity/abc documents Contracts",
            line_items=[],
            estimated_value=None,
            documents=[],
            commercial_pricing=None,
        )
    )
    assert "http" not in str(bad.get("Part_number") or "").lower()
    assert str(bad.get("Part_number") or "UNKNOWN") in {"UNKNOWN", "UNKNOWN"}


def test_completeness_and_readiness_scoring():
    row = _opp()
    inv = build_document_evidence_profile(row)
    values = extract_value_evidence(row)
    qty = extract_quantity_evidence(row)
    product = extract_product_evidence(row)
    pkg = build_document_evidence_intelligence(row, run_recovery=False)
    comp = procurement_completeness_score(inv, values, qty, product, pkg.get("full") if False else None)
    # Rebuild with package path via full intelligence
    assert pkg["PROCUREMENT_COMPLETENESS"]["PROCUREMENT_COMPLETENESS"] in {
        COMPLETE_PURCHASE_PACKAGE,
        READY_FOR_PRICING,
        READY_FOR_SUPPLIER_RESEARCH,
        PARTIAL,
    }
    assert pkg["COMMERCIAL_READINESS"]["score"] >= 40
    assert "explanation" in pkg["COMMERCIAL_READINESS"]


def test_queue_creation_and_thin_docs():
    thin = build_document_evidence_intelligence(
        _opp(
            title="Vague stuff",
            description="n/a",
            line_items=[],
            estimated_value=None,
            documents=[],
            commercial_pricing=None,
        ),
        run_recovery=False,
    )
    assert thin["PROCUREMENT_COMPLETENESS"]["PROCUREMENT_COMPLETENESS"] in {
        DOCUMENT_RECOVERY_REQUIRED,
        "INSUFFICIENT_DATA",
        PARTIAL,
    }
    assert thin["RECOVERY_QUEUE"]["queue"] in {
        Q_NEEDS_DOCUMENT_RECOVERY,
        Q_NEEDS_PRODUCT_IDENTITY,
        Q_NEEDS_VALUE,
    }


def test_auth_blocked_skips_retry_hammering():
    row = _opp(
        source_access_state="AUTH_REQUIRED",
        evidence_failure={"primary_reason": "AUTH_REQUIRED", "reasons": ["AUTH_REQUIRED"]},
        evidence_recovery_attempts=[
            {"tier": "T1", "at": "2026-09-01T00:00:00+00:00", "failure": "AUTH_REQUIRED"},
            {"tier": "T2", "at": "2026-09-02T00:00:00+00:00", "failure": "AUTH_REQUIRED"},
        ],
        documents=[],
        line_items=[],
        estimated_value=None,
        commercial_pricing=None,
    )
    pkg = build_document_evidence_intelligence(row, run_recovery=True, allow_paid=False)
    assert pkg["SOURCE_HEALTH"]["status"] == HEALTH_AUTH_REQUIRED
    assert pkg["SOURCE_HEALTH"]["skip_further_automated_retry"] is True
    assert pkg["RECOVERY_RUN"]["executed"] is False


def test_va_update_and_forbidden_actions(tmp_path):
    store = M3PipelineStore(path=tmp_path / "ev.json", durable=False)
    store._rows["a"] = _opp(canonical_id="a")
    store.save()
    ok = apply_va_evidence_update(
        store,
        "a",
        action="ATTACH_EVIDENCE",
        note="Uploaded pricing schedule",
        attached_document={
            "name": "award.pdf",
            "document_type": "award_notice",
            "text": "Award amount $125000",
            "bytes_recovered": True,
        },
    )
    assert ok["ok"] is True
    bad = apply_va_evidence_update(store, "a", action="SUBMIT_BID")
    assert bad["ok"] is False
    assert bad["error"] == "VA_ACTION_NOT_PERMITTED"
    escalate = apply_va_evidence_update(store, "a", action="ESCALATE", note="Need portal login")
    assert escalate["ok"] is True


def test_economics_handoff_and_analyze(tmp_path):
    store = M3PipelineStore(path=tmp_path / "ev2.json", durable=False)
    store._rows["a"] = _opp(canonical_id="a")
    store._rows["b"] = _opp(
        canonical_id="b",
        title="Vague",
        description="n/a",
        line_items=[],
        estimated_value=None,
        documents=[],
        commercial_pricing=None,
    )
    store.save()
    out = analyze_document_evidence_top(store, limit=5, run_recovery=False, allow_paid=False)
    assert out["analyzed"] >= 1
    assert out["paid"] == 0
    assert "queues" in out
    assert "commercial_readiness" in out
    pkg = build_document_evidence_intelligence(store.get("a") or _opp(canonical_id="a"), run_recovery=False)
    assert "ECONOMICS_HANDOFF" in pkg
    sec = deal_room_evidence_recovery_section(store.get("a") or _opp(canonical_id="a"))
    assert sec["kind"] == "M3DealRoomEvidenceRecovery"
    assert sec["Next_Action"]

"""Targeted Federal/DLA product-intelligence tests — NOT full suite."""

from __future__ import annotations

from discovery.dla_product_extract import (
    compute_product_transaction_readiness,
    enrich_with_dla_structure,
    extract_dla_product_structure,
    extract_dla_research_signals,
    resolve_product_identity,
)
from discovery.federal_description_recovery import (
    apply_description_recovery,
    classify_description_field,
    recover_description,
)
from discovery.federal_package_refs import classify_reference_url, classify_technical_data_state, enumerate_package_references
from federal_dla_product_constants import (
    DESCRIPTION_EXTERNAL_POINTER,
    DESCRIPTION_INLINE,
    ID_EXACT_NSN,
    READY_COMMERCIAL_RESEARCH,
    REF_SAM_DESCRIPTION,
    TECH_DIBBS_REF,
)


def test_description_inline_and_url_classify():
    assert classify_description_field("Widget NSN 1234-01-234-5678")["kind"] == DESCRIPTION_INLINE
    u = classify_description_field("https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=abc")
    assert u["kind"] == DESCRIPTION_EXTERNAL_POINTER
    assert u["is_url"] is True


def test_description_recovery_inline_no_network():
    row = {"description": "NSN 1234-01-234-5678 BOLT Qty: 10 UOI: EA", "external_id": "n1"}
    rec = recover_description(row, authorize_live=False)
    assert rec["description_state"] == DESCRIPTION_INLINE
    assert rec["content_hash"]
    applied = apply_description_recovery(row, rec)
    assert applied["description_recovered"] is True
    assert "original_description" in (applied.get("raw_metadata") or {})


def test_description_url_without_live_stays_pointer():
    row = {
        "description": "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=abc",
        "document_links": [{"url": "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=abc", "kind": "sam_description"}],
        "external_id": "n2",
    }
    rec = recover_description(row, authorize_live=False)
    assert rec["description_state"] == DESCRIPTION_EXTERNAL_POINTER


def test_package_ref_classification_and_tech_state():
    assert classify_reference_url("https://api.sam.gov/x", kind_hint="sam_description") == REF_SAM_DESCRIPTION
    row = {
        "title": "SPE7M1 RFQ",
        "description": "See DIBBS for quote submission. TDMT technical data may apply.",
        "document_links": [{"url": "https://www.dibbs.bsm.dla.mil/RFQ/x", "kind": "portal"}],
    }
    refs = enumerate_package_references(row)
    assert refs
    tech = classify_technical_data_state(row, refs)
    assert tech["dibbs_reference"] is True
    assert tech["technical_data_state"] in {TECH_DIBBS_REF, "TDMT_REFERENCE_AVAILABLE", TECH_DIBBS_REF}


def test_nsn_clin_qty_fat_signals_and_identity():
    row = {
        "title": "NSN 1234-01-234-5678 CONNECTOR",
        "description": (
            "CLIN 0001 P/N ABC-99-XYZ Qty: 150 Unit of Issue: EA CAGE CODE 12345 "
            "Approved source required. FAT required. MIL-STD-2073 packaging. "
            "Inspection at destination. Submit quote via DIBBS."
        ),
    }
    e = enrich_with_dla_structure(row)
    s = e["dla_product_structure"]
    assert s["has_exact_nsn"] and s["has_exact_pn"] and s["has_quantity"] and s["has_uoi"]
    assert s["fat_required_signal"] and s["packaging_signal"] and s["inspection_signal"]
    assert s["clins"]
    ident = resolve_product_identity(s)
    assert ident["identity_state"] == ID_EXACT_NSN
    sigs = extract_dla_research_signals(e, s)
    assert "FAT_REQUIRED" in sigs
    assert "SUBMISSION_PATH_DIBBS" in sigs
    assert "PACKAGING_COMPLEXITY_SIGNAL" in sigs


def test_readiness_commercial_when_nsn_and_qty():
    row = enrich_with_dla_structure(
        {
            "title": "NSN 1234-01-234-5678 BOLT",
            "description": "Qty: 25 Unit of Issue: EA",
            "description_state": "DESCRIPTION_PUBLIC_RECOVERED",
            "notice_semantic_class": "BID_OR_QUOTE_READY",
        }
    )
    ready = compute_product_transaction_readiness(
        row,
        struct=row["dla_product_structure"],
        description_state="DESCRIPTION_PUBLIC_RECOVERED",
        refs_count=1,
        docs_recovered=0,
    )
    assert ready["knows_what"] is True
    assert ready["knows_how_many"] is True
    assert ready["commercial_research_ready"] is True
    assert ready["readiness_state"] == READY_COMMERCIAL_RESEARCH


def test_unknown_preserved_not_rejected():
    from discovery.dla_product_extract import classify_federal_product_cheap

    row = enrich_with_dla_structure({"title": "Miscellaneous requirement", "description": ""})
    screen = classify_federal_product_cheap(row)
    assert screen.get("reject_unknown") is not True


def test_idc_structure_and_guaranteed_minimum():
    row = {
        "title": "IDC for hardware",
        "description": "Indefinite Delivery Contract with guaranteed minimum quantity of 1.",
    }
    s = extract_dla_product_structure(row)
    assert s["structure_type"] in {"IDC", "SIDC"}
    assert s["guaranteed_minimum_signal"] is True


def test_enrichment_priority_and_campaign_offline():
    from discovery.federal_dla_enrichment import (
        enrichment_priority_score,
        run_federal_dla_enrichment_campaign,
    )

    hot = {
        "notice_id": "a1",
        "is_dla": True,
        "bid_quote_ready": True,
        "notice_semantic_class": "BID_OR_QUOTE_READY",
        "title": "NSN 1234-01-234-5678 WIDGET",
        "description": "Qty: 5 Unit of Issue: EA Approved source. CAGE CODE 1AAAA",
        "document_links": [],
        "federal_product_class": "FEDERAL_PRODUCT_LIKELY",
    }
    cold = {
        "notice_id": "b1",
        "notice_semantic_class": "AWARD_OR_HISTORY",
        "title": "Award Notice",
        "description": "Awarded",
    }
    assert enrichment_priority_score(hot) > enrichment_priority_score(cold)
    camp = run_federal_dla_enrichment_campaign(
        [hot, cold],
        authorize_live=False,
        limit=10,
        fetch_documents=False,
        prefer_dla=True,
    )
    assert camp["metrics"]["sample_size"] >= 1
    assert camp["metrics"]["exact_nsn"] >= 1
    assert camp["anti_bot_bypass"] == 0


def test_handoff_file_primary_durable_constants():
    from m3_pipeline_store import DURABLE_INLINE_MAX_BYTES, PIPELINE_DURABLE_POINTER_KEY

    assert DURABLE_INLINE_MAX_BYTES >= 1_000_000
    assert PIPELINE_DURABLE_POINTER_KEY


def test_pipeline_save_skip_remote_merge_signature():
    import inspect
    from m3_pipeline_store import M3PipelineStore

    sig = inspect.signature(M3PipelineStore.save)
    assert "skip_remote_merge" in sig.parameters


def test_amendment_change_detection():
    from discovery.federal_dla_enrichment import detect_amendment_changes

    changes = detect_amendment_changes(
        {"quantity": 10, "exact_nsn": "1234-01-234-5678", "description_content_hash": "abc"},
        {"quantity": 25, "exact_nsn": "1234-01-234-5678", "description_content_hash": "abc"},
    )
    assert any(c["field"] == "quantity" and c["invalidates"] == "economics" for c in changes)


def test_description_url_from_document_links_kind():
    from discovery.federal_description_recovery import recover_description

    row = {
        "notice_id": "abc123",
        "description": None,
        "document_links": [
            {
                "url": "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=abc123",
                "kind": "sam_description",
            }
        ],
    }
    rec = recover_description(row, authorize_live=False)
    assert rec["description_state"] == "DESCRIPTION_EXTERNAL_POINTER"
    assert "noticedesc" in (rec.get("source_url") or "")


def test_compact_nsn_qty_ui_from_sam_noticedesc_style():
    row = enrich_with_dla_structure(
        {
            "title": "43--PUMP,ROTARY",
            "description": (
                "Proposed procurement for NSN 4320012431951 PUMP,ROTARY:\n"
                "Line 0001 Qty 10     UI EA  Deliver To: W1A8 DLA"
            ),
            "description_state": "DESCRIPTION_PUBLIC_RECOVERED",
        }
    )
    s = row["dla_product_structure"]
    assert s["nsn"] == "4320-01-243-1951"
    assert s["quantity"] == 10
    assert s["unit_of_issue"] == "EA"
    assert s["clins"]
    ready = compute_product_transaction_readiness(
        row, struct=s, description_state="DESCRIPTION_PUBLIC_RECOVERED", refs_count=1
    )
    assert ready["commercial_research_ready"] is True


def test_build_version_bump():
    from app import APP_BUILD_VERSION

    # Superseded by product-resale discovery intelligence build; keep prior modules intact.
    assert APP_BUILD_VERSION == "20260922-m3-micro-lab-automation-1"


def test_sam_checkpoint_save_never_shrinks(tmp_path, monkeypatch):
    import json
    from discovery import federal_sam_ingest as fsi
    import database

    path = tmp_path / "m3_federal_sam_checkpoint.json"
    path.write_text(
        json.dumps(
            {
                "seen_notice_ids": ["a", "b", "c"],
                "seen_count": 3,
                "coverage_state": "FEDERAL_SAM_PUBLIC_COVERAGE_COMPLETE_TO_CHECKPOINT",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(fsi, "_checkpoint_file", lambda: path)
    monkeypatch.setattr(database, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("no db")))

    out = fsi.save_sam_checkpoint(
        {"seen_notice_ids": ["b"], "seen_count": 1, "coverage_state": "PARTIAL"}
    )
    assert out["seen_count"] >= 3
    assert set(out["seen_notice_ids"]) >= {"a", "b", "c"}
    assert "COMPLETE" in (out.get("coverage_state") or "")

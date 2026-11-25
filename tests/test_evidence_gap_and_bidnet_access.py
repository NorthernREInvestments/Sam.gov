"""Evidence-gap taxonomy and BidNet access-block policy (no network)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from m3.research.procurement_requirements import (
    assess_sourcing_readiness,
    classify_evidence_gap,
    detect_pricing_basis,
    extract_requirements,
    owner_blocker_codes,
)
from m3.research.staged_inventory import (
    _access_block_reason,
    apply_known_access_blocks,
    assign_owner_queue,
    evaluate_record,
    record_source_access_blocks,
)


NOW = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)


def test_pricing_basis_without_firm_quantity():
    title = "Annual Catalog Contract — Unit Pricing for Office Supplies as Needed"
    req = extract_requirements(title)
    basis = detect_pricing_basis(title, req)
    assert basis and basis["pricing_basis"] == "unit_or_catalog"
    src = assess_sourcing_readiness(title=title, requirements=req, eligibility_state="UNKNOWN")
    assert "quantity_not_stated" not in (src.get("sourcing_missing") or [])


def test_evidence_gap_distinguishes_extraction_from_never_acquired():
    req = {"line_items": [], "requirements_state": "NO_TEXT"}
    src = {"sourcing_state": "DOCUMENT_REQUIRED", "sourcing_missing": ["quantity_not_stated", "product_identity_too_vague_for_quote"]}
    never = classify_evidence_gap(description="", document_text="", requirements=req, sourcing=src)
    assert never["evidence_gap_kind"] == "NEVER_ACQUIRED"
    extracted = classify_evidence_gap(
        description="Buyer requests 50 EA of traffic cones meeting ASTM specs with delivery to depot.",
        document_text="", requirements=req, sourcing=src,
    )
    assert extracted["evidence_gap_kind"] == "EXTRACTION_INCOMPLETE"
    blocked = classify_evidence_gap(
        description="", document_text="", requirements=req, sourcing=src,
        access_block="host_access_blocked:bidnetdirect.com", retrieval_status="robots_disallow",
    )
    assert blocked["evidence_gap_kind"] == "SOURCE_ACCESS_BLOCKED"


def test_bidnet_access_block_recorded_once(tmp_path: Path):
    first = record_source_access_blocks(tmp_path)
    assert "bidnetdirect.com" in first["blocks"]
    assert first["blocks"]["bidnetdirect.com"]["do_not_retry"] is True
    recorded_at = first["blocks"]["bidnetdirect.com"]["recorded_at"]
    second = record_source_access_blocks(tmp_path)
    assert second["blocks"]["bidnetdirect.com"]["recorded_at"] == recorded_at


def test_bidnet_listing_not_archived_for_access_block():
    rec = {
        "title": "Purchase of Five (5) Marked Police Utility Vehicles",
        "description": "", "document_text": "",
        "source": "bidnet",
        "official_url": "https://www.bidnetdirect.com/texas/solicitations/open-bids/statewide/x/1",
        "lifecycle_status": "LIKELY_OPEN", "record_kind": "SOLICITATION",
        "deadline_at": "2026-12-15T18:00:00+00:00",
        "retrieval_status": "robots_disallow",
        "retrieval_detail": "robots.txt HTTP 403",
    }
    reason = _access_block_reason(rec) or ""
    assert "robots_disallow" in reason or "bidnetdirect.com" in reason
    ev = evaluate_record(rec, NOW)
    assert ev["evidence_gap"]["evidence_gap_kind"] in {"SOURCE_ACCESS_BLOCKED", "NEVER_ACQUIRED"}
    assert any(b["code"] == "SOURCE_ACCESS_BLOCKED" for b in ev["owner_blockers"])
    q, why = assign_owner_queue(rec, ev)
    assert q == "access_required"
    assert q != "archived"
    assert "robots" in why.lower() or "host_access_blocked" in why or "bidnet" in why.lower()


def test_bidnet_title_complete_stays_research_ready():
    """Listing title already states brand/model + qty — do not hide behind access_required."""
    rec = {
        "title": "Purchase of a New Interstate Trailer Model 14RBS (Or Equal)",
        "description": "", "document_text": "",
        "source": "bidnet",
        "official_url": "https://www.bidnetdirect.com/ohio/solicitations/open-bids/statewide/x/2",
        "lifecycle_status": "VERIFIED_OPEN", "record_kind": "SOLICITATION",
        "deadline_at": "2026-12-20T18:00:00+00:00",
        "retrieval_status": "robots_disallow",
        "retrieval_detail": "robots.txt HTTP 403",
    }
    ev = evaluate_record(rec, NOW)
    assert ev["sourcing"]["sourcing_state"] in {"SUPPLIER_RESEARCH_READY", "COMMERCIAL_RESEARCH_READY"}
    q, _ = assign_owner_queue(rec, ev)
    assert q in {"supplier_research_ready", "commercial_research_ready"}
    assert q != "archived"


def test_apply_known_blocks_stamps_without_overwrite():
    blocks = {"blocks": {"bidnetdirect.com": {
        "host": "bidnetdirect.com", "status": "robots_disallow",
        "detail": "robots.txt HTTP 403", "do_not_retry": True,
    }}}
    rows = [
        {"source": "bidnet", "official_url": "https://www.bidnetdirect.com/x", "title": "Salt"},
        {"source": "bidnet", "official_url": "https://www.bidnetdirect.com/y", "title": "Trucks",
         "retrieval_status": "page_only", "retrieval_detail": "kept"},
    ]
    stats = apply_known_access_blocks(rows, blocks)
    assert stats["stamped"] == 1
    assert rows[0]["retrieval_status"] == "robots_disallow"
    assert rows[1]["retrieval_status"] == "page_only"


def test_owner_blockers_include_extraction_code():
    presence = {"product_presence": "EXPLICIT_PRODUCT", "evidence_completeness": "SUMMARY"}
    req = extract_requirements("Office chairs", description="The City will purchase office chairs. Specs TBD.")
    src = assess_sourcing_readiness(title="Office chairs", requirements=req, eligibility_state="UNKNOWN")
    gap = classify_evidence_gap(
        description="The City will purchase office chairs. Specs TBD.", document_text="",
        requirements=req, sourcing=src,
    )
    blocks = owner_blocker_codes(
        title="Office chairs", requirements=req, sourcing=src, presence=presence,
        eligibility={"eligibility_state": "UNKNOWN"}, evidence_gap=gap, deadline_at="2026-12-01T00:00:00+00:00",
    )
    codes = {b["code"] for b in blocks}
    assert "EXTRACTION_INCOMPLETE" in codes or "MISSING_PRODUCT_IDENTITY" in codes

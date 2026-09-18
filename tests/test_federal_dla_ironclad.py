"""Targeted Federal / DLA iron-clad unit tests — NOT full suite."""

from __future__ import annotations

import inspect
from pathlib import Path

from discovery.dla_product_extract import (
    classify_federal_product_cheap,
    enrich_with_dla_structure,
    extract_dla_product_structure,
)
from discovery.dla_reconciliation import (
    build_dla_from_sam,
    build_dla_source_reconciliation,
    classify_dla_relationship,
    run_dla_coverage_sample,
)
from discovery.dla_source_map import classify_dibbs_access_from_metrics
from discovery.federal_sam_ingest import (
    classify_federal_agency_bucket,
    classify_sam_notice_type,
    is_dla_organization,
    normalize_sam_opportunity,
    reconcile_sam_page_counts,
)
from federal_dla_constants import (
    DIBBS_BOT_BLOCKED_AUTOMATION,
    NOTICE_AWARD_OR_HISTORY,
    NOTICE_BID_OR_QUOTE_READY,
    REL_SAM_AND_DIBBS,
    REL_SAM_ONLY,
)


def test_sam_notice_type_bid_ready():
    n = classify_sam_notice_type({"type": "o", "title": "Solicitation for widgets"})
    assert n["notice_semantic_class"] == NOTICE_BID_OR_QUOTE_READY
    assert n["bid_quote_ready"] is True


def test_sam_notice_type_award_not_bid_ready():
    n = classify_sam_notice_type({"type": "a", "title": "Award Notice"})
    assert n["notice_semantic_class"] == NOTICE_AWARD_OR_HISTORY
    assert n["bid_quote_ready"] is False


def test_federal_org_dla_and_agency_buckets():
    assert classify_federal_agency_bucket("DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.DLA LAND") == "DLA"
    assert classify_federal_agency_bucket("DEPT OF THE ARMY.AMC") == "ARMY"
    assert is_dla_organization(
        {
            "fullParentPathName": "DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY",
            "solicitationNumber": "SPE7M1-26-T-0001",
            "title": "BOLT",
        }
    )


def test_normalize_sam_no_set_aside_filter():
    raw = {
        "noticeId": "abc123",
        "title": "NSN 1234-01-234-5678 WIDGET",
        "solicitationNumber": "SPE7M1-26-T-0001",
        "type": "o",
        "active": "Yes",
        "fullParentPathName": "DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.DLA LAND AND MARITIME",
        "naicsCode": "332722",
        "classificationCode": "53",
        "uiLink": "https://sam.gov/opp/abc123/view",
    }
    row = normalize_sam_opportunity(raw)
    assert row["source_id"] == "fed_sam_contract_opportunities"
    assert row["is_dla"] is True
    assert row["bid_quote_ready"] is True
    assert row["external_id"] == "abc123"


def test_nsn_pn_qty_extraction():
    row = {
        "title": "NSN 1234-01-234-5678 CONNECTOR",
        "description": "P/N ABC-99-XYZ Qty: 150 Unit of Issue: EA Approved source required. CAGE CODE 12345",
    }
    s = extract_dla_product_structure(row)
    assert s["has_exact_nsn"]
    assert s["nsn"] == "1234-01-234-5678"
    assert s["has_exact_pn"]
    assert s["has_quantity"]
    assert s["quantity"] == 150
    assert s["approved_source_signal"] is True


def test_federal_cheap_screen_preserves_unknown():
    row = enrich_with_dla_structure({"title": "Miscellaneous requirement", "description": ""})
    screen = classify_federal_product_cheap(row)
    assert screen["federal_product_class"] in {
        "FEDERAL_UNKNOWN",
        "FEDERAL_SERVICE",
        "FEDERAL_MIXED",
        "FEDERAL_PRODUCT_LIKELY",
        "FEDERAL_CONSTRUCTION",
        "FEDERAL_PRODUCT_PLUS_MINOR_SERVICE",
    }
    # UNKNOWN must not be rejected solely for sparse evidence
    assert screen.get("reject_unknown") is not True


def test_dla_reconciliation_relationships():
    assert classify_dla_relationship(in_sam=True, in_dibbs=False) == REL_SAM_ONLY
    assert classify_dla_relationship(in_sam=True, in_dibbs=True) == REL_SAM_AND_DIBBS
    sam = [
        {
            "solicitation_number": "SPE7M1-26-T-0001",
            "is_dla": True,
            "title": "NSN 1234-01-234-5678 BOLT",
            "notice_semantic_class": "BID_OR_QUOTE_READY",
            "bid_quote_ready": True,
        }
    ]
    pack = build_dla_from_sam(sam)
    assert pack["current_unique"] == 1
    assert pack["exact_nsn"] >= 1
    recon = build_dla_source_reconciliation(sam_dla=pack["opportunities"], dibbs_rows=[])
    assert recon["sam_only"] == 1


def test_dla_sample_no_extrapolation_flag():
    sample = run_dla_coverage_sample(
        sample_ids=["SPE7M1-26-T-0001", "SPRPA1-26-Q-0002"],
        sam_dla=[{"solicitation_number": "SPE7M1-26-T-0001"}],
        m3_rows=[{"solicitation_number": "SPE7M1-26-T-0001"}],
    )
    assert sample["extrapolation_forbidden"] is True
    assert sample["found_in_sam"] == 1
    assert sample["sample_size"] == 2


def test_dibbs_bot_classification():
    st = classify_dibbs_access_from_metrics(
        {"ok": False, "raw": 0, "source_stop_reason": "BOT_PROTECTED"}
    )
    assert st == DIBBS_BOT_BLOCKED_AUTOMATION


def test_sam_count_reconciliation_explains_window_overlap():
    windows = [
        {"authoritative_total": 100, "unique_captured_window": 100},
        {"authoritative_total": 80, "unique_captured_window": 50},
    ]
    r = reconcile_sam_page_counts(windows, m3_unique=140)
    assert r["AUTHORITATIVE_COUNT"] == 180
    assert r["M3_COUNT"] == 140
    assert "DIFFERENCE_REASON" in r


def test_handoff_large_batch_constants():
    from m3_pipeline_handoff import CHECKPOINT_EVERY_LARGE, LARGE_BATCH_THRESHOLD

    assert LARGE_BATCH_THRESHOLD == 500
    assert CHECKPOINT_EVERY_LARGE >= 200


def test_federal_sam_source_registered():
    from discovery.agency_seeds import FEDERAL_NON_SAM_LIVE

    ids = {f["source_id"] for f in FEDERAL_NON_SAM_LIVE}
    assert "fed_sam_contract_opportunities" in ids


def test_scheduler_twice_daily_denver_not_hourly():
    from scheduler import configure_m3_discovery_job

    src = inspect.getsource(configure_m3_discovery_job)
    assert "hour=6" in src or "06:00" in src
    assert "hour=14" in src or "14:00" in src
    sched_src = Path(__file__).resolve().parents[1].joinpath("scheduler.py").read_text(encoding="utf-8")
    assert "America/Denver" in sched_src
    assert "hourly disabled" in sched_src.lower() or "not hourly" in sched_src.lower()

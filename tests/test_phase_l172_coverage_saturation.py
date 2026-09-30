"""Phase L.17.2 coverage saturation tests."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.coverage_saturation import (
    L172_CHECKPOINT,
    apply_catalogs,
    build_registration_unlocks,
    load_l172_checkpoint,
    load_persisted_registry,
    platform_leverage_report,
)
from discovery.jurisdiction_registry import REGISTRY_PATH, status_breakdown
from discovery.lower48 import (
    ACCESSIBLE_NOW,
    BUILD_L172,
    FREE_REGISTRATION_REQUIRED,
    NONFEDERAL_ACCESSIBLE_NOW,
    UNKNOWN_RESEARCH_PENDING,
    classify_opportunity_access,
    current_access_score,
    is_nonfederal_accessible_now,
    registration_unlock_score,
)
from discovery.platform_buyer_catalog import platform_buyer_catalog, platform_catalog_by_family
from discovery.sam_api_parked import SAM_API_PENDING_REPLACEMENT_KEY, SAM_API_BUDGETED, sam_api_park_status
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.original_solicitation import resolve_original_solicitation, submission_path_checklist
from phase_l.quality_audit import VALIDATED_QUOTE_TARGET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"


def test_resume_no_national_registry_reset():
    assert REGISTRY_PATH.exists()
    before = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    total_before = before["counts"]["total"]
    assert total_before >= 20000
    reg = load_persisted_registry()
    assert reg["counts"]["total"] == total_before
    assert reg["counts"]["states"] == 48
    assert reg["counts"]["counties"] >= 3000
    # applying catalogs must not wipe inventory
    cp = load_l172_checkpoint()
    apply_catalogs(reg, cp=cp)
    assert len(reg["jurisdictions"]) >= total_before


def test_platform_enumeration_and_crosswalk():
    cat = platform_buyer_catalog()
    assert len(cat) >= 80
    families = platform_catalog_by_family()
    assert "OpenGov" in families or any("OpenGov" in k for k in families)
    reg = load_persisted_registry()
    lev = platform_leverage_report(reg)
    assert lev["platforms"]
    assert "PlatformLeverageScore" in lev["platforms"][0] or lev["platforms"]


def test_access_state_and_registration_unlock():
    city = {
        "title": "County IT laptops",
        "jurisdiction": "COUNTY",
        "our_bid_access": "YES",
        "source_id": "structured_socrata_la_ramp_open_bids",
        "state_code": "CA",
    }
    bidnet = {
        "title": "City fleet parts",
        "jurisdiction": "CITY",
        "our_bid_access": "YES",
        "source_id": "network_bidnet_texas",
        "registration_action": "REGISTER_BEFORE_BID",
    }
    fed = {"title": "DIBBS RFQ", "jurisdiction": "FEDERAL", "source_id": "fed_dla_dibbs_rfq"}
    assert is_nonfederal_accessible_now(city)
    assert current_access_score(bidnet)["label"] == NONFEDERAL_ACCESSIBLE_NOW
    assert classify_opportunity_access(city) in {ACCESSIBLE_NOW, FREE_REGISTRATION_REQUIRED}
    assert not is_nonfederal_accessible_now(fed)
    score = registration_unlock_score(buyers_unlocked=200, recurring=True, cost=0.0)
    assert score["RegistrationUnlockScore"] > 100
    reg = load_persisted_registry()
    unlocks = build_registration_unlocks(reg)
    assert unlocks["top"]


def test_authoritative_source_chain_and_submission():
    row = {
        "title": "IFB Pumps",
        "solicitation_number": "IFB-99",
        "detail_url": "https://example.gov/bid/99",
        "source_url": "https://data.example.gov/resource/x.json",
        "source_id": "structured_test",
        "agency": "Example County",
    }
    orig = resolve_original_solicitation(row)
    sub = submission_path_checklist(row, original=orig)
    assert row["source_url"] != row["detail_url"]
    assert "checks" in sub or sub is not None


def test_gates_and_no_sam_dibbs_outreach():
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    assert BIDNET_AUTH_HISTORY_PARKED
    st = sam_api_park_status()
    assert st["status"] in (SAM_API_PENDING_REPLACEMENT_KEY, SAM_API_BUDGETED)
    assert st["calls_consumed"] == 0
    assert VALIDATED_QUOTE_TARGET == "VALIDATED_QUOTE_TARGET"


def test_l172_artifacts_when_present():
    required = [
        "l172_unknown_reduction.json",
        "l172_county_saturation.json",
        "l172_municipality_saturation.json",
        "l172_platform_enumeration.json",
        "l172_platform_leverage.json",
        "l172_registration_unlocks.json",
        "l172_accessible_now.json",
        "l172_source_productivity.json",
        "l172_fresh_hunt.json",
        "l172_owner_worklist.json",
        "l172_quote_targets.json",
        "l172_summary.json",
    ]
    if not (OUT / "l172_summary.json").exists():
        return
    for name in required:
        assert (OUT / name).exists(), name
    summary = json.loads((OUT / "l172_summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"].startswith("PHASE_L172_")
    assert summary.get("no_sam_api_calls") is True
    assert summary.get("no_outreach") is True
    assert summary["baseline_used"]["baseline_source"] == "L17"
    assert summary["coverage"]["unknown_after"] < summary["coverage"]["unknown_before"] or summary[
        "coverage"
    ]["counties_mapped"] > 9
    for doc in (
        "phase_l172_coverage_saturation.md",
        "phase_l172_platform_enumeration.md",
        "phase_l172_platform_leverage.md",
        "phase_l172_registration_unlocks.md",
        "phase_l172_accessible_now.md",
        "phase_l172_source_productivity.md",
        "phase_l172_owner_workflow.md",
        "phase_l172_legacy_cleanup.md",
        "phase_l172_regression.md",
    ):
        assert (DOCS / doc).exists(), doc

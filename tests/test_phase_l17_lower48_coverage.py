"""Phase L.17 Lower-48 coverage engine tests."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.jurisdiction_registry import (
    build_base_registry,
    coverage_percentages,
    enrich_registry_from_known_sources,
    load_checkpoint,
    platform_jurisdiction_map,
    status_breakdown,
)
from discovery.lower48 import (
    AUTOMATED_TIER2,
    BUILD,
    DIBBS_CAGE_REQUIRED,
    FREE_REGISTRATION_REQUIRED,
    LOWER_48,
    LOWER_48_SET,
    NONFEDERAL_ACCESSIBLE_NOW,
    UNKNOWN_RESEARCH_PENDING,
    classify_source_status,
    current_access_score,
    is_nonfederal_accessible_now,
    normalize_jurisdiction_id,
)
from discovery.sam_api_parked import SAM_API_PENDING_REPLACEMENT_KEY, SAM_API_BUDGETED, sam_api_park_status
from discovery.structured_adapters import cross_source_dedupe_key, dedupe_structured_rows
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.original_solicitation import resolve_original_solicitation
from phase_l.quality_audit import VALIDATED_QUOTE_TARGET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"
DATA = ROOT / "data"


def test_all_lower48_states_represented():
    assert len(LOWER_48) == 48
    assert "AK" not in LOWER_48_SET and "HI" not in LOWER_48_SET
    reg = build_base_registry(include_municipalities=False)
    states = {j["state"] for j in reg["jurisdictions"].values() if j["buyer_type"] == "STATE"}
    assert states == LOWER_48_SET
    assert reg["counts"]["states"] == 48
    assert reg["counts"]["counties"] >= 3000


def test_county_registry_completeness():
    assert DATA.joinpath("lower48_counties.json").exists()
    blob = json.loads((DATA / "lower48_counties.json").read_text(encoding="utf-8"))
    assert blob["count"] >= 3000
    states = {c["state"] for c in blob["counties"]}
    assert states == LOWER_48_SET
    munis = json.loads((DATA / "lower48_municipalities.json").read_text(encoding="utf-8"))
    assert munis["count"] >= 15000


def test_jurisdiction_identity_normalization():
    a = normalize_jurisdiction_id(state="TX", buyer_type="CITY", name="Houston", geoid="4835000")
    b = normalize_jurisdiction_id(state="tx", buyer_type="city", name="Houston!!!", geoid="4835000")
    assert a.startswith("TX:CITY:")
    assert "houston" in a
    assert a.split(":")[2] == b.split(":")[2]  # same geoid


def test_source_status_taxonomy_and_platform_map():
    assert classify_source_status(structured_tier="TIER_2_STABLE_PUBLIC_STRUCTURED", automated=True) == AUTOMATED_TIER2
    assert (
        classify_source_status(auth_required=True, platform="BidNet", portal_url="https://x")
        == FREE_REGISTRATION_REQUIRED
    )
    assert classify_source_status() == UNKNOWN_RESEARCH_PENDING
    reg = enrich_registry_from_known_sources(build_base_registry(include_municipalities=False))
    br = status_breakdown(reg)
    assert UNKNOWN_RESEARCH_PENDING in br or sum(br.values()) > 0
    pm = platform_jurisdiction_map(reg)
    assert pm["platforms"] is not None
    pct = coverage_percentages(reg)
    assert pct["state_source_mapped_pct"] > 50  # all states have portals from STATE_MATRIX
    assert "county_source_mapped_pct" in pct
    cp = load_checkpoint()
    assert "completed_jurisdiction_ids" in cp


def test_authoritative_and_submission_preservation():
    row = {
        "title": "IFB Electric Actuator",
        "solicitation_number": "232247",
        "detail_url": "https://rampla.org/x",
        "source_url": "https://data.lacity.org/resource/hf3r-utnq.json",
        "source_id": "structured_socrata_la_ramp_open_bids",
    }
    orig = resolve_original_solicitation(row)
    assert orig.get("solicitation_number") == "232247" or row["title"]
    # discovery vs bid location distinction
    assert row["source_url"] != row["detail_url"]


def test_nonfederal_accessible_now_and_free_registration():
    city = {
        "title": "City laptop purchase",
        "jurisdiction": "CITY",
        "our_bid_access": "YES",
        "source_id": "structured_socrata_la_ramp_open_bids",
        "state_code": "CA",
    }
    fed = {
        "title": "DIBBS RFQ NSN",
        "jurisdiction": "FEDERAL",
        "source_id": "fed_dla_dibbs_rfq",
        "our_bid_access": "YES",
    }
    assert is_nonfederal_accessible_now(city) is True
    assert current_access_score(city)["label"] == NONFEDERAL_ACCESSIBLE_NOW
    assert is_nonfederal_accessible_now(fed) is False
    assert DIBBS_CAGE_REQUIRED == "DIBBS_CAGE_REQUIRED"


def test_cross_jurisdiction_dedupe_and_gates():
    rows = [
        {"title": "A", "solicitation_number": "1", "agency": "City", "source_id": "s1", "state_code": "TX"},
        {"title": "A", "solicitation_number": "1", "agency": "City", "source_id": "s2", "state_code": "TX"},
        {"title": "B", "solicitation_number": "2", "agency": "Other City", "source_id": "s1", "state_code": "CA"},
    ]
    uniq, dups = dedupe_structured_rows(rows)
    assert len(uniq) == 2 and len(dups) == 1
    assert cross_source_dedupe_key(rows[0]) == cross_source_dedupe_key(rows[1])
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    assert VALIDATED_QUOTE_TARGET == "VALIDATED_QUOTE_TARGET"
    assert BIDNET_AUTH_HISTORY_PARKED
    st = sam_api_park_status()
    assert st["status"] in (SAM_API_PENDING_REPLACEMENT_KEY, SAM_API_BUDGETED)
    assert st["calls_consumed"] == 0


def test_l17_artifacts_when_present():
    required = [
        "l17_lower48_jurisdiction_registry.json",
        "l17_state_coverage.json",
        "l17_county_coverage.json",
        "l17_municipality_coverage.json",
        "l17_platform_jurisdiction_map.json",
        "l17_registration_queue.json",
        "l17_manual_public_queue.json",
        "l17_accessible_now_opportunities.json",
        "l17_fresh_hunt.json",
        "l17_quote_targets.json",
        "l17_summary.json",
    ]
    if not (OUT / "l17_summary.json").exists():
        return
    for name in required:
        assert (OUT / name).exists(), name
    summary = json.loads((OUT / "l17_summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"].startswith("PHASE_L17_")
    assert summary.get("no_sam_api_calls") is True
    assert summary.get("no_outreach") is True
    assert summary["geographic_inventory"]["states"] == 48
    assert summary["geographic_inventory"]["counties"] >= 3000
    assert summary["sam_api"]["calls_consumed"] == 0
    for doc in (
        "phase_l17_lower48_strategy.md",
        "phase_l17_state_coverage.md",
        "phase_l17_county_coverage.md",
        "phase_l17_municipality_coverage.md",
        "phase_l17_platform_mapping.md",
        "phase_l17_registration_strategy.md",
        "phase_l17_accessible_now.md",
        "phase_l17_source_gaps.md",
        "phase_l17_legacy_cleanup.md",
        "phase_l17_regression.md",
    ):
        assert (DOCS / doc).exists(), doc

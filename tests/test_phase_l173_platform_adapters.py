"""Phase L.17.3 platform adapter expansion tests."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.platform_adapters import (
    ADAPTERS,
    ANTI_BOT,
    BUILD,
    EXACT,
    INGESTION_ACTIVE,
    OPENGOV_AGENCY_MIRRORS,
    OpenGovPlatformAdapter,
    PUBLIC_LISTING_AUTOMATABLE,
    STRONG,
    classify_fetch_result,
    crosswalk_confidence,
    extract_portal_slug,
    platform_inventory,
)
from discovery.sam_api_parked import SAM_API_PENDING_REPLACEMENT_KEY, SAM_API_BUDGETED, sam_api_park_status
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED
from phase_l.l173_platform_expansion import diagnose_ready_to_research
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.original_solicitation import resolve_original_solicitation, submission_path_checklist
from phase_l.quality_audit import VALIDATED_QUOTE_TARGET
from discovery.lower48 import (
    NONFEDERAL_ACCESSIBLE_NOW,
    is_nonfederal_accessible_now,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"


def test_generic_adapter_config_and_slug():
    assert "OpenGov" in ADAPTERS
    assert extract_portal_slug("https://procurement.opengov.com/portal/raleigh") == "raleigh"
    assert extract_portal_slug("https://www.boston.gov/bid-listings") is None
    inv = platform_inventory()
    assert inv["adapters_implemented"]
    assert any(p["platform"] == "OpenGov" for p in inv["platforms"])


def test_opengov_cdn_vs_mirror_and_access_states():
    adapter = OpenGovPlatformAdapter()
    cdn = {"jurisdiction_id": "X", "bid_portal": "https://procurement.opengov.com/portal/raleigh"}
    assert "opengov.com" in (adapter.resolve_list_url(cdn) or "")
    mirror_id = next(iter(OPENGOV_AGENCY_MIRRORS))
    mirrored = {"jurisdiction_id": mirror_id, "bid_portal": "https://procurement.opengov.com/portal/x"}
    assert "opengov.com" not in (adapter.resolve_list_url(mirrored) or "")
    access, act, docs = classify_fetch_result(
        status=403, body="Just a moment... cloudflare", n_opps=0, platform="OpenGov", list_url="x"
    )
    assert access == ANTI_BOT
    access2, act2, _ = classify_fetch_result(
        status=200, body="<html>bids</html>", n_opps=5, platform="OpenGov", list_url="x"
    )
    assert access2 == PUBLIC_LISTING_AUTOMATABLE
    assert act2 == INGESTION_ACTIVE


def test_crosswalk_confidence():
    assert crosswalk_confidence("Raleigh", "Raleigh") == EXACT
    assert crosswalk_confidence("Wake County", "Wake") == STRONG
    assert crosswalk_confidence("Alpha", "Beta") in {"NO_MATCH", "AMBIGUOUS"}


def test_accessible_now_and_ready_research_diagnosis():
    row = {
        "title": "City networking switches",
        "jurisdiction": "CITY",
        "our_bid_access": "YES",
        "source_id": "platform_opengov_test",
        "state_code": "AZ",
    }
    assert is_nonfederal_accessible_now(row)
    diag = diagnose_ready_to_research(
        [{"title": "x"}],
        {"READY_FOR_OWNER_APPROVAL": 1, "NEEDS_MINOR_REVIEW": 0},
    )
    assert "REGISTER_TO_UNLOCK" in diag["root_cause"] or "free-registration" in diag["root_cause"]
    assert diag["standards_unchanged"] is True


def test_authoritative_chain_and_gates():
    row = {
        "title": "IFB Motors",
        "solicitation_number": "IFB-1",
        "detail_url": "https://example.gov/bid/1",
        "source_url": "https://discovery.example/list",
        "source_id": "platform_opengov_x",
    }
    orig = resolve_original_solicitation(row)
    sub = submission_path_checklist(row, original=orig)
    assert row["source_url"] != row["detail_url"]
    assert sub is not None
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    assert BIDNET_AUTH_HISTORY_PARKED
    st = sam_api_park_status()
    assert st["status"] in (SAM_API_PENDING_REPLACEMENT_KEY, SAM_API_BUDGETED)
    assert st["calls_consumed"] == 0
    assert VALIDATED_QUOTE_TARGET == "VALIDATED_QUOTE_TARGET"
    assert BUILD.startswith("20260928-m3-phase-l173")


def test_l173_artifacts_when_present():
    required = [
        "l173_platform_adapter_inventory.json",
        "l173_opengov_results.json",
        "l173_bonfire_results.json",
        "l173_planetbids_results.json",
        "l173_ionwave_results.json",
        "l173_simplehtml_results.json",
        "l173_platform_productivity.json",
        "l173_registration_unlocks.json",
        "l173_accessible_now.json",
        "l173_ready_to_research.json",
        "l173_fresh_hunt.json",
        "l173_quote_targets.json",
        "l173_summary.json",
    ]
    if not (OUT / "l173_summary.json").exists():
        return
    for name in required:
        assert (OUT / name).exists(), name
    summary = json.loads((OUT / "l173_summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"].startswith("PHASE_L173_")
    assert summary.get("no_sam_api_calls") is True
    assert summary["baseline_used"]["baseline_source"] == "L172"
    ready = json.loads((OUT / "l173_ready_to_research.json").read_text(encoding="utf-8"))
    assert "diagnosis" in ready
    # READY_TO_RESEARCH should be populated when accessible-now exists
    an = json.loads((OUT / "l173_accessible_now.json").read_text(encoding="utf-8"))
    if an.get("count", 0) > 0:
        assert ready.get("count", 0) > 0 or summary["after"].get("READY_FOR_OWNER_APPROVAL", 0) > 0
    for doc in (
        "phase_l173_platform_adapter_strategy.md",
        "phase_l173_opengov.md",
        "phase_l173_bonfire.md",
        "phase_l173_planetbids.md",
        "phase_l173_ionwave.md",
        "phase_l173_simplehtml.md",
        "phase_l173_registration_unlocks.md",
        "phase_l173_accessible_now.md",
        "phase_l173_owner_queues.md",
        "phase_l173_legacy_cleanup.md",
        "phase_l173_regression.md",
    ):
        assert (DOCS / doc).exists(), doc

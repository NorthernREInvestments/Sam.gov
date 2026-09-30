"""Phase L.18 research queue conversion + OpenGov mirror tests."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.opengov_agency_mirrors import (
    OPENGOV_CDN_ANTI_BOT,
    propose_mirrors_for_jurisdiction,
)
from discovery.platform_adapters import crosswalk_confidence
from discovery.sam_api_parked import SAM_API_PENDING_REPLACEMENT_KEY, SAM_API_BUDGETED, sam_api_park_status
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED
from phase_l.l18_research_conversion import (
    BUILD,
    EXPIRED,
    NEEDS_SPEC_RESOLUTION,
    PURSUE_QUOTE_NOW,
    QUANTITY_EXACT,
    QUANTITY_UNRESOLVED,
    REGISTER_AND_PURSUE,
    RESEARCH_COMPLETE_WAITING_QUOTE,
    SKIP_PRODUCT,
    classify_owner_decision,
    classify_quantity_state,
    deadline_runway,
    is_likely_services,
    load_research_queue_input,
    match_inventory_row,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.original_solicitation import resolve_original_solicitation, submission_path_checklist
from phase_l.quality_audit import VALIDATED_QUOTE_TARGET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"


def test_research_queue_input_loadable():
    q = load_research_queue_input()
    assert len(q) == 51


def test_match_inventory_and_expired_stop():
    rows = [
        {"title": "City laptops", "solicitation_number": "IFB-1", "source_id": "structured_x"},
        {"title": "Other", "solicitation_number": "IFB-2", "source_id": "structured_y"},
    ]
    packet = {"title": "City laptops", "solicitation": "IFB-1", "source_id": "unknown"}
    assert match_inventory_row(packet, rows)["solicitation_number"] == "IFB-1"
    runway = deadline_runway({"response_deadline": "2020-01-01T00:00:00"}, {"stage0": {}})
    assert runway["label"] == "EXECUTION_FAIL"
    assert runway["days"] is not None and runway["days"] < 0


def test_quantity_identity_owner_states():
    from phase_l.l18_research_conversion import (
        EXACT_PRODUCT,
        STRONG_PRODUCT_IDENTITY,
        classify_product_identity,
        profit_tiers,
    )

    assert classify_quantity_state({"quantity": 10, "quality": "EXACT"}) == QUANTITY_EXACT
    assert classify_quantity_state({}) == QUANTITY_UNRESOLVED
    assert classify_quantity_state({}, {"title": "One (1) 4x4 Utility Vehicle"}) == QUANTITY_EXACT
    assert classify_product_identity({}, {"title": "Ricoh ScanSnap SV600 Scanner"}, {}) == EXACT_PRODUCT
    assert (
        classify_product_identity({}, {"title": "ASUS Chromebox desktop computers"}, {})
        == STRONG_PRODUCT_IDENTITY
    )
    assert is_likely_services({"title": "Consulting services for ERP"})
    assert is_likely_services({"title": "Operation Services in Connection with Vehicle Parking"})
    assert not is_likely_services({"title": "Dell laptop computers qty 50"})
    tiers = profit_tiers({"max_profit": 12000}, {})
    assert tiers["positive"] and tiers["gte_5k"] and tiers["gte_10k"] and not tiers["gte_25k"]
    d, reason = classify_owner_decision(
        expired=False,
        services=True,
        access_blocked=False,
        qty_state=QUANTITY_UNRESOLVED,
        identity="IDENTITY_UNRESOLVED",
        gov_grade="GOV_VALUE_UNKNOWN",
        suppliers=[],
        qstate="ECONOMIC_CASE_TOO_WEAK",
        needs_reg=False,
        econ={},
        runway={"label": "STRONG_RUNWAY", "days": 10},
        recurring=False,
    )
    assert d == SKIP_PRODUCT
    d2, _ = classify_owner_decision(
        expired=False,
        services=False,
        access_blocked=False,
        qty_state=QUANTITY_EXACT,
        identity="STRONG_PRODUCT_IDENTITY",
        gov_grade="GOV_VALUE_B",
        suppliers=[{"grade": "B"}],
        qstate=VALIDATED_QUOTE_TARGET,
        needs_reg=True,
        econ={},
        runway={"label": "STRONG_RUNWAY", "days": 10},
        recurring=False,
    )
    assert d2 == REGISTER_AND_PURSUE
    assert PURSUE_QUOTE_NOW and RESEARCH_COMPLETE_WAITING_QUOTE and NEEDS_SPEC_RESOLUTION


def test_opengov_mirror_crosswalk_no_cdn():
    conf = crosswalk_confidence("Raleigh", "City of Raleigh")
    assert conf in {"EXACT", "STRONG", "AMBIGUOUS"}
    j = {
        "jurisdiction_id": "NC:CITY:3755000:raleigh_city",
        "name": "Raleigh city",
        "state": "NC",
        "buyer_type": "CITY",
        "bid_portal": "https://procurement.opengov.com/portal/raleigh",
    }
    props = propose_mirrors_for_jurisdiction(j)
    assert props
    assert all("opengov.com" not in p["mirror_url"].lower() for p in props)
    assert OPENGOV_CDN_ANTI_BOT == "OPENGOV_CDN_ANTI_BOT"
    # Known Phoenix mirror is EXACT and never CDN
    from discovery.platform_adapters import OPENGOV_AGENCY_MIRRORS

    assert "opengov.com" not in OPENGOV_AGENCY_MIRRORS["AZ:CITY:0455000:phoenix_city"].lower()


def test_multiline_freight_financing_constraints():
    from phase_l.l18_research_conversion import winability_evidence

    win = winability_evidence(
        {"title": "Commodity commercial item IFB", "offer_count": 2},
        {"suppliers": {"candidates": [{"g": 1}, {"g": 2}]}},
        {"label": "STRONG_RUNWAY", "days": 8},
    )
    assert win["kind"] == "WinabilityEvidence"
    assert win["positive_factor_count"] >= 2
    assert "not a statistical" in win["note"].lower()
    # Multiline solicitations must not collapse to one generic product identity
    from phase_l.l18_research_conversion import DESCRIPTIVE_SPEC, classify_product_identity

    id1 = classify_product_identity({}, {"title": "Line 1: Office Furniture"}, {})
    id2 = classify_product_identity({}, {"title": "Line 2: ASUS Chromebox"}, {})
    assert id1 == DESCRIPTIVE_SPEC
    assert id2 != id1
    # Financing / outreach / SAM constraints retained
    assert sam_api_park_status()["calls_allowed_this_phase"] == 0
    assert BIDNET_AUTH_HISTORY_PARKED


def test_authoritative_gates_unchanged():
    row = {
        "title": "IFB Parts",
        "solicitation_number": "X-1",
        "detail_url": "https://buyer.gov/bid/1",
        "source_url": "https://discovery.example/list",
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
    assert BUILD.startswith("20260928-m3-phase-l18")
    assert EXPIRED == "EXPIRED"


def test_l18_artifacts_when_present():
    required = [
        "l18_research_queue_input.json",
        "l18_research_results.json",
        "l18_quantity_resolution.json",
        "l18_configuration_resolution.json",
        "l18_gov_evidence.json",
        "l18_supplier_evidence.json",
        "l18_economics.json",
        "l18_winability_evidence.json",
        "l18_opengov_mirrors.json",
        "l18_owner_decisions.json",
        "l18_quote_targets.json",
        "l18_summary.json",
    ]
    if not (OUT / "l18_summary.json").exists():
        return
    for name in required:
        assert (OUT / name).exists(), name
    summary = json.loads((OUT / "l18_summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"].startswith("PHASE_L18_")
    assert summary.get("no_sam_api_calls") is True
    assert summary.get("no_cloudflare_bypass") is True
    assert summary["research_queue"]["attempted"] >= 51
    assert summary["research_queue"]["attempted"] == summary["research_queue"]["completed"]
    # researched rows should not all remain in queue
    assert summary["research_queue"]["remaining"] < summary["research_queue"]["input"]
    for doc in (
        "phase_l18_research_conversion.md",
        "phase_l18_quantity_config.md",
        "phase_l18_gov_evidence.md",
        "phase_l18_supplier_research.md",
        "phase_l18_economics.md",
        "phase_l18_winability.md",
        "phase_l18_opengov_mirrors.md",
        "phase_l18_owner_decisions.md",
        "phase_l18_legacy_cleanup.md",
        "phase_l18_regression.md",
    ):
        assert (DOCS / doc).exists(), doc

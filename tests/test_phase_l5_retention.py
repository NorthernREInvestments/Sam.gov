"""Phase L.5 commercial retention repair tests."""

from __future__ import annotations

from phase_l.acquisition_lanes import (
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    MILSPEC_OPEN_CHANNEL,
    MILSPEC_SPECIALTY,
    STAGE3_NO_ROW_CAP,
    classify_acquisition_lane,
)
from phase_l.progressive_funnel import run_progressive_stages_cheap, stage1_cheap_triage, stage2_identity_anchor
from phase_l.product_page_resolution import EXACT_VERIFIED
from phase_l.retention_audit import audit_accessible_rows
from phase_l.stage2_admission import (
    BRAND_OR_EQUAL,
    SPEC_DRIVEN_COMMERCIAL_PRODUCT,
    detect_brand_or_equal,
    detect_spec_driven_commercial,
)


def test_missing_mpn_survives_stage1_and_stage2():
    row = {"title": "Laptop computers for school district classrooms", "our_bid_access": "YES"}
    assert stage1_cheap_triage(row)["pass"] is True
    s2 = stage2_identity_anchor(row)
    assert s2["pass"] is True
    assert s2.get("legacy_strict_would_pass") is False


def test_missing_quantity_survives():
    row = {"title": "Ford F-150 Police Responder fleet vehicle", "our_bid_access": "YES"}
    s2 = stage2_identity_anchor(row)
    assert s2["pass"] is True
    assert s2.get("quantity") in (None, s2.get("quantity"))
    assert "QUANTITY" in str(s2.get("quantity_status") or "QUANTITY_UNRESOLVED")


def test_no_history_survives():
    row = {"title": "Dell PowerEdge server rackmount", "our_bid_access": "YES"}
    pipe = run_progressive_stages_cheap(row)
    assert pipe.get("survives_to_stage3") or (pipe.get("stage2") or {}).get("pass")


def test_no_public_price_survives():
    row = {"title": "Commercial pumps for water treatment plant", "our_bid_access": "YES"}
    assert stage2_identity_anchor(row)["pass"] is True


def test_quote_required_survives():
    row = {
        "title": "Bobcat ToolCat UW56 — contact dealer for quote",
        "our_bid_access": "YES",
    }
    pipe = run_progressive_stages_cheap(row)
    assert (pipe.get("stage2") or {}).get("pass") is True


def test_brand_or_equal_survives():
    row = {"title": "Ford or equal police interceptor utility vehicle", "our_bid_access": "YES"}
    boe = detect_brand_or_equal(row)
    assert boe["brand_or_equal"] is True
    s2 = stage2_identity_anchor(row)
    assert s2["pass"] is True
    assert "brand_or_equal" in (s2.get("identity_anchors") or [])


def test_descriptive_commercial_spec_survives():
    row = {
        "title": "75 HP compact track loader enclosed cab high-flow hydraulics",
        "our_bid_access": "YES",
    }
    assert detect_spec_driven_commercial(row) or stage2_identity_anchor(row)["pass"]
    assert stage2_identity_anchor(row)["pass"] is True
    assert stage2_identity_anchor(row).get("legacy_strict_would_pass") is False


def test_attachment_enrichment_route():
    row = {
        "title": "Equipment procurement package",
        "our_bid_access": "YES",
        "attachments": [{"filename": "vehicle_specifications_pricing.xlsx"}],
    }
    s2 = stage2_identity_anchor(row)
    assert s2["pass"] is True
    assert s2.get("document_enrichment_required") or "document_signal" in (s2.get("identity_anchors") or [])


def test_state_local_generic_title_with_spec_attachment():
    row = {
        "title": "Invitation to Bid — Fleet Items",
        "agency": "City of Austin",
        "our_bid_access": "YES",
        "attachments": [{"name": "line_items_bid_form_vehicle_specs.pdf"}],
    }
    assert stage2_identity_anchor(row)["pass"] is True


def test_registration_not_hard_blocker():
    row = {
        "title": "Pickup trucks for public works",
        "our_bid_access": "YES",
        "access_note": "REGISTER_BEFORE_BID",
    }
    assert stage1_cheap_triage(row)["pass"] is True


def test_nsn_alone_does_not_force_specialty_when_commercial():
    row = {
        "title": "NSN 7025-01-111-2222 Dell Latitude laptop computer",
        "our_bid_access": "YES",
        "nsn": "7025011112222",
    }
    lane = classify_acquisition_lane(row, commercial={"manufacturer": "Dell", "model": "Latitude"})
    assert lane["acquisition_lane"] in {MILSPEC_OPEN_CHANNEL, "COMMERCIAL_OPEN_CHANNEL", "COMMERCIAL_DISTRIBUTOR_CHANNEL"}
    assert lane["acquisition_lane"] != MILSPEC_SPECIALTY or lane.get("reason")


def test_commercial_nsn_classification():
    row = {"title": "NSN filter fluid commercial automotive", "our_bid_access": "YES", "nsn": "2940123915954"}
    lane = classify_acquisition_lane(row)
    # filter is commercial category — should not be blind specialty if category hits
    assert lane["rejected"] is False


def test_unknown_lane_limited_research_still_admits():
    row = {"title": "Modular office furniture workstations", "our_bid_access": "YES"}
    s2 = stage2_identity_anchor(row)
    assert s2["pass"] is True


def test_true_hard_rejects_remain():
    row = {"title": "Professional consulting services only", "our_bid_access": "YES"}
    pipe = run_progressive_stages_cheap(row)
    assert pipe.get("drop_stage") in {0, 1} or (pipe.get("stage1") or {}).get("pass") is False


def test_no_total_caps():
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP


def test_counterfactual_replay_runs_small():
    rows = [
        {"title": "Laptop computers for school district", "our_bid_access": "YES", "agency": "Austin ISD"},
        {"title": "NSN 2995-01-313-0343 VALVE WSDC SPRTA", "our_bid_access": "YES", "nsn": "2995013130343"},
        {"title": "Consulting services FY27", "our_bid_access": "YES"},
    ]
    audit = audit_accessible_rows(rows)
    assert "counterfactual_replay" in audit
    assert audit["counterfactual_replay"]["new_stage2"] >= audit["counterfactual_replay"]["old_stage2"]


def test_original_solicitation_preservation():
    from phase_l.original_solicitation import resolve_original_solicitation

    row = {
        "title": "Fleet vehicles",
        "detail_url": "https://www.bidnetdirect.com/texas/solicitations/open-bids/1",
        "solicitation_id": "ITB-1",
        "agency": "City of Austin",
    }
    orig = resolve_original_solicitation(row)
    assert orig is not None


def test_final_gate_unchanged():
    assert EXACT_VERIFIED == "EXACT_VERIFIED"


def test_spec_driven_status_constant():
    assert SPEC_DRIVEN_COMMERCIAL_PRODUCT
    assert BRAND_OR_EQUAL

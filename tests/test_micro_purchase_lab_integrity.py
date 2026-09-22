"""Targeted tests — µLab integrity: quantity/CLIN, variants, actionability, history-first economics."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from micro_purchase_lab_integrity import (
    ACT_AGGREGATOR,
    ACT_DEADLINE_SUSPICIOUS,
    ACT_RFI,
    ACT_SOURCES_SOUGHT,
    ACT_TRANSACTIONAL,
    COMP_INCUMBENT,
    COMP_MFR_DIRECT,
    ID_EXACT_MPN,
    ID_VARIANT_CONFLICT,
    PATH_DIBBS,
    PATH_GSA_EBUY,
    QTY_AMBIGUOUS,
    QTY_EXACT,
    QTY_STRONG,
    SRC_SOLE,
    assess_actionability,
    assess_approved_source,
    assess_supplier_competitor_risk,
    assess_variant_identity,
    classify_procurement_path,
    compute_max_acquisition_cost,
    evaluate_integrity,
    extract_clins,
    historical_price_range,
    normalize_commercial_to_base,
    normalize_part_number,
    normalize_quantity,
    parts_materially_different,
)
from micro_purchase_lab_pipeline import (
    STATE_COMPLETE,
    STATE_RAW,
    STATE_REJECTED,
    STATE_RESEARCHABLE,
    STATE_WEAK_IDENTITY,
    evaluate_opportunity,
)
from micro_purchase_lab_source_router import (
    NEED_COMMERCIAL_PRICE,
    NEED_DIBBS_RFQ,
    NEED_FEDERAL_AWARD_HISTORY,
    AUTH_AGGREGATOR,
    AUTH_ISSUER,
    prefer_stronger,
    route_evidence,
)


def _open(**kwargs):
    base = {
        "canonical_id": "c-int-1",
        "status": "OPEN",
        "deadline": (date.today() + timedelta(days=14)).isoformat(),
        "deadline_evaluation": {"calendar_days_remaining": 14},
        "source_id": "state_co",
        "agency": "Test Agency",
        "estimated_value": 3900,
        "detail_url": "https://example.gov/sol/1",
    }
    base.update(kwargs)
    return base


def _complete_ready(**kwargs):
    base = _open(
        title="DEWALT DCD996B Hammer Drill cordless",
        manufacturer="DEWALT",
        part_number="DCD996B",
        quantity=12,
        unit_of_issue="EA",
        estimated_value=3900,
        government_price_history={
            "awards": [
                {
                    "date": "2025-03-01",
                    "unit_price": 327.5,
                    "quantity": 10,
                    "part_number": "DCD996B",
                    "manufacturer": "DEWALT",
                    "vendor": "Prior Vendor",
                }
            ]
        },
        commercial_pricing={
            "lowest_public_new_unit": 219.0,
            "public_source": "Industrial Distributor",
            "as_of": date.today().isoformat(),
        },
    )
    base.update(kwargs)
    return base


# --- Quantity / CLIN ---


def test_case_of_10_not_one_piece():
    q = normalize_quantity(raw_quantity=1, raw_uom="CASE", pieces_per_pack=10)
    assert q["confidence"] == QTY_EXACT
    assert q["total_piece_equivalent"] == "10"
    assert q["normalized_base_quantity"] == "10"


def test_case_without_pack_size_ambiguous():
    q = normalize_quantity(raw_quantity=1, raw_uom="CS")
    assert q["confidence"] == QTY_AMBIGUOUS
    assert "PACK_SIZE_CONFLICT" in q["conflicts"]


def test_two_clins_different_destinations_kept_separate():
    row = _open(
        title="Widget supply",
        quantity=None,
        line_items=[
            {"clin": "0001", "quantity": 810, "uom": "EA", "destination": "Richmond"},
            {"clin": "0002", "quantity": 168, "uom": "EA", "destination": "Tracy"},
        ],
    )
    # remove header qty only path
    row.pop("quantity", None)
    out = extract_clins(row)
    assert out["clin_count"] == 2
    assert out["multiple_destinations"] is True
    assert out["keep_line_economics_separate"] is True
    assert out["aggregate_piece_equivalent"] == "978"
    assert out["aggregate_valid_for_undifferentiated_total"] is False
    assert out["overall_quantity_confidence"] in {QTY_EXACT, QTY_STRONG}


def test_ambiguous_quantity_blocks_complete():
    row = _complete_ready(quantity=1, unit_of_issue="CASE")  # no pieces_per_pack
    out = evaluate_opportunity(row)
    assert out["research_state"] != STATE_COMPLETE
    assert "PACK_SIZE_CONFLICT" in (out.get("complete_blockers") or [out.get("unresolved_reason")]) or out[
        "unresolved_reason"
    ] in {"PACK_SIZE_CONFLICT", "QUANTITY_UNRESOLVED"}


def test_idiq_annual_vs_min_order_unresolved():
    row = _complete_ready(
        description="IDIQ estimated annual quantity: 40823 Minimum delivery order: 2267",
        contract_type="IDIQ",
        quantity=40823,
    )
    qty = extract_clins(row)
    assert qty["quantity_basis"]["unresolved"] is True
    out = evaluate_opportunity(row)
    assert out["research_state"] != STATE_COMPLETE
    assert "IDIQ_QUANTITY_BASIS_UNRESOLVED" in (out.get("complete_blockers") or [])


def test_supplier_price_normalized_to_same_unit():
    n = normalize_commercial_to_base(unit_price=100, pack_quantity=10, seller_uom="CASE", government_base_uom="EA")
    assert n["ok"] is True
    assert Decimal(n["normalized_unit_price"]) == Decimal("10.0000")


# --- Identity / variants ---


def test_cosmetic_part_normalization_and_variant_rejects():
    assert normalize_part_number(" dcd996b ") == "DCD996B"
    assert parts_materially_different("DCD996B", "DCD996P2") is True
    assert parts_materially_different("ABC-123-4", "ABC-123-5") is True
    assert parts_materially_different("ABC-123", "ABC123") is False


def test_variant_mismatch_prevents_complete():
    row = _complete_ready(compared_part_number="DCD996P2")
    v = assess_variant_identity(row)
    assert v["identity_state"] == ID_VARIANT_CONFLICT
    assert v["ok_for_complete"] is False
    out = evaluate_opportunity(row)
    assert out["research_state"] != STATE_COMPLETE
    assert out["research_state"] == STATE_WEAK_IDENTITY or "VARIANT_CONFLICT" in (
        out.get("complete_blockers") or [out.get("unresolved_reason")]
    )


def test_exact_mpn_and_nsn_identity():
    v = assess_variant_identity(_open(manufacturer="ACME", part_number="X1-99"))
    assert v["identity_state"] == ID_EXACT_MPN
    v2 = assess_variant_identity(_open(nsn="1234-00-567-8901", title="Bolt"))
    assert v2["identity_state"] == "EXACT_NSN"


# --- DIBBS / approved source ---


def test_dibbs_path_and_approved_cage():
    row = _open(
        source_id="dla_dibbs",
        solicitation_number="SPE7M1-26-T-0001",
        is_dla=True,
        nsn="5310-00-123-4567",
        approved_cages=["12345"],
        approved_source_required=True,
        description="Approved source CAGE 12345 required. FOB DESTINATION. Delivery 30 days. MIL-STD-2073 packaging.",
        quantity=50,
        unit_of_issue="EA",
    )
    path = classify_procurement_path(row)
    assert path["path"] == PATH_DIBBS
    approved = assess_approved_source(row)
    assert approved["state"] == SRC_SOLE
    assert approved["ok_for_complete"] is True
    integ = evaluate_integrity(row, pipeline_partial={"last_government_unit_price": "10", "current_public_price": "8"})
    assert integ["execution"]["mil_std_automatic_reject"] is False
    assert "SPECIAL_PACKAGING_REQUIRED" in integ["execution"]["flags"]


def test_source_controlled_traceability_unresolved_blocks():
    row = _complete_ready(
        description="Manufacturer traceability required",
        traceability_required=True,
    )
    out = evaluate_opportunity(row)
    assert out["research_state"] != STATE_COMPLETE
    assert "TRACEABILITY_UNRESOLVED" in (out.get("complete_blockers") or [])


# --- Actionability ---


def test_actionability_transactional_vs_non():
    assert assess_actionability(_open(notice_type="RFQ")).get("state") == ACT_TRANSACTIONAL
    assert assess_actionability(_open(title="Sources Sought for widgets", notice_type="SOURCES SOUGHT"))["state"] == ACT_SOURCES_SOUGHT
    assert assess_actionability(_open(title="RFI market research", notice_type="RFI"))["state"] == ACT_RFI
    assert assess_actionability(_open(title="Agency forecast only", notice_type="FORECAST"))["transactional"] is False
    assert assess_actionability(_open(title="Vendor registration portal only"))["transactional"] is False
    agg = assess_actionability(_open(source_id="bidnet_direct", source_family="AGGREGATOR"))
    assert agg["state"] == ACT_AGGREGATOR
    upgraded = assess_actionability(
        _open(source_id="bidnet_direct", source_family="AGGREGATOR", authoritative_solicitation_resolved=True)
    )
    assert upgraded["transactional"] is True


def test_suspicious_deadline_and_aggregator_not_complete():
    row = _complete_ready(
        source_id="bidnet_x",
        source_family="AGGREGATOR",
        deadline_evaluation={"calendar_days_remaining": 1200},
    )
    act = assess_actionability(row)
    assert act["state"] in {ACT_DEADLINE_SUSPICIOUS, ACT_AGGREGATOR}
    out = evaluate_opportunity(row)
    assert out["research_state"] != STATE_COMPLETE
    assert out["research_state"] in {STATE_RAW, STATE_RESEARCHABLE, STATE_WEAK_IDENTITY}


# --- History / commercial / economics ---


def test_history_total_not_unit_and_range_not_fabricated():
    rng = historical_price_range(
        [
            {"unit_price": None, "unit_price_status": "TOTAL_ONLY_NOT_UNIT", "total_amount": 10000},
            {"unit_price": "12.50"},
            {"unit_price": "14.00"},
        ]
    )
    assert rng["sample_count"] == 2
    assert rng["fabricated"] is False
    assert rng["recent_low"] == "12.50"


def test_max_acquisition_cost_and_margin_vs_markup():
    mac = compute_max_acquisition_cost(
        historical_unit_price="312.40",
        quantity=10,
        required_gross_margin_pct=25,
        known_freight=50,
        known_packaging=20,
    )
    assert mac["ok"] is True
    assert mac["preliminary"] is True
    # revenue 3124; max product = 3124*0.75 - 70 = 2273; unit = 227.30
    assert Decimal(mac["max_landed_unit_cost"]) == Decimal("227.30")
    assert Decimal(mac["max_total_acquisition_cost"]) == Decimal("2273.00")


def test_wrong_variant_commercial_and_quote_required_label():
    bad = normalize_commercial_to_base(unit_price=50, pack_quantity=1, seller_uom="KG", government_base_uom="EA")
    assert bad["ok"] is False
    assert bad["reason"] == "UOM_CONFLICT"


def test_competitor_risk_surfaces_not_auto_reject():
    row = _complete_ready(
        manufacturer="DEWALT",
        historical_awards=[{"awardee": "DEWALT Industrial", "unit_price": 250}],
        commercial_pricing={
            "lowest_public_new_unit": 270.0,
            "public_source": "DEWALT Factory Store",
            "as_of": date.today().isoformat(),
        },
    )
    risk = assess_supplier_competitor_risk(row, "DEWALT Factory Store")
    assert risk["state"] in {COMP_MFR_DIRECT, COMP_INCUMBENT}
    assert risk["fatal"] is False
    out = evaluate_opportunity(row)
    # May or may not COMPLETE depending on other gates; competitor alone must not reject
    assert out["research_state"] != STATE_REJECTED
    assert out.get("supplier_competitor_risk", {}).get("fatal") is False


# --- Procurement access ---


def test_gsa_ebuy_vehicle_required_blocks_complete():
    row = _complete_ready(description="Respond via GSA eBuy RFQ channel only")
    path = classify_procurement_path(row)
    assert path["path"] == PATH_GSA_EBUY
    assert path["ok_for_complete_direct"] is False
    out = evaluate_opportunity(row)
    assert out["research_state"] != STATE_COMPLETE
    assert "VEHICLE_ACCESS_REQUIRED" in (out.get("complete_blockers") or [])


def test_complete_ready_fixture_still_completes():
    out = evaluate_opportunity(_complete_ready())
    assert out["research_state"] == STATE_COMPLETE
    assert out["integrity_allows_complete"] is True
    assert out["max_acquisition_cost"]["ok"] is True
    assert out["procurement_path"]["path"] in {"STATE_LOCAL_PORTAL", "DIRECT_SMALL_BUY", "SAM_OPEN_MARKET_RFQ"}


def test_existing_rejects_unchanged():
    for title, expect in [
        ("Campus Landscaping Services FY26", True),
        ("Sproul Junior High Remodel", True),
        ("Fresh Produce and Dairy Delivery", True),
    ]:
        out = evaluate_opportunity(_open(title=title, description=title))
        assert out["research_state"] == STATE_REJECTED


def test_unknown_never_complete():
    row = _open(title="Miscellaneous", description="")
    del row["estimated_value"]
    out = evaluate_opportunity(row)
    assert out["research_state"] != STATE_COMPLETE


def test_source_router_priority():
    routes = route_evidence(NEED_DIBBS_RFQ)["routes"]
    assert routes
    assert routes[0]["authority"] == AUTH_ISSUER or routes[0]["authority_rank"] <= 1
    hist = route_evidence(NEED_FEDERAL_AWARD_HISTORY)
    assert hist["warehouse"] is False
    commercial = route_evidence(NEED_COMMERCIAL_PRICE)["routes"]
    assert commercial[0]["authority_rank"] < 6  # manufacturer before aggregator
    a = {"authority": AUTH_ISSUER, "value": 1}
    b = {"authority": AUTH_AGGREGATOR, "value": 2}
    assert prefer_stronger(a, b)["authority"] == AUTH_ISSUER

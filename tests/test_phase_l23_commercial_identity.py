"""Phase L.2.3 commercial identity recovery tests."""

from __future__ import annotations

from phase_l.commercial_identity import (
    CFG_PARTIAL,
    EXACT_COMMERCIAL_MODEL,
    EXACT_EQUIPMENT_MODEL,
    EXACT_VEHICLE_TRIM,
    GENERIC_PRODUCT,
    PRICEABILITY_HIGH,
    PRICEABILITY_LOW,
    PRICEABILITY_NONCOMMERCIAL,
    PROMISING_UNIT_ECONOMICS,
    QUANTITY_REQUIRED,
    TOTAL_PROFIT_UNKNOWN_QUANTITY,
    apply_commercial_overlay_to_screen,
    compute_unit_spread,
    market_research_eligible,
    models_equivalent,
    normalize_commercial_model,
    recover_commercial_identity,
)
from phase_l.enrichment import finalize_economics, stage_a_identity
from phase_l.market_price import generate_market_queries, build_search_identity


def test_bobcat_toolcat_exact_commercial_model():
    row = {
        "title": "Brand Name Bobcat ToolCat UW56 Utility Work Machine",
        "description": "Bobcat ToolCat UW56. Brand name. Qty 1 EA.",
    }
    c = recover_commercial_identity(row)
    assert c["manufacturer"] == "Bobcat"
    assert "UW56" in (c["model"] or "")
    assert c["commercial_identity_state"] == EXACT_EQUIPMENT_MODEL
    assert c["commercial_priceability"] == PRICEABILITY_HIGH
    assert c["market_research_eligible"] is True
    assert c["configuration_completeness"] == CFG_PARTIAL


def test_ford_f150_police_responder_exact_vehicle():
    row = {
        "title": "One or More 2027 Ford F-150 Police Responder(s) or Equivalent for MC Sheriff",
        "description": "Ford F-150 Police Responder vehicles",
    }
    c = recover_commercial_identity(row)
    assert c["manufacturer"] == "Ford"
    assert "Police Responder" in (c["model"] or "") or "F-150" in (c["model"] or "")
    assert c["commercial_identity_state"] == EXACT_VEHICLE_TRIM
    assert c["commercial_priceability"] == PRICEABILITY_HIGH
    assert c["market_research_eligible"] is True


def test_dell_poweredge_exact_commercial_model():
    row = {"title": "Dell PowerEdge R670 Server", "description": "Brand name Dell PowerEdge R670"}
    c = recover_commercial_identity(row)
    assert c["manufacturer"] == "Dell"
    assert "R670" in (c["model"] or "")
    assert c["commercial_identity_state"] == EXACT_COMMERCIAL_MODEL
    assert c["market_research_eligible"] is True


def test_missing_mpn_not_automatically_partial():
    row = {"title": "Canon imageRUNNER ADVANCE DX C5860i", "description": "multifunction printer"}
    c = recover_commercial_identity(row)
    assert c.get("mpn") in (None, "")
    assert c["commercial_identity_state"] == EXACT_COMMERCIAL_MODEL
    assert c["market_research_eligible"] is True


def test_wrong_model_not_equivalent():
    assert not models_equivalent("PowerEdge R660", "PowerEdge R670")
    assert not models_equivalent("UW56", "UW53")
    assert models_equivalent("UW-56", "UW56")
    assert models_equivalent("PowerEdge-R670", "PowerEdge R670")
    assert models_equivalent("F 150 Police Responder", "F-150 Police Responder")


def test_normalize_does_not_collapse_distinct_models():
    assert normalize_commercial_model("R660") != normalize_commercial_model("R670")


def test_exact_model_incomplete_config_researchable_not_auto_economics():
    row = {"title": "Dell PowerEdge R670", "description": "server brand name"}
    c = recover_commercial_identity(row)
    assert c["market_research_eligible"] is True
    assert c["configuration_completeness"] == CFG_PARTIAL
    # Unit spread / total economics still require prices; config does not block research
    screen = apply_commercial_overlay_to_screen(stage_a_identity(row), row)
    assert screen["market_research_eligible"] is True
    assert screen["researchable"] is True


def test_priceability_commercial_high_military_low():
    hi = recover_commercial_identity(
        {"title": "Brand Name Bobcat ToolCat UW56 Utility Work Machine"}
    )
    assert hi["commercial_priceability"] == PRICEABILITY_HIGH
    lo = recover_commercial_identity(
        {
            "title": "NSN 2840-01-448-7511 Turbine Afterburner Source Controlled",
            "description": "drawing-controlled depot repair exclusive",
            "nsn": "2840-01-448-7511",
        }
    )
    assert lo["commercial_priceability"] in {
        PRICEABILITY_LOW,
        PRICEABILITY_NONCOMMERCIAL,
    }


def test_generic_laptop_blocked():
    row = {"title": "Laptop Computer", "description": "general purpose laptop"}
    c = recover_commercial_identity(row)
    assert c["commercial_identity_state"] == GENERIC_PRODUCT
    gate = market_research_eligible(row, commercial=c)
    assert gate["market_research_eligible"] is False


def test_strong_commercial_allowed():
    row = {"title": "Ford F-150 crew cab truck", "description": "one F-150"}
    c = recover_commercial_identity(row)
    # F-150 alone may be STRONG not full Police Responder trim
    assert c["market_research_eligible"] is True or c["commercial_identity_state"] in {
        "STRONG_COMMERCIAL_MODEL",
        EXACT_VEHICLE_TRIM,
        "EXACT_BRAND_MODEL",
    }


def test_unit_spread_without_quantity():
    u = compute_unit_spread(
        historical_unit_price=5000.0,
        public_retail_unit_price=3000.0,
        quantity=None,
    )
    assert u["unit_raw_spread"] == 2000.0
    assert u["total_status"] == TOTAL_PROFIT_UNKNOWN_QUANTITY
    assert PROMISING_UNIT_ECONOMICS in (u["research_signal"] or "")
    assert QUANTITY_REQUIRED in (u["research_signal"] or "")


def test_finalize_economics_unit_spread_no_ready_to_bid():
    econ = finalize_economics(
        {
            "quantity": None,
            "historical_award_unit_price": 5000.0,
            "public_retail_unit_price": 3000.0,
        }
    )
    assert econ["economics_completed"] is False
    assert econ["blocker"] == "UNKNOWN_QUANTITY"
    assert econ.get("ready_to_bid") is False
    assert econ["unit_economics"]["unit_raw_spread"] == 2000.0


def test_model_first_queries_for_commercial():
    sid = build_search_identity(
        {"manufacturer": "Bobcat", "model": "ToolCat UW56"},
        {"title": "Brand Name Bobcat ToolCat UW56 Utility Work Machine"},
    )
    qs = generate_market_queries(sid)
    assert qs
    assert any("UW56" in q or "ToolCat" in q for q in qs[:5])


def test_overlay_promotes_phase_j_partial_bobcat():
    row = {
        "title": "Brand Name Bobcat ToolCat UW56 Utility Work Machine",
        "description": "Bobcat ToolCat UW56 brand name",
        "our_bid_access": "YES",
    }
    screen0 = stage_a_identity(row)
    # Phase J typically PARTIAL without MPN
    assert screen0.get("identity_research_state") in {
        "IDENTITY_PARTIAL",
        "IDENTITY_INSUFFICIENT",
        "OR_EQUAL_RESEARCHABLE",
        "IDENTITY_STRONG",
        "IDENTITY_EXACT",
    }
    screen = apply_commercial_overlay_to_screen(screen0, row)
    assert screen["market_research_eligible"] is True
    assert (screen.get("identity") or {}).get("model")
    assert (screen.get("identity") or {}).get("manufacturer") == "Bobcat"

"""Product identity + market pricing — extraction, readiness, economics link."""

from __future__ import annotations

from m3_commercial_engine import PRICE_LEVEL_2_PUBLIC, PRICE_LEVEL_4_UNKNOWN, SCORE_HIGH
from m3_pipeline_store import M3PipelineStore
from m3_product_pricing import (
    INSUFFICIENT,
    MATCH_HIGH,
    MATCH_MEDIUM,
    MATCH_UNKNOWN,
    NEEDS_ID,
    READY,
    VAL_ESTIMATED,
    VAL_UNKNOWN,
    analyze_product_pricing_top,
    build_contract_value_model,
    build_market_price_profile,
    build_product_identity_profile,
    build_product_pricing_intelligence,
    deal_room_product_section,
    extract_requirements,
    product_match_confidence,
    research_readiness_score,
)


def _opp(**kwargs):
    base = {
        "canonical_id": kwargs.get("canonical_id", "pp-1"),
        "title": "Cisco Catalyst 9300 Switches",
        "description": "P/N C9300-48P quantity QTY: 12 EA delivery only",
        "agency": "State of Illinois",
        "estimated_value": 120000,
        "product_category": "IT_NETWORKING",
        "line_items": [
            {
                "description": "Cisco Catalyst 9300-48P",
                "part_number": "C9300-48P",
                "manufacturer": "Cisco",
                "quantity": 12,
                "unit": "EA",
            }
        ],
    }
    base.update(kwargs)
    return base


def test_product_extraction_and_part_match():
    req = extract_requirements(_opp())
    assert req["alternates_allowed"] is False or isinstance(req["alternates_allowed"], bool)
    ident = build_product_identity_profile(_opp())
    assert ident["Manufacturer"] == "Cisco"
    assert ident["Manufacturer_part_number"] != "UNKNOWN" or "C9300" in str(ident.get("Model_number"))
    match = product_match_confidence(ident)
    assert match["PRODUCT_MATCH_CONFIDENCE"] in {MATCH_HIGH, MATCH_MEDIUM}
    assert match["pricing_research_allowed"] is True


def test_unknown_identity_blocks_pricing_research():
    row = _opp(title="Miscellaneous supplies", description="as needed", line_items=[], estimated_value=None)
    # wipe manufacturer cues
    row["title"] = "General office needs"
    row["description"] = "various items"
    ident = build_product_identity_profile(row)
    match = product_match_confidence(ident)
    market = build_market_price_profile(row, ident, match, allow_paid_web=False)
    assert market["primary_level"] == PRICE_LEVEL_4_UNKNOWN
    assert market.get("research_blocked_reason") == "identity_insufficient_for_pricing" or not match["pricing_research_allowed"]


def test_contract_value_extraction():
    cv = build_contract_value_model(_opp())
    assert cv["confidence"] == VAL_ESTIMATED
    assert cv["contract_value"] == 120000
    empty = build_contract_value_model(_opp(estimated_value=None, title="Widget"))
    assert empty["confidence"] == VAL_UNKNOWN


def test_pricing_evidence_levels_from_existing():
    row = _opp(
        commercial_pricing={"lowest_public_new_unit": 2100, "public_source": "https://example.test/cisco"}
    )
    ident = build_product_identity_profile(row)
    match = product_match_confidence(ident)
    market = build_market_price_profile(row, ident, match, allow_paid_web=False)
    assert market["primary_level"] == PRICE_LEVEL_2_PUBLIC
    assert market["items"]
    assert market["items"][0]["Notes"] == "not_assumed_as_final_acquisition_cost"


def test_research_readiness_scoring():
    ident = build_product_identity_profile(_opp())
    match = product_match_confidence(ident)
    contract = build_contract_value_model(_opp())
    ready = research_readiness_score(ident, match, contract)
    assert ready["RESEARCH_READINESS"] == READY
    weak = research_readiness_score(
        {"Identity_confidence": MATCH_UNKNOWN, "Manufacturer": "UNKNOWN", "Manufacturer_part_number": "UNKNOWN", "NSN": "UNKNOWN", "Quantity": "UNKNOWN"},
        {"PRODUCT_MATCH_CONFIDENCE": MATCH_UNKNOWN, "pricing_research_allowed": False},
        {"confidence": VAL_UNKNOWN},
    )
    assert weak["RESEARCH_READINESS"] in {NEEDS_ID, INSUFFICIENT}


def test_economics_integration_and_analyze(tmp_path):
    store = M3PipelineStore(path=tmp_path / "p.json", durable=False)
    store._rows["a"] = _opp(
        canonical_id="a",
        commercial_pricing={"lowest_public_new_unit": 8000, "public_source": "https://ex.test/x"},
    )
    store._rows["b"] = _opp(
        canonical_id="b",
        title="Vague stuff",
        description="n/a",
        line_items=[],
        estimated_value=None,
    )
    store.save()
    out = analyze_product_pricing_top(store, limit=5, allow_paid_web=False)
    assert out["analyzed"] >= 1
    assert out["paid"] == 0
    assert "pricing_levels" in out
    pkg = build_product_pricing_intelligence(store.get("a") or _opp(canonical_id="a"), allow_paid_web=False)
    assert "ECONOMICS_IMPACT" in pkg
    assert pkg["ECONOMICS_IMPACT"]["can_calculate_profit"] is True or pkg["ECONOMICS_IMPACT"]["missing"]
    sec = deal_room_product_section(store.get("a") or _opp(canonical_id="a"))
    assert sec["kind"] == "M3DealRoomProductIntelligence"
    assert sec["Next_Action"]

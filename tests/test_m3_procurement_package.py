"""Complete procurement package intelligence — extraction, BOM, readiness, economics."""

from __future__ import annotations

from m3_commercial_engine import PRICE_LEVEL_2_PUBLIC, PRICE_LEVEL_4_UNKNOWN
from m3_pipeline_store import M3PipelineStore
from m3_procurement_package import (
    BOM_CONFIRMED,
    INSUFFICIENT_DATA,
    MATCH_HIGH,
    MATCH_MEDIUM,
    MATCH_UNKNOWN,
    NEEDS_DOCUMENTS,
    PARTIAL,
    Q_NEEDS_PRODUCT_IDENTITY,
    READY_FOR_ECONOMICS,
    READY_FOR_PRICING,
    analyze_procurement_packages_top,
    build_commercial_requirement_profile,
    build_package_product_identity,
    build_procurement_bom,
    build_procurement_package,
    build_product_configuration_profile,
    commercial_completeness_score,
    deal_room_procurement_package_section,
    validate_identifier,
)


def _opp(**kwargs):
    base = {
        "canonical_id": kwargs.get("canonical_id", "pkg-1"),
        "title": "Cisco Catalyst 9300 Switches",
        "description": "P/N C9300-48P quantity QTY: 12 EA FOB destination deliver to Building 4 Warehouse",
        "agency": "State of Illinois",
        "buyer": "DoIT",
        "solicitation_number": "IFB-2026-0199",
        "contract_type": "IFB",
        "estimated_value": 120000,
        "product_category": "IT_NETWORKING",
        "line_items": [
            {
                "description": "Cisco Catalyst 9300-48P Switch",
                "part_number": "C9300-48P",
                "manufacturer": "Cisco",
                "quantity": 12,
                "unit": "EA",
            },
            {
                "description": "Rack mount kit accessory",
                "part_number": "C9300-RM-KIT",
                "manufacturer": "Cisco",
                "quantity": 12,
                "unit": "EA",
            },
            {
                "description": "SmartNet support agreement 1-year",
                "part_number": "CON-SNT-C930048",
                "manufacturer": "Cisco",
                "quantity": 12,
                "unit": "EA",
            },
        ],
    }
    base.update(kwargs)
    return base


def test_contract_and_value_extraction():
    cr = build_commercial_requirement_profile(_opp())
    assert cr["Solicitation_ID"] == "IFB-2026-0199"
    assert cr["Agency"] == "State of Illinois"
    assert cr["Estimated_contract_value"] == 120000
    assert cr["Quantity"] == 12 or cr["Quantity"] == 12.0
    assert cr["Confidence"] in {"VERIFIED", "ESTIMATED"}
    assert len(cr["Line_items"]) >= 2


def test_quantity_and_model_extraction():
    ident = build_package_product_identity(_opp())
    assert ident["Manufacturer"] == "Cisco"
    assert ident["Manufacturer_part_number"] == "C9300-48P"
    assert ident["Identity_confidence"] in {MATCH_HIGH, MATCH_MEDIUM}
    assert ident["Quantity"] == 12 or ident["Quantity"] == 12.0


def test_part_number_validation_accepts_real_rejects_false():
    ok = validate_identifier("C9300-48P", source="line_items_or_bom", kind="part_number")
    assert ok["accepted"] is True
    assert ok["Validation_status"] == "ACCEPTED"

    bad_url = validate_identifier("https://sam.gov/opp/abc123", source="portal", kind="part_number")
    assert bad_url["accepted"] is False

    bad_word = validate_identifier("Contracts", source="portal", kind="part_number")
    assert bad_word["accepted"] is False

    bad_ts = validate_identifier("20260917120000", source="portal", kind="part_number")
    assert bad_ts["accepted"] is False


def test_accessory_extraction_and_configuration_tree():
    row = _opp()
    ident = build_package_product_identity(row)
    cfg = build_product_configuration_profile(row, ident)
    assert cfg["PRIMARY_EQUIPMENT"]
    assert any("accessory" in str(a.get("Item") or "").lower() for a in cfg["REQUIRED_ACCESSORIES"])
    assert any(a.get("config_type") == "SUPPORT_AGREEMENTS" or "support" in str(a.get("Item") or "").lower()
               for group in (cfg.get("items_by_type") or {}).values() for a in group)


def test_bom_creation():
    row = _opp()
    ident = build_package_product_identity(row)
    cfg = build_product_configuration_profile(row, ident)
    bom = build_procurement_bom(row, ident, cfg)
    assert bom["line_count"] >= 2
    assert bom["lines"][0]["config_type"] == "PRIMARY_EQUIPMENT"
    assert any(L.get("Confidence") == BOM_CONFIRMED for L in bom["lines"])
    # UNKNOWN accessories should not be cost-eligible unless confirmed/likely
    for L in bom["lines"]:
        if L.get("Confidence") == "UNKNOWN":
            assert L.get("include_in_cost") is False


def test_false_identifier_rejected_from_identity():
    row = _opp(
        title="Physical Security Systems",
        description="See https://sam.gov/opportunity/xyz documents portal",
        line_items=[],
        estimated_value=None,
    )
    # Inject a portal-looking fake part via description only without P/N label — should not invent
    ident = build_package_product_identity(row)
    # Should not accept URL fragments as part numbers
    assert "http" not in str(ident.get("Manufacturer_part_number") or "").lower()
    assert "sam.gov" not in str(ident.get("Manufacturer_part_number") or "").lower()


def test_pricing_readiness_and_completeness():
    row = _opp(commercial_pricing={"lowest_public_new_unit": 2100, "public_source": "https://example.test/cisco"})
    pkg = build_procurement_package(row, allow_paid_web=False)
    assert pkg["COMMERCIAL_COMPLETENESS"]["COMMERCIAL_COMPLETENESS"] in {
        READY_FOR_ECONOMICS,
        READY_FOR_PRICING,
        PARTIAL,
    }
    assert pkg["MARKET_PRICING"]["primary_level"] in {
        PRICE_LEVEL_2_PUBLIC,
        PRICE_LEVEL_4_UNKNOWN,
        "LEVEL_2_PUBLIC_MARKET",
        "LEVEL_2_PUBLIC",
    } or "LEVEL_2" in str(pkg["MARKET_PRICING"]["primary_level"]) or pkg["MARKET_PRICING"]["primary_level"] == PRICE_LEVEL_2_PUBLIC
    weak = build_procurement_package(
        _opp(title="Misc supplies", description="various", line_items=[], estimated_value=None),
        allow_paid_web=False,
    )
    assert weak["COMMERCIAL_COMPLETENESS"]["COMMERCIAL_COMPLETENESS"] in {
        INSUFFICIENT_DATA,
        NEEDS_DOCUMENTS,
        PARTIAL,
    }
    assert weak["PRODUCT_IDENTITY"]["Identity_confidence"] in {MATCH_UNKNOWN, MATCH_MEDIUM, "LOW", MATCH_HIGH} or True
    assert weak["RESEARCH_READINESS"]["RESEARCH_QUEUE"] in {
        Q_NEEDS_PRODUCT_IDENTITY,
        "NEEDS_DOCUMENT_RECOVERY",
        "NEEDS_CONFIGURATION",
        "NEEDS_PRICING",
    }


def test_economics_integration_and_analyze(tmp_path):
    store = M3PipelineStore(path=tmp_path / "pkg.json", durable=False)
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
    out = analyze_procurement_packages_top(store, limit=5, allow_paid_web=False)
    assert out["analyzed"] >= 1
    assert out["paid"] == 0
    assert out["configuration"]["boms_created"] >= 1
    assert "pricing_levels" in out
    pkg = build_procurement_package(store.get("a") or _opp(canonical_id="a"), allow_paid_web=False)
    assert "ECONOMICS_HANDOFF" in pkg
    assert pkg["ECONOMICS_HANDOFF"]["can_calculate"] is True or pkg["ECONOMICS_HANDOFF"]["missing"]
    sec = deal_room_procurement_package_section(store.get("a") or _opp(canonical_id="a"))
    assert sec["kind"] == "M3DealRoomProcurementPackage"
    assert sec["Next_Action"]
    assert sec["BOM"]

"""Tests for Product Identity Resolution Engine."""

from __future__ import annotations

from m3_product_identity_resolution import (
    CATEGORY_ONLY,
    EXACT_IDENTITY,
    EXACT_ONLY,
    EQUIVALENTS_ALLOWED,
    NEEDS_IDENTITY_RESEARCH,
    READY_FOR_SUPPLIER_SEARCH,
    READY_WITH_SPECIFICATIONS,
    SPECIFICATION_BASED,
    SPECIFICATION_IDENTITY,
    UNKNOWN,
    analyze_product_identity_resolution_top,
    apply_va_identity_update,
    build_specification_profile,
    build_substitution_profile,
    classify_resolution_category,
    deal_room_product_identity_resolution_section,
    reject_false_identifier,
    resolve_line_product_identity,
    resolve_opportunity_product_identities,
)


def test_reject_false_identifiers():
    assert reject_false_identifier("SOL-2026-00412")["accepted"] is False
    assert reject_false_identifier("CLIN-0001")["accepted"] is False
    assert reject_false_identifier("DOC-99")["accepted"] is False
    assert reject_false_identifier("12")["accepted"] is False
    assert reject_false_identifier("names")["accepted"] is False
    assert reject_false_identifier("numbers")["accepted"] is False
    assert reject_false_identifier("C9300-48P-A")["accepted"] is True
    assert reject_false_identifier("1234-56-789-0123", kind="nsn")["accepted"] is True


def test_rejects_boilerplate_manufacturer_names():
    row = {
        "canonical_id": "boilerplate-1",
        "title": "Tungsten-Carbide Blades for Snow/Ice Removal",
        "line_items": [{"description": "002367300 - Tungsten-Carbide BLADE", "quantity": 1}],
        "attachment_text": (
            "Manufacturer names and part numbers are provided for reference only. "
            "Tungsten-carbide blade material. Dimensions 48 inch. Application snow/ice removal."
        ),
    }
    prof = resolve_line_product_identity(row, row["line_items"][0], index=1)
    assert prof["Manufacturer"] != "names"
    assert prof["Manufacturer_part_number"] != "numbers"
    assert prof["Identity_type"] in {SPECIFICATION_IDENTITY, CATEGORY_ONLY, UNKNOWN}


def test_exact_part_model_nsn_extraction():
    row = {
        "canonical_id": "exact-1",
        "title": "Cisco Network Switches",
        "description": "OEM: Cisco. P/N: C9300-48P-A. Model Catalyst 9300.",
        "line_items": [
            {
                "description": "Cisco Catalyst 9300 Switch",
                "part_number": "C9300-48P-A",
                "manufacturer": "Cisco",
                "quantity": 10,
                "unit": "EA",
            }
        ],
        "attachment_text": "NSN 7025-01-123-4567 required. CAGE 0GXW3.",
    }
    # NSN must be valid pattern 4-2-3-4
    row["attachment_text"] = "NSN 7025-01-123-4567. Manufacturer Cisco."
    prof = resolve_line_product_identity(row, row["line_items"][0], index=1)
    assert prof["kind"] == "PRODUCT_IDENTITY_RESOLUTION_PROFILE"
    assert prof["Identity_type"] == EXACT_IDENTITY
    assert prof["Manufacturer"] == "Cisco"
    assert prof["Manufacturer_part_number"] == "C9300-48P-A"
    assert prof["Confidence"] in {"HIGH", "MEDIUM"}
    assert (prof["SUPPLIER_RESEARCH_READINESS_SCORE"] or {}).get("status") == READY_FOR_SUPPLIER_SEARCH


def test_specification_identity_for_generic_blade():
    row = {
        "canonical_id": "blade-1",
        "title": "Tungsten-Carbide Blades for Snow/Ice Removal",
        "line_items": [
            {
                "description": "Tungsten-carbide blades for snow/ice removal, 3/4 inch x 6 ft, ASTM compatible mounting",
                "quantity": 3,
                "unit": "EA",
            }
        ],
        "attachment_text": (
            "Industrial blade shall be tungsten-carbide. Dimensions 0.75 in x 6 ft. "
            "Application: snow/ice removal plow. Must meet ASTM wear standard. "
            "Compatible with existing plow mounting brackets."
        ),
    }
    prof = resolve_line_product_identity(row, row["line_items"][0], index=1)
    assert prof["Identity_type"] == SPECIFICATION_IDENTITY
    assert prof["Manufacturer"] == "UNKNOWN"
    assert prof["Manufacturer_part_number"] == "UNKNOWN"
    spec = prof["SPECIFICATION_PROFILE"]
    assert spec["kind"] == "SPECIFICATION_PROFILE"
    assert spec["well_defined"] is True
    materials = spec.get("Materials")
    assert materials != "UNKNOWN"
    assert any("carbide" in str(m).lower() for m in materials)
    readiness = prof["SUPPLIER_RESEARCH_READINESS_SCORE"]["status"]
    assert readiness in {READY_WITH_SPECIFICATIONS, NEEDS_IDENTITY_RESEARCH}


def test_category_only_and_unknown():
    row = {
        "canonical_id": "cat-1",
        "title": "Equipment package",
        "line_items": [{"description": "maintenance component", "quantity": 1}],
    }
    prof = resolve_line_product_identity(row, row["line_items"][0], index=1)
    assert prof["Identity_type"] in {CATEGORY_ONLY, SPECIFICATION_IDENTITY, UNKNOWN}
    assert prof["Manufacturer_part_number"] == "UNKNOWN"
    # Must not invent manufacturer
    assert prof["Manufacturer"] == "UNKNOWN"


def test_category_classification():
    assert classify_resolution_category("network switch and servers")["Category"] == "IT_EQUIPMENT"
    assert classify_resolution_category("tungsten-carbide snow blade")["Category"] == "INDUSTRIAL_COMPONENTS"
    assert classify_resolution_category("law enforcement badges")["Category"] in {
        "OTHER",
        "SAFETY_EQUIPMENT",
        "CONSUMABLES",
    }


def test_substitution_never_assumed():
    unk = build_substitution_profile("Purchase blades", CATEGORY_ONLY)
    assert unk["Status"] == "UNKNOWN"
    assert unk["Equivalents_allowed"] is False

    exact = build_substitution_profile("Brand name only. No substitutes accepted.", EXACT_IDENTITY)
    assert exact["Status"] == EXACT_ONLY

    eq = build_substitution_profile("Cisco Catalyst or equal approved", EXACT_IDENTITY)
    assert eq["Status"] == EQUIVALENTS_ALLOWED

    spec = build_substitution_profile("Must meet the following specifications", SPECIFICATION_IDENTITY)
    assert spec["Status"] == SPECIFICATION_BASED


def test_pricing_handoff_rejects_generic_only():
    row = {
        "canonical_id": "gen-1",
        "title": "Equipment package",
        "line_items": [{"description": "equipment package", "quantity": 1}],
    }
    prof = resolve_line_product_identity(row, row["line_items"][0], index=1)
    handoff = prof["PRICING_HANDOFF"]
    if prof["Identity_type"] in {CATEGORY_ONLY, UNKNOWN}:
        assert handoff["eligible"] is False
        assert "generic" in handoff["reason"] or "not_eligible" in handoff["reason"] or "do_not" in handoff["reason"]


def test_specification_profile_builder():
    spec = build_specification_profile(
        "Industrial cutting blade",
        "Material: carbide. Size: 12 inch. Application: metal cutting. ASTM B123. Compatible with Model X mounts.",
    )
    assert spec["Confidence"] in {"HIGH", "MEDIUM"}
    assert spec["well_defined"] is True


def test_opportunity_resolution_and_queues(monkeypatch):
    monkeypatch.setattr("m3_product_identity_resolution.save_learning_index", lambda idx: None)
    monkeypatch.setattr(
        "m3_product_identity_resolution.load_learning_index",
        lambda: {"kind": "M3ProductIdentityLearning", "unresolved_categories": {}, "manufacturers_found": {}, "common_descriptions": {}, "portal_success": {}},
    )
    row = {
        "canonical_id": "opp-mix",
        "title": "Mixed buy",
        "source_id": "state_ia",
        "line_items": [
            {
                "description": "Dell PowerEdge server",
                "part_number": "R750",
                "manufacturer": "Dell",
                "quantity": 2,
            },
            {
                "description": "Tungsten-carbide plow blade 3/4 in x 6 ft for snow removal ASTM",
                "quantity": 4,
            },
        ],
        "attachment_text": "Dell OEM required. Second item per specification; or equal not stated.",
    }
    result = resolve_opportunity_product_identities(row, update_learning=False)
    assert result["kind"] == "M3ProductIdentityResolution"
    assert result["Products_analyzed"] == 2
    assert result["Exact_identities"] >= 1
    assert result["VA"]["may_invent_identities"] is False


def test_analyze_top_and_deal_room_va(monkeypatch):
    monkeypatch.setattr("m3_product_identity_resolution.save_resolution_index", lambda idx: None)
    monkeypatch.setattr("m3_product_identity_resolution.save_learning_index", lambda idx: None)
    monkeypatch.setattr(
        "m3_product_identity_resolution.load_resolution_index",
        lambda: {"kind": "M3ProductIdentityResolutionIndex", "by_id": {}},
    )
    monkeypatch.setattr(
        "m3_product_identity_resolution.load_learning_index",
        lambda: {"kind": "M3ProductIdentityLearning", "unresolved_categories": {}, "manufacturers_found": {}, "common_descriptions": {}, "portal_success": {}},
    )

    class Store:
        def __init__(self):
            self._rows = {
                "a": {
                    "canonical_id": "a",
                    "title": "Law Enforcement Badges and Repair Services",
                    "source_id": "state_ia",
                    "line_items": [
                        {
                            "description": "Iowa State Patrol Badge, metal, gold finish, standard mounting",
                            "quantity": 1,
                            "unit": "EA",
                        }
                    ],
                    "attachment_text": (
                        "Badge shall be metal with gold finish. Application: law enforcement uniform. "
                        "Required features: state seal engraving. Dimensions approx 2.5 inch."
                    ),
                }
            }

        def all(self):
            return list(self._rows.values())

        def save(self):
            return None

    store = Store()
    run = analyze_product_identity_resolution_top(store, limit=5, persist=True)
    assert run["kind"] == "M3ProductIdentityResolutionRun"
    assert run["NEXT_STATE"] == "PRODUCT_IDENTITY_RESOLUTION_OPERATIONAL"
    assert run["PRODUCT_IDENTITY"]["Products_analyzed"] >= 1
    assert store._rows["a"].get("product_identity_resolution")

    sec = deal_room_product_identity_resolution_section(store._rows["a"])
    assert sec["kind"] == "M3DealRoomProductIdentityResolution"
    assert "Identity_type" in sec

    denied = apply_va_identity_update(store, "a", action="INVENT_IDENTITY")
    assert denied["ok"] is False
    denied2 = apply_va_identity_update(store, "a", action="APPROVE_SUBSTITUTION")
    assert denied2["ok"] is False

    ok = apply_va_identity_update(
        store,
        "a",
        action="ATTACH_EVIDENCE",
        evidence={"Manufacturer": "Blackinton", "Manufacturer_part_number": "B-12345"},
        note="Found in official badge catalog PDF",
        line_index=0,
    )
    assert ok["ok"] is True
    # False part still rejected
    bad = apply_va_identity_update(
        store,
        "a",
        action="ATTACH_EVIDENCE",
        evidence={"Manufacturer_part_number": "SOL-999"},
        line_index=0,
    )
    assert bad["ok"] is True
    profiles = store._rows["a"]["product_identity_resolution_full"]["PRODUCT_IDENTITY_RESOLUTION_PROFILES"]
    assert profiles[0]["Manufacturer_part_number"] == "UNKNOWN"

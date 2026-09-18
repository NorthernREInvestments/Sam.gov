"""Tests for Commercial Product Matching + Supplier Pricing Research Engine."""

from __future__ import annotations

from m3_commercial_product_matching import (
    DISTRIBUTOR_PATH_FOUND,
    PUBLIC_PRICE_ONLY,
    Q_NEEDS_GOVERNMENT_REVENUE_RESEARCH,
    Q_NEEDS_PRODUCT_MATCH,
    WHOLESALE_UNVERIFIED,
    analyze_commercial_matching_top,
    analyze_line_commercial_match,
    apply_va_matching_update,
    build_commercial_compliance_matrix,
    build_commercial_price_profile,
    build_search_strategies,
    build_wholesale_opportunity_profile,
    deal_room_commercial_market_section,
    extract_evidence_candidates,
)
from m3_deal_economics import STATUS_UNKNOWN
from m3_product_identity_resolution import (
    READY_WITH_SPECIFICATIONS,
    SPECIFICATION_IDENTITY,
    resolve_opportunity_product_identities,
)


class _MemStore:
    def __init__(self, rows: dict):
        self._rows = rows

    def all(self):
        return list(self._rows.values())

    def save(self):
        return None


def _blade_row(**extra):
    row = {
        "canonical_id": "blade-cm-1",
        "title": "Tungsten-Carbide Blades for Snow/Ice Removal",
        "line_items": [
            {
                "description": "002367300 - Tungsten-Carbide BLADE",
                "quantity": 20,
                "unit": "EA",
            }
        ],
        "attachment_text": (
            "Industrial blade shall be tungsten-carbide. Dimensions 0.75 in x 6 ft. "
            "Application: snow/ice removal plow. Must meet ASTM wear standard. "
            "Compatible with existing plow mounting brackets. "
            "Or equal products meeting these specifications are acceptable."
        ),
        "documents": [
            {
                "extracted_text": (
                    "Tungsten-Carbide BLADE 3 FT. SECTION. Mill Certifications required. "
                    "F.O.B Destination Ames IA."
                )
            }
        ],
    }
    row.update(extra)
    return row


def test_extract_agency_sku_and_botanical_candidates():
    blade = _blade_row()
    identity = resolve_opportunity_product_identities(blade, update_learning=False)
    primary = identity["primary_profile"]
    cands = extract_evidence_candidates(blade, primary)
    origins = {c.get("candidate_origin") for c in cands}
    assert "agency_catalog_reference" in origins or any(
        c.get("SKU") == "002367300" for c in cands
    )

    seed_profile = {
        "Original_description": "Big bluestem (Andropogon gerardii)",
        "Resolved_product_name": "Big bluestem (Andropogon gerardii)",
        "Identity_type": "CATEGORY_ONLY",
        "Specifications": {},
        "Line_item": 1,
        "Quantity": 324.3,
    }
    seed_cands = extract_evidence_candidates(
        {"canonical_id": "seed-1", "title": "Wildflower and Native Grass Seed"},
        seed_profile,
    )
    assert any(c.get("candidate_origin") == "botanical_commercial_identity" for c in seed_cands)
    assert any("Andropogon gerardii" in str(c.get("Commercial_product_name")) for c in seed_cands)


def test_compliance_matrix_no_assumed_compliance():
    profile = {
        "Specifications": {
            "Materials": ["tungsten-carbide"],
            "Dimensions": "6 ft",
            "Application": "snow plow",
            "Standards": ["ASTM"],
            "Required_features": ["mounting"],
        }
    }
    brief = {
        "candidate_origin": "specification_search_brief",
        "Specifications": profile["Specifications"],
    }
    matrix = build_commercial_compliance_matrix(profile, brief)
    assert matrix["kind"] == "COMMERCIAL_COMPLIANCE_MATRIX"
    assert matrix["presented_as_compliant"] is False
    assert matrix["overall"] == "SEARCH_BRIEF_NOT_A_PRODUCT"

    mismatch = {
        "candidate_origin": "va_attached",
        "Manufacturer": "Acme",
        "Specifications": {
            "Materials": ["mild steel"],
            "Dimensions": "6 ft",
            "Application": "snow plow",
            "Standards": ["ASTM"],
            "Required_features": ["mounting"],
        },
        "Evidence_source": "https://example.com/product",
    }
    m2 = build_commercial_compliance_matrix(profile, mismatch)
    assert any(r["Result"] == "MISMATCH" for r in m2["rows"])
    assert m2["presented_as_compliant"] is False


def test_search_strategies_and_wholesale():
    row = _blade_row()
    identity = resolve_opportunity_product_identities(row, update_learning=False)
    strategies = build_search_strategies(identity["primary_profile"], row)
    approaches = {s["approach"] for s in strategies}
    assert "exact_description" in approaches
    assert "specification" in approaches
    assert "distributor_catalog" in approaches

    pricing = {"best_observed_unit": 900.0, "counts": {"LEVEL_2": 1}}
    rels = [
        {
            "Supplier": "industrial_mro",
            "Supplier_type": "industrial_mro",
            "Confidence": "MEDIUM",
        }
    ]
    wh = build_wholesale_opportunity_profile(pricing, rels)
    assert wh["kind"] == "WHOLESALE_OPPORTUNITY_PROFILE"
    assert wh["distributor_paths"] >= 1
    assert WHOLESALE_UNVERIFIED in wh["statuses"] or wh["status"] == WHOLESALE_UNVERIFIED
    assert wh["notes"]


def test_pricing_profile_levels_and_va_attach():
    row = _blade_row(
        commercial_pricing={"public_unit_price": 175.5, "public_source": "https://example.com/p"}
    )
    identity = resolve_opportunity_product_identities(row, update_learning=False)
    profile = identity["primary_profile"]
    cands = extract_evidence_candidates(row, profile)
    pricing = build_commercial_price_profile(row, profile, cands)
    assert pricing["kind"] == "COMMERCIAL_PRICE_PROFILE"
    assert pricing["counts"]["LEVEL_2"] >= 1 or pricing["best_observed_unit"] == 175.5

    store = _MemStore({"blade-cm-1": _blade_row()})
    bad = apply_va_matching_update(store, "blade-cm-1", action="CONTACT_SUPPLIER")
    assert bad["ok"] is False
    ok = apply_va_matching_update(
        store,
        "blade-cm-1",
        action="RESEARCH_PRODUCTS",
        evidence={
            "Commercial_product_name": "Carbide plow blade insert",
            "Manufacturer": "ExampleBladeCo",
            "url": "https://example.com/blade",
            "Match_confidence": "MEDIUM",
        },
    )
    assert ok["ok"] is True
    assert store._rows["blade-cm-1"]["va_commercial_candidates"]


def test_line_and_batch_and_deal_room():
    row = _blade_row(estimated_value=45000)
    identity = resolve_opportunity_product_identities(row, update_learning=False)
    assert identity["Specification_identities"] >= 1
    primary = identity["primary_profile"]
    assert primary["Identity_type"] == SPECIFICATION_IDENTITY
    line = analyze_line_commercial_match(row, primary, allow_paid_web=False)
    assert line["kind"] == "M3LineCommercialMatch"
    assert len(line["SEARCH_STRATEGIES"]) >= 3
    assert line["SUPPLIER_PRODUCT_RELATIONSHIPS"]
    assert line["COMMERCIAL_PRICE_PROFILE"]["kind"] == "COMMERCIAL_PRICE_PROFILE"
    assert line["queue"] in {
        Q_NEEDS_PRODUCT_MATCH,
        "NEEDS_SPEC_VALIDATION",
        "NEEDS_SUPPLIER_RESEARCH",
        "NEEDS_PRICING",
        "NEEDS_WHOLESALE_VERIFICATION",
        Q_NEEDS_GOVERNMENT_REVENUE_RESEARCH,
        "READY_FOR_ECONOMICS",
    }

    store = _MemStore(
        {
            "blade-cm-1": row,
            "seed-1": {
                "canonical_id": "seed-1",
                "title": "Wildflower and Native Grass Seed",
                "line_items": [
                    {
                        "description": "Big bluestem (Andropogon gerardii)",
                        "quantity": 324.3,
                        "unit": "LB",
                    }
                ],
            },
        }
    )
    run = analyze_commercial_matching_top(store, limit=5, persist=False)
    assert run["NEXT_STATE"] == "COMMERCIAL_PRODUCT_MATCHING_OPERATIONAL"
    assert run["analyzed"] >= 1
    assert run["SAFETY"]["Outreach_actions"] == 0
    assert run["PRODUCT_MATCHING"]["Products_analyzed"] >= 1

    panel = deal_room_commercial_market_section(store._rows["blade-cm-1"])
    assert panel["kind"] == "M3DealRoomCommercialMarketResearch"
    assert "CONTACT_SUPPLIER" in panel["VA_forbidden_actions"]
    assert panel["Acquisition_status"] in {STATUS_UNKNOWN, "BELOW_TARGET", "MEETS_TARGET", "WITHIN_ACCEPTABLE_RANGE", "EXCEEDS_TARGET", "UNVIABLE"}


def test_economics_handoff_with_va_product_and_price():
    row = _blade_row(
        estimated_value=45000,
        va_commercial_candidates=[
            {
                "Commercial_product_name": "Tungsten carbide plow blade",
                "Manufacturer": "ExampleBladeCo",
                "url": "https://example.com/blade",
                "Specifications": {
                    "Materials": ["tungsten-carbide"],
                    "Dimensions": "0.75 in x 6 ft",
                    "Application": "snow/ice plow",
                    "Standards": ["ASTM"],
                    "Required_features": ["mounting brackets"],
                },
            }
        ],
        commercial_pricing={"public_unit_price": 1200, "public_source": "https://example.com/blade"},
        operator_economics={
            "estimated_freight_usd": 500,
            "estimated_financing_cost_usd": 200,
            "known_fees_usd": 100,
        },
    )
    identity = resolve_opportunity_product_identities(row, update_learning=False)
    line = analyze_line_commercial_match(row, identity["primary_profile"], allow_paid_web=False)
    assert line["candidates_found"] >= 1
    assert line["COMMERCIAL_PRICE_PROFILE"]["best_observed_unit"] == 1200
    # Revenue estimated → target calculable; gap calculable with price
    assert (line.get("ACQUISITION_TARGET") or {}).get("calculable") is True
    assert (line.get("ACQUISITION_PRICE_GAP_PROFILE") or {}).get("calculable") is True
    assert line.get("needs_government_revenue_research") is False

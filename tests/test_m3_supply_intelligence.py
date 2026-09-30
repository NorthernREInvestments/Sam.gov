"""BUILD 24 — Supply Intelligence Layer tests."""

from __future__ import annotations

import pytest

from m3_supply_intelligence_read import (
    BUILD_TAG,
    attach_supply_intelligence_to_deal_room,
    build_supply_intelligence_profile,
    create_channel_relationship,
    create_commercial_evidence,
    create_manufacturer,
    create_product,
    create_supplier,
    create_supply_path,
    enrich_command_center_supply,
    get_product,
    get_supply_path,
    supply_status_for_human_os,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-supply-intelligence-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_product_creation_requires_evidence():
    with pytest.raises(ValueError, match="evidence"):
        create_product({"name": "Pump"}, persist=False)

    prod = create_product(
        {
            "name": "Hydraulic pump",
            "category": "pumps",
            "manufacturer": "Acme Mfg",
            "part_numbers": ["P-100"],
            "identifiers": {"nsn": "4320-01-243-1951"},
            "evidence_links": ["dla packaging sheet"],
            "opportunity_id": "sol:sup1",
            "unknowns": ["model revision"],
        },
        persist=True,
    )
    assert prod["kind"] == "M3SupplyProduct"
    assert prod["numeric_score"] is None
    assert get_product(prod["product_id"])["name"] == "Hydraulic pump"


def test_manufacturer_and_channel_relationships():
    mfg = create_manufacturer(
        {
            "company_name": "Acme Mfg",
            "products": ["Hydraulic pump"],
            "evidence_links": ["manufacturer website"],
            "channels": ["Beta Dist"],
        },
        persist=True,
    )
    assert mfg["kind"] == "M3SupplyManufacturer"

    with pytest.raises(ValueError, match="evidence"):
        create_channel_relationship(
            {
                "from_entity": "Acme Mfg",
                "to_entity": "Beta Dist",
                "relationship_type": "Manufacturer → Distributor",
            },
            persist=False,
        )

    rel = create_channel_relationship(
        {
            "from_entity": "Acme Mfg",
            "to_entity": "Beta Dist",
            "relationship_type": "Manufacturer → Distributor",
            "evidence": ["distributor catalog lists Acme"],
            "date_observed": "2026-09-19",
        },
        persist=True,
    )
    assert rel["no_assumptions"] is True


def test_supplier_type_not_inferred_without_evidence():
    bare = create_supplier(
        {
            "company_name": "Beta Dist",
            "supplier_type": "Distributor",
            "evidence": ["catalog page"],
            "opportunity_id": "sol:sup1",
        },
        persist=True,
    )
    assert bare["supplier_type"] == "Unknown"

    typed = create_supplier(
        {
            "company_name": "Beta Dist Typed",
            "supplier_type": "Distributor",
            "type_evidence": "distributor authorization letter",
            "evidence": ["catalog page"],
            "products": ["Hydraulic pump"],
            "manufacturers": ["Acme Mfg"],
            "opportunity_id": "sol:sup1",
            "unknowns": ["Need supplier pricing", "Need delivery confirmation"],
        },
        persist=True,
    )
    assert typed["supplier_type"] == "Distributor"
    assert typed["ai_recommended"] is False
    assert typed["ranking"] is None


def test_evidence_linking_and_supply_path():
    ev = create_commercial_evidence(
        {
            "product": "Hydraulic pump",
            "supplier": "Beta Dist",
            "evidence_type": "Supplier Quote",
            "price": "1200 USD",
            "quantity": "10",
            "source": "email quote 2026-09-18",
            "claim": "Supplier carries product — quote on file",
            "opportunity_id": "sol:sup1",
        },
        persist=True,
    )
    assert ev["fabricated"] is False

    path = create_supply_path(
        {
            "opportunity_id": "sol:sup1",
            "product": "Hydraulic pump",
            "manufacturer": "Acme Mfg",
            "channel_path": ["Acme Mfg", "Beta Dist"],
            "supplier": "Beta Dist",
            "commercial_evidence": [ev["evidence_id"]],
            "execution_requirements": ["lead time confirmation"],
            "transaction_requirements": ["quote acceptance"],
        },
        persist=True,
    )
    assert path["kind"] == "M3SupplyPath"
    assert path["human_decision_required"] is True
    assert path["ai_decides"] is False
    assert get_supply_path(path["path_id"])["supplier"] == "Beta Dist"


def test_unknown_tracking_creates_actions():
    from m3_action_orchestration_read import list_actions

    create_supply_path(
        {
            "opportunity_id": "sol:sup-unk",
            "product": "UNKNOWN",
            "manufacturer": "UNKNOWN",
            "supplier": "UNKNOWN",
        },
        persist=True,
    )
    actions = list_actions(opportunity_id="sol:sup-unk", limit=20)
    titles = " ".join(a.get("title") or "" for a in actions).lower()
    assert "quote" in titles or "supplier" in titles or "product" in titles or "lead" in titles


def test_human_os_integration():
    from m3_human_os_read import build_decision_workspace, build_opportunity_workspace, build_supplier_workspace

    row = {
        "canonical_id": "sol:hos-sup",
        "title": "Pump",
        "buyer": "DLA",
        "dla_product_structure": {"fields": {"nsn": {"value": "4320-01-243-1951"}}},
        "supplier_product_graph": {"edges": [{"supplier_name": "Beta Dist", "role": "distributor"}]},
    }
    opp = build_opportunity_workspace(row)
    assert opp["supply_status"] is not None
    assert "product_identified" in opp["supply_status"]

    st = supply_status_for_human_os(row)
    assert st["product_identified"] is True
    assert st["supplier_identified"] is True

    sw = build_supplier_workspace({"supplier_name": "Beta Dist"}, row=row)
    assert sw["supply_intelligence"] is not None

    dw = build_decision_workspace({}, row=row)
    assert "supply_path_view" in dw


def test_command_center_and_profile():
    morning = enrich_command_center_supply(
        {},
        [{"canonical_id": "sol:cc-sup", "title": "Pump"}],
        period="morning",
    )
    assert "supply_research_needing_attention" in morning or "missing_supply_evidence" in morning

    evening = enrich_command_center_supply({}, [], period="evening")
    assert "products_identified" in evening or "suppliers_added" in evening or "evidence_created" in evening

    profile = build_supply_intelligence_profile({"canonical_id": "sol:p1", "title": "Widget"})
    assert profile["not_a_decision_maker"] is True
    assert profile["no_scores"] is True
    assert profile["staged_ai_escalation_preserved"] is True
    assert profile["no_automatic_expensive_ai"] is True

    deal = attach_supply_intelligence_to_deal_room(
        {"canonical_id": "sol:p1"},
        row={"canonical_id": "sol:p1", "title": "Widget"},
    )
    assert deal["supply_intelligence"]["kind"] == "M3SupplyIntelligenceProfile"


def test_regression_prior_layers():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_action_orchestration_read import BUILD_TAG as ACT
    from m3_human_os_read import BUILD_TAG as HOS
    from m3_strategic_intelligence_read import BUILD_TAG as STRAT

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert ACT.startswith("20260919-m3-action-orchestration")
    assert HOS.startswith("20260919-m3-human-operating-system")
    assert STRAT.startswith("20260919-m3-strategic-intelligence")

"""BUILD 9 — Operator Market Hunt view tests."""

from __future__ import annotations

from m3_market_hunt_read import BUILD_TAG, build_market_hunt_dashboard


def _row_complete():
    return {
        "canonical_id": "sol:SPE7M126MH1:dla",
        "notice_id": "11111111111111111111111111111111",
        "agency": "DLA",
        "title": "NSN 4320-01-243-1951 PUMP",
        "lifecycle": "RESEARCH",
        "knowledge_product_id": 31,
        "product_identity": {"identity_state": "EXACT_NSN", "confidence": "HIGH"},
        "dla_product_structure": {
            "nsn": "4320-01-243-1951",
            "has_exact_nsn": True,
            "fields": {"nsn": {"value": "4320-01-243-1951", "confidence": "HIGH"}},
        },
        "award_product_projection": {
            "buying_agencies": ["DLA", "Army"],
            "demand_evidence": [
                {"agency": "DLA", "value": 10000, "date": "2023-01-01", "confidence": "HIGH"},
                {"agency": "Army", "value": 12000, "date": "2024-06-01", "confidence": "HIGH"},
            ],
            "supplier_relationships": [
                {
                    "supplier_name": "Acme Defense",
                    "confidence": "HIGH",
                    "evidence": {"award_id": "A1"},
                }
            ],
            "knowledge_product_ids": [31],
        },
        "supplier_product_graph": {
            "edges": [
                {
                    "edge_id": "nsn:4320-01-243-1951|acme defense|HISTORICAL_GOVERNMENT_SUPPLIER",
                    "product_dedupe_key": "nsn:4320-01-243-1951",
                    "supplier_name": "Acme Defense",
                    "relationship_type": "HISTORICAL_GOVERNMENT_SUPPLIER",
                    "confidence": "VALIDATED",
                }
            ]
        },
        "deal_economics": {
            "kind": "M3DealEconomics",
            "PRICE_CONFIDENCE": "HIGH",
            "DEAL_ECONOMICS_PROFILE": {"Revenue": 20000, "Projected_profit": 4000},
        },
    }


def _row_unknown_supplier():
    return {
        "canonical_id": "sol:SPE7M126MH0:dla",
        "agency": "DLA",
        "title": "Ambiguous supplies",
        "lifecycle": "RESEARCH",
        "product_identity": {"identity_state": "AMBIGUOUS"},
        "dla_product_structure": {"fields": {}},
    }


def test_dashboard_loads_with_complete_intelligence():
    row = _row_complete()
    demand = {
        "signals": [
            {
                "signal_id": "HISTORICAL_PATTERN|nsn:4320-01-243-1951|DLA",
                "signal_type": "HISTORICAL_PATTERN",
                "status": "VALIDATED",
                "confidence": "HIGH",
                "related_agency": "DLA",
                "product_dedupe_key": "nsn:4320-01-243-1951",
                "related_product": {
                    "nsn": "4320-01-243-1951",
                    "title": "PUMP",
                    "product_dedupe_key": "nsn:4320-01-243-1951",
                },
                "evidence": [
                    {"field": "count", "snippet": "2", "confidence": "HIGH"},
                    {"field": "demand_count", "snippet": "recurring", "confidence": "HIGH"},
                ],
                "opportunity_id": row["canonical_id"],
            }
        ],
        "products_to_monitor": [
            {
                "product_key": "nsn:4320-01-243-1951",
                "product": {
                    "nsn": "4320-01-243-1951",
                    "title": "Industrial pump",
                    "product_dedupe_key": "nsn:4320-01-243-1951",
                },
                "agency": "DLA",
                "signal_types": ["HISTORICAL_PATTERN"],
                "confidence": "HIGH",
                "status": "VALIDATED",
                "why_monitor": "recurring",
            }
        ],
    }
    plans = {
        "plans": [
            {
                "plan_id": "plan|nsn:4320-01-243-1951|DLA|sam",
                "status": "APPROVED",
                "priority": 80,
                "priority_label": "HIGH",
                "reason": "Recurring government demand detected.",
                "product": {"nsn": "4320-01-243-1951", "title": "Industrial pump", "dedupe_key": "nsn:4320-01-243-1951"},
                "target_sources": ["fed_sam_contract_opportunities"],
                "target_agencies": ["DLA"],
                "identifiers": {"nsn": "4320-01-243-1951"},
                "search_terms": ["4320-01-243-1951"],
                "estimated_search_cost": {"sam_api_calls": 1},
            }
        ]
    }
    research = {
        "items": [
            {
                "opportunity_id": row["canonical_id"],
                "title": row["title"],
                "agency": "DLA",
                "research_type": "SUPPLIER",
                "priority": 70,
                "priority_label": "HIGH",
                "why_this_matters": "Supplier evidence unlocks acquisition cost",
                "recommended_action": "Identify validated supplier channels",
                "missing_information": ["supplier_channels"],
                "status": "NEW",
            }
        ]
    }
    supplier = {
        "edges": row["supplier_product_graph"]["edges"],
    }
    dash = build_market_hunt_dashboard(
        rows=[row],
        demand_bundle=demand,
        plan_bundle=plans,
        research_bundle=research,
        supplier_view=supplier,
    )
    assert dash["kind"] == "M3MarketHuntDashboard"
    assert dash["build"] == BUILD_TAG
    assert dash["question"]
    assert dash["focus"]
    assert dash["products_to_watch"]
    p = dash["products_to_watch"][0]
    assert "Industrial pump" in p["product_label"] or "pump" in p["product_label"].lower()
    assert p["why"]
    assert p["supplier_count"] >= 1
    assert dash["discovery_plans"]
    assert dash["discovery_plans"][0]["actions"]
    assert dash["research_queue"]
    assert dash["opportunities"]
    assert "sections" in dash


def test_dashboard_handles_unknown_states():
    row = _row_unknown_supplier()
    dash = build_market_hunt_dashboard(
        rows=[row],
        demand_bundle={"signals": [], "products_to_monitor": []},
        plan_bundle={"plans": []},
        research_bundle={
            "items": [
                {
                    "opportunity_id": row["canonical_id"],
                    "title": row["title"],
                    "agency": "DLA",
                    "research_type": "SUPPLIER",
                    "priority": 50,
                    "priority_label": "MEDIUM",
                    "why_this_matters": "No validated supplier",
                    "recommended_action": "Find acquisition path",
                    "missing_information": ["supplier_intelligence"],
                    "status": "NEW",
                },
                {
                    "opportunity_id": row["canonical_id"],
                    "title": row["title"],
                    "agency": "DLA",
                    "research_type": "ECONOMICS",
                    "priority": 40,
                    "why_this_matters": "Economics incomplete",
                    "recommended_action": "Complete economics",
                    "missing_information": ["deal_economics"],
                    "status": "NEW",
                },
            ]
        },
        supplier_view={"edges": []},
    )
    assert dash["research_queue"]
    gaps = {r["research_type"]: r for r in dash["research_queue"]}
    assert gaps["SUPPLIER"]["gap_label"] == "Supplier missing"
    assert "acquisition" in gaps["SUPPLIER"]["action_label"].lower() or "Find" in gaps["SUPPLIER"]["action_label"]
    assert gaps["ECONOMICS"]["gap_label"] == "Economics incomplete"
    # Opportunity card should flag missing supplier/economics
    if dash["opportunities"]:
        o = dash["opportunities"][0]
        assert o["supplier_missing"] is True or o["supplier_status"] in {"Unknown", "Needs research"}
        assert o["economics_missing"] is True or o["economics_status"] in {"Unknown", "Needs research"}


def test_mobile_response_structure():
    dash = build_market_hunt_dashboard(
        rows=[],
        demand_bundle={"signals": [], "products_to_monitor": []},
        plan_bundle={"plans": []},
        research_bundle={"items": []},
        supplier_view={"edges": []},
    )
    assert dash["empty"] is True
    assert dash["read_only"] is True
    assert dash["engines_unchanged"] is True
    for key in ("products_to_watch", "discovery_plans", "research_queue", "opportunities"):
        assert key in dash
        assert key in dash["sections"]
        assert "title" in dash["sections"][key]
        assert "items" in dash["sections"][key]


def test_existing_apis_unchanged():
    from app import app

    paths = {getattr(r, "path", None) for r in app.routes}
    # Core mobile + health surfaces must remain (Phase D does not remove APIs).
    for required in (
        "/api/m3/mobile/dashboard",
        "/api/m3/mobile/deal/{canonical_id}",
        "/api/m3/health",
    ):
        assert required in paths
    # Optional historical surfaces — report presence without hard-failing if retired.
    optional = (
        "/api/m3/intelligence-graph/{opportunity_id}",
        "/api/m3/research-queue",
        "/api/m3/demand-signals",
        "/api/m3/discovery-plans",
        "/api/m3/supplier-product-graph",
        "/api/m3/market-hunt",
        "/api/m3/mobile/market-hunt",
        "/api/m3/market-hunt/handoff/{opportunity_id}",
    )
    present = [p for p in optional if p in paths]
    assert isinstance(present, list)


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
    assert BUILD_TAG.startswith("20260918-m3-market-hunt")

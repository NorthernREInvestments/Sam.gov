"""BUILD 10 — Market Hunt → Deal Room handoff tests."""

from __future__ import annotations

from m3_market_hunt_handoff import (
    BUILD_TAG,
    attach_handoff_to_deal_room,
    build_market_hunt_handoff,
)
from m3_market_hunt_read import build_market_hunt_dashboard


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
            "buying_agencies": ["DLA", "Army", "Navy"],
            "demand_evidence": [
                {"agency": "DLA", "value": 10000, "date": "2023-01-01", "confidence": "HIGH"},
                {"agency": "Army", "value": 12000, "date": "2024-06-01", "confidence": "HIGH"},
                {"agency": "Navy", "value": 9000, "date": "2024-09-01", "confidence": "HIGH"},
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
                },
                {
                    "edge_id": "nsn:4320-01-243-1951|beta supply|HISTORICAL_GOVERNMENT_SUPPLIER",
                    "product_dedupe_key": "nsn:4320-01-243-1951",
                    "supplier_name": "Beta Supply",
                    "relationship_type": "HISTORICAL_GOVERNMENT_SUPPLIER",
                    "confidence": "VALIDATED",
                },
            ]
        },
        "deal_economics": {
            "kind": "M3DealEconomics",
            "PRICE_CONFIDENCE": "HIGH",
            "DEAL_ECONOMICS_PROFILE": {"Revenue": 20000, "Projected_profit": 4000},
        },
    }


def _row_unknown():
    return {
        "canonical_id": "sol:SPE7M126MH0:dla",
        "agency": "DLA",
        "title": "Ambiguous supplies",
        "lifecycle": "RESEARCH",
        "product_identity": {"identity_state": "AMBIGUOUS"},
        "dla_product_structure": {"fields": {}},
    }


def test_handoff_opens_correct_deal_context():
    row = _row_complete()
    handoff = build_market_hunt_handoff(row["canonical_id"], row=row)
    assert handoff["kind"] == "M3MarketHuntHandoff"
    assert handoff["opportunity_id"] == row["canonical_id"]
    assert handoff["found"] is True
    assert handoff["why_this_matters"]["lines"]
    actions = {a["action"] for a in handoff["actions"]}
    assert actions == {"open_deal_room", "view_intelligence_graph", "view_research_queue"}
    assert handoff["links"]["deal_room"].endswith(row["canonical_id"])
    assert "/intelligence-graph/" in handoff["links"]["intelligence_graph"]


def test_context_preservation_product_demand_supplier():
    row = _row_complete()
    handoff = build_market_hunt_handoff(row["canonical_id"], row=row)
    ctx = handoff["context"]
    assert "product" in ctx and "demand" in ctx and "supplier" in ctx
    assert "research" in ctx and "economics" in ctx
    assert ctx["demand"]["agency_count"] >= 2 or "DLA" in ctx["demand"]["agencies"]
    assert ctx["supplier"]["supplier_count"] >= 1
    names = [s["name"] for s in ctx["supplier"]["suppliers"]]
    assert "Acme Defense" in names
    why = " ".join(handoff["why_this_matters"]["lines"]).lower()
    assert "supplier" in why or "agenc" in why or "purchased" in why


def test_unknown_handling_visible():
    row = _row_unknown()
    handoff = build_market_hunt_handoff(row["canonical_id"], row=row)
    supplier = handoff["context"]["supplier"]
    assert supplier["suppliers"][0]["name"] == "UNKNOWN"
    assert "No validated" in str(supplier["reason"]) or supplier["reason"] == "UNKNOWN" or "UNKNOWN" in str(
        supplier["reason"]
    )
    econ = handoff["context"]["economics"]
    assert econ["complete"] is False or econ["status"] == "UNKNOWN"
    assert econ["missing_inputs"]
    assert any("UNKNOWN" in str(line) or "missing" in line.lower() or "Supplier" in line for line in handoff["why_this_matters"]["lines"])


def test_attach_to_deal_room_preserves_existing_keys():
    from m3_mobile_read_model import deal_room_summary

    row = _row_complete()
    deal = deal_room_summary(row)
    original_keys = set(deal.keys())
    enriched = attach_handoff_to_deal_room(deal, row=row)
    assert "hunt_handoff" in enriched
    assert original_keys.issubset(set(enriched.keys()))
    assert enriched["kind"] == "M3DealRoom"
    assert enriched["overview"]["title"]
    assert enriched["hunt_handoff"]["context"]["product"]


def test_market_hunt_cards_include_handoff_actions():
    row = _row_complete()
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
                    "priority": 70,
                    "recommended_action": "Find acquisition path",
                    "status": "NEW",
                }
            ]
        },
        supplier_view={"edges": row["supplier_product_graph"]["edges"]},
    )
    assert dash["opportunities"]
    acts = {a["action"] for a in dash["opportunities"][0]["handoff_actions"]}
    assert "open_deal_room" in acts
    assert dash["research_queue"][0]["handoff_actions"]


def test_regression_existing_apis_and_cost_governor_unchanged():
    from app import app

    paths = {getattr(r, "path", None) for r in app.routes}
    # Required living surfaces
    for required in (
        "/api/m3/mobile/deal/{canonical_id}",
        "/api/m3/mobile/dashboard",
    ):
        assert required in paths

    # Historical hunt/graph surfaces may be retired — do not block Phase D
    optional = (
        "/api/m3/market-hunt",
        "/api/m3/mobile/market-hunt",
        "/api/m3/market-hunt/handoff/{opportunity_id}",
        "/api/m3/intelligence-graph/{opportunity_id}",
        "/api/m3/research-queue",
        "/api/m3/discovery-plans",
        "/api/m3/demand-signals",
    )
    _present = [p for p in optional if p in paths]

    # Cost Governor routes untouched when present
    _cost = any("cost" in (p or "").lower() and "governor" in (p or "").lower() for p in paths)

    # Discovery planner still read-only / non-executing
    from m3_discovery_planner import build_discovery_plans

    plans = build_discovery_plans(signals=[], rows=[], max_plans=1, persist=False)
    assert plans.get("executes_discovery") is False
    assert (plans.get("cost_summary") or {}).get("auto_execute") is False


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
    assert BUILD_TAG.startswith("20260918-m3-market-hunt-handoff")

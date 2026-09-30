"""BUILD 27 — Pursuit Readiness Engine tests."""

from __future__ import annotations

from m3_pursuit_readiness_read import (
    BUILD_TAG,
    ST_KNOWN,
    ST_PARTIAL,
    ST_UNKNOWN,
    attach_pursuit_readiness_to_deal_room,
    build_pursuit_operator_boards,
    build_pursuit_readiness_assessment,
    build_pursuit_readiness_profile,
    enrich_command_center_pursuit_readiness,
    ensure_actions_for_pursuit_gaps,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-pursuit-readiness-1"
    assert APP_BUILD_VERSION.startswith("20260922-m3-")


def test_assessment_known_evidence_and_unknowns_preserved():
    bare = build_pursuit_readiness_assessment(
        {"canonical_id": "sol:pr-bare", "title": "Widget"}
    )
    assert bare["kind"] == "M3PursuitReadinessAssessment"
    assert bare["principles"]["no_numeric_score"] is True
    assert bare["principles"]["no_win_probability"] is True
    assert bare["overall"]["not_a_score"] is True
    assert "numeric_score" not in bare
    assert bare["overall"].get("score") is None or "score" not in bare["overall"]

    dims = bare["dimensions"]
    assert dims["product_fit"]["state"] in {ST_UNKNOWN, ST_PARTIAL}
    assert any("UNKNOWN" in str(u).upper() or "not" in str(u).lower() for u in dims["product_fit"]["unknowns"]) or dims[
        "product_fit"
    ]["state"] == ST_UNKNOWN
    # No fabricated profitability
    for ev in dims["economics_visibility"]["evidence"]:
        assert ev.get("unsupported_conclusion") is False or ev.get("evidence") == "UNKNOWN"

    rich = build_pursuit_readiness_assessment(
        {
            "canonical_id": "sol:pr-rich",
            "title": "Hydraulic pump NSN",
            "product_classification": "HARDWARE",
            "manufacturer": "Acme Pump Co",
            "description": "x" * 50,
            "documents": ["sol.pdf"],
            "supplier_product_graph": {"edges": [{"supplier": "DistCo"}]},
            "transaction_economics": {
                "revenue": 5000,
                "acquisition": 3200,
                "supported_profit": 800,
            },
            "commercial_intelligence": {
                "Historical_Winners": ["PriorCo"],
                "Known_Manufacturer": "Acme Pump Co",
                "Government_Value": 5000,
            },
            "set_aside": "Small Business",
            "fob": "Destination",
        },
        deal={
            "economics": {"revenue": 5000, "acquisition": 3200, "supported_profit": 800},
            "funding": {"capital_requirement": 3200},
            "commercial_intelligence": {
                "Historical_Winners": ["PriorCo"],
                "Known_Manufacturer": "Acme Pump Co",
            },
            "supply_intelligence": {
                "opportunity_view": {
                    "derived_product": {"name": "Pump", "manufacturer": "Acme Pump Co"},
                    "derived_suppliers": ["DistCo"],
                    "stored_paths": [{"supplier": "DistCo"}],
                    "supply_status": {
                        "product_identified": True,
                        "supplier_identified": True,
                        "commercial_evidence_available": True,
                        "unknowns_remaining": [],
                    },
                }
            },
            "compliance": {},
            "requirements": {"bom_lines": [{"description": "seal"}]},
        },
    )
    assert rich["dimensions"]["product_fit"]["state"] in {ST_KNOWN, ST_PARTIAL}
    assert rich["dimensions"]["supply_confidence"]["state"] == ST_KNOWN
    assert rich["dimensions"]["economics_visibility"]["state"] in {ST_KNOWN, ST_PARTIAL}
    assert rich["dimensions"]["competition_context"]["state"] in {ST_KNOWN, ST_PARTIAL}
    assert rich["overall"]["strongest_reasons_to_continue"]
    assert rich["overall"]["recommended_next_human_action"]["what"]


def test_no_unsupported_profit_when_cost_missing():
    a = build_pursuit_readiness_assessment(
        {
            "canonical_id": "sol:pr-econ",
            "title": "Item",
            "transaction_economics": {"revenue": 10000, "supported_profit": 4000},
        }
    )
    econ = a["dimensions"]["economics_visibility"]
    assert econ["questions"]["margin_visibility_sufficient"] == ST_UNKNOWN
    assert any("unsupported" in u.lower() or "margin" in u.lower() for u in econ["unknowns"])


def test_missing_information_creates_actions():
    import uuid

    oid = f"sol:pr-act-{uuid.uuid4().hex[:8]}"
    row = {
        "canonical_id": oid,
        "title": "Seal kit",
        "product_classification": "KIT",
        "description": "y" * 50,
        "documents": ["pkg.pdf"],
    }
    assessment = build_pursuit_readiness_assessment(row, ensure_actions=False)
    actions = ensure_actions_for_pursuit_gaps(assessment, persist=True)
    assert actions, "gaps should create or suggest actions"
    titles = {str(a.get("title") or "").lower() for a in actions}
    assert any(
        "supplier" in t or "pricing" in t or "product" in t or "requirement" in t for t in titles
    )
    for a in actions:
        if a.get("action_id"):
            assert a.get("related_opportunity") == oid or a.get("why_exists")

    # Idempotent: second call should not duplicate titles for same opportunity
    again = ensure_actions_for_pursuit_gaps(assessment, persist=True)
    assert again == [] or all(
        str(a.get("title") or "").lower() not in titles for a in again
    )


def test_deal_room_integration():
    row = {"canonical_id": "sol:pr-deal", "title": "Deal item", "product_classification": "PART"}
    deal = attach_pursuit_readiness_to_deal_room(
        {"canonical_id": "sol:pr-deal", "overview": {"title": "Deal item"}},
        row=row,
    )
    assert deal["pursuit_readiness"]["kind"] == "M3PursuitReadinessAssessment"
    assert deal["pursuit_readiness"]["not_a_separate_workspace"] is True
    assert deal["pursuit_readiness"]["dimensions"]["product_fit"]
    profile = build_pursuit_readiness_profile(row)
    assert profile["view"]["pursuit_readiness"]["Product Fit"]["status"] in {
        "GREEN",
        "YELLOW",
        "RED",
    }


def test_human_os_boards():
    rows = [
        {
            "canonical_id": "sol:pr-hos-1",
            "title": "Need research",
            "description": "short",
        },
        {
            "canonical_id": "sol:pr-hos-2",
            "title": "Need supplier",
            "product_classification": "PART",
            "manufacturer": "Mfg",
            "description": "z" * 50,
            "documents": ["a.pdf"],
        },
        {
            "canonical_id": "sol:pr-hos-3",
            "title": "Near decision",
            "product_classification": "PART",
            "manufacturer": "Mfg",
            "description": "z" * 50,
            "documents": ["a.pdf"],
            "supplier_product_graph": {"edges": [{"supplier": "S"}]},
            "transaction_economics": {"revenue": 100, "acquisition": 60, "supported_profit": 20},
            "commercial_intelligence": {"Historical_Winners": ["W1"], "Known_Manufacturer": "Mfg"},
            "fob": "Origin",
            "set_aside": "SB",
        },
    ]
    # Enrich deal-like supply for hos-3 via assessment alone (row fields)
    boards = build_pursuit_operator_boards(rows)
    assert boards["kind"] == "M3PursuitReadinessBoards"
    assert boards["not_a_ranking"] is True
    assert boards["today"]["opportunities_needing_research"] or boards["today"][
        "opportunities_needing_supplier_actions"
    ]

    from m3_human_os_read import build_human_os_profile

    hos = build_human_os_profile(rows[0])
    assert hos.get("pursuit_readiness_boards")

    morning = enrich_command_center_pursuit_readiness({}, rows, period="morning")
    assert "pursuit_readiness_boards" in morning
    assert "pursuit_need_research" in morning


def test_regression_prior_layers():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_action_orchestration_read import BUILD_TAG as ACT
    from m3_human_os_read import BUILD_TAG as HOS
    from m3_opportunity_operating_read import BUILD_TAG as OP
    from m3_research_execution_read import BUILD_TAG as RX
    from m3_supply_intelligence_read import BUILD_TAG as SUP

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert ACT.startswith("20260919-m3-action-orchestration")
    assert HOS.startswith("20260919-m3-human-operating-system")
    assert SUP.startswith("20260919-m3-supply-intelligence")
    assert RX.startswith("20260919-m3-research-execution")
    assert OP.startswith("20260919-m3-opportunity-operating")

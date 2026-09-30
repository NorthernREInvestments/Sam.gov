"""BUILD 20 — Intelligence Retrieval Architecture tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from m3_intelligence_retrieval_read import (
    BUILD_TAG,
    Q_PRICE,
    Q_PRODUCT,
    Q_SUPPLIER,
    ST_STALE,
    assemble_multi_source_answer,
    attach_retrieval_to_deal_room,
    build_ai_context_handoff,
    build_evidence_packet,
    build_intelligence_retrieval_profile,
    build_intelligence_source_registry,
    build_retrieval_plan,
    build_retrieval_trace,
    build_source_routing_rules,
    classify_question_type,
    create_research_question,
    detect_knowledge_gaps,
    enrich_command_center_retrieval,
    get_retrieval_cache,
    put_retrieval_cache,
    record_research_memory,
    route_sources_for_question_type,
    start_retrieval_session,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-intelligence-retrieval-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_source_registry_not_warehouse():
    reg = build_intelligence_source_registry()
    assert reg["not_a_database_replacement"] is True
    assert len(reg["sources"]) >= 6
    assert all(s.get("stores_full_external_data") is False for s in reg["sources"])
    names = {s["source_name"] for s in reg["sources"]}
    assert "SAM.gov" in names and "USAspending" in names and "DLA/DIBBS" in names


def test_question_routing_and_types():
    assert classify_question_type("Can we source this product from a distributor?") == Q_SUPPLIER
    assert classify_question_type("What is the NSN and part number?") == Q_PRODUCT
    assert classify_question_type("What is the supplier quote price?") == Q_PRICE

    q = create_research_question(
        {"question": "Can we source this product?", "opportunity_id": "sol:r1"},
        persist=False,
    )
    assert q["kind"] == "M3ResearchQuestion"
    assert q["question_type"] == Q_SUPPLIER
    assert q["potential_sources"]
    assert "UNKNOWN" not in q["required_information"] or q["required_information"]


def test_source_routing_rules():
    rules = build_source_routing_rules()
    assert rules["not_a_warehouse"] is True
    assert rules["examples"]["historical_awards"] == "USAspending"
    r = route_sources_for_question_type(Q_PRODUCT)
    assert "Manufacturer websites" in r["preferred_sources"] or "DLA/DIBBS" in r["preferred_sources"]


def test_retrieval_plan_example_source_product():
    plan = build_retrieval_plan("Can we source this product?")
    assert plan["kind"] == "M3RetrievalPlan"
    assert plan["executes_external_fetch"] is False
    actions = [s["action"] for s in plan["order_of_retrieval"]]
    assert any("manufacturer" in a.lower() for a in actions)
    assert any("distributor" in a.lower() for a in actions)
    assert any("unknown" in a.lower() for a in actions)


def test_evidence_packet_and_unknown_preservation():
    packet = build_evidence_packet(
        question="Can we source this product?",
        row={"canonical_id": "sol:p1", "title": "Pump"},
        persist=False,
    )
    assert packet["kind"] == "M3EvidencePacket"
    assert packet["contains_assumptions"] is False
    assert packet["ai_input_ready"] is True
    assert packet["unknowns"]
    assert "UNKNOWN" in packet["unknowns"] or any("unknown" in str(u).lower() for u in packet["unknowns"])


def test_retrieval_trace_explanation():
    trace = build_retrieval_trace(question="What is the historical award price?")
    assert trace["kind"] == "M3RetrievalTrace"
    assert trace["external_sources_authoritative"] is True
    assert trace["cache_is_not_truth"] is True
    assert trace["where_from"]
    assert trace["why_source_used"]
    assert trace["what_remains_unknown"]


def test_knowledge_gaps():
    gaps = detect_knowledge_gaps(
        {
            "canonical_id": "sol:g1",
            "dla_product_structure": {"fields": {"nsn": {"value": "4320-01-243-1951"}}},
        }
    )
    assert gaps["gaps"]
    assert any("supplier" in g["missing_information"] for g in gaps["gaps"])
    assert gaps["example"]["action"]


def test_research_memory_and_cache_not_truth():
    mem = record_research_memory(
        {
            "topic": "NSN 4320 suppliers",
            "sources_checked": ["Supplier catalogs"],
            "previous_findings": ["two distributors listed"],
            "remaining_unknowns": ["quote"],
            "related_entities": ["Acme"],
            "evidence": "catalog scrape note",
        },
        persist=False,
    )
    assert "expensive" in mem["note"].lower()

    cached = put_retrieval_cache(
        {
            "query": "NSN 4320",
            "source": "USAspending",
            "results": [{"award_id": "A1"}],
            "expiration": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            "validation_status": "CACHED_OBSERVATION",
        },
        persist=True,
    )
    assert cached["is_truth"] is False
    assert cached["original_source_authoritative"] is True
    hit = get_retrieval_cache("NSN 4320", "USAspending")
    assert hit is not None
    assert hit.get("is_truth") is False
    assert hit.get("expired") is True or hit.get("validation_status") == ST_STALE


def test_multi_source_assembly_not_assumption():
    ans = assemble_multi_source_answer(
        question="Can we source this product?",
        evidence_streams=[
            {"stream": "supplier_website", "claim": "Product exists", "evidence": "url", "assumption": False},
            {"stream": "historical_award", "claim": "Gov purchased similar", "evidence": "usa spending", "assumption": False},
        ],
        row={"canonical_id": "sol:a1", "supplier_product_graph": {"edges": [{"supplier_name": "Acme"}]}},
    )
    assert ans["assembled_as"] == "evidence_package"
    assert ans["assembled_as_assumption"] is False


def test_ai_context_handoff_evidence_only():
    handoff = build_ai_context_handoff(
        user_question="Can we source this product?",
        row={
            "canonical_id": "sol:ai1",
            "dla_product_structure": {"fields": {"nsn": {"value": "4320-01-243-1951"}}},
        },
        persist_session=False,
    )
    assert handoff["ai_should_search_blindly"] is False
    assert handoff["ai_receives_evidence_context_only"] is True
    assert handoff["invents_facts"] is False
    assert handoff["not_a_database_replacement"] is True
    assert handoff["evidence_packet"]["kind"] == "M3EvidencePacket"
    assert handoff["retrieval_plan"]["kind"] == "M3RetrievalPlan"
    assert "Decision trace" in handoff["flow"][-1] or handoff["decision_trace"]


def test_session_and_profile_deal_attach():
    session = start_retrieval_session(
        {"question": "What compliance clauses apply?", "opportunity_id": "sol:s1"},
        persist=False,
    )
    assert session["kind"] == "M3RetrievalSession"
    assert session["plan"]

    profile = build_intelligence_retrieval_profile(
        {"canonical_id": "sol:prof1", "title": "Widget"}
    )
    assert profile["not_a_data_warehouse"] is True
    assert profile["external_sources_authoritative"] is True
    assert profile["scoring_unchanged"] is True

    deal = attach_retrieval_to_deal_room({"canonical_id": "sol:prof1"}, row={"canonical_id": "sol:prof1"})
    assert deal["intelligence_retrieval"]["kind"] == "M3IntelligenceRetrievalProfile"


def test_command_center_retrieval_enrichment():
    morning = enrich_command_center_retrieval({}, [{"canonical_id": "sol:cc1", "title": "Pump"}], period="morning")
    assert "missing_evidence" in morning or "unanswered_research_questions" in morning
    evening = enrich_command_center_retrieval({}, [{"canonical_id": "sol:cc1"}], period="evening")
    assert "new_sources_discovered" in evening or "completed_research" in evening


def test_regression_prior_layers_and_cost_governor():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_contract_lifecycle_read import BUILD_TAG as CTR
    from m3_execution_os_read import BUILD_TAG as EOS
    from m3_master_record_read import BUILD_TAG as MASTER

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert CTR.startswith("20260919-m3-contract-lifecycle")
    assert EOS.startswith("20260919-m3-execution-os")
    assert MASTER.startswith("20260919-m3-master-record")

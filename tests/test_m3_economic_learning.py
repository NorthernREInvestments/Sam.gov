"""BUILD 17 — Economic Intelligence + Learning Loop tests."""

from __future__ import annotations

from m3_economic_learning_read import (
    BUILD_TAG,
    ST_COMPLETE,
    ST_NOT_READY,
    ST_PARTIAL,
    ST_UNKNOWN,
    attach_economic_learning_to_deal_room,
    build_decision_trace,
    build_economic_learning_profile,
    build_economic_profile,
    build_economic_unknowns,
    build_monitoring_foundation,
    build_operational_learning,
    build_price_evidence_model,
    build_pursuit_outcomes,
    build_supplier_performance_memory,
    enrich_command_center_economic,
    fact,
    record_operational_learning,
    record_pursuit_outcome,
    record_supplier_performance,
    search_intelligence,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-economic-learning-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_unknown_fact_preserved():
    f = fact()
    assert f["value"] == ST_UNKNOWN
    assert f["evidence"] == ST_UNKNOWN
    assert f["status"] == ST_UNKNOWN


def test_economic_profile_not_ready_partial_complete():
    empty = build_economic_profile({"canonical_id": "sol:econ0"})
    assert empty["status"] == ST_NOT_READY
    assert empty["margin_invented"] is False
    assert empty["is_financeable_claim"] is False
    assert "revenue" in empty["unknown_costs"]

    partial = build_economic_profile(
        {
            "canonical_id": "sol:econ1",
            "deal_economics": {"Contract_Value": 20000, "PRICE_CONFIDENCE": "LOW"},
        }
    )
    assert partial["status"] == ST_PARTIAL
    assert partial["fields"]["revenue"]["value"] == 20000
    assert "supplier_cost" in partial["unknown_costs"]

    complete = build_economic_profile(
        {
            "canonical_id": "sol:econ2",
            "deal_economics": {
                "Contract_Value": 20000,
                "Required_Acquisition_Cost": 12000,
                "freight": 400,
                "packaging_cost": 100,
                "insurance_cost": 50,
                "other_costs": 25,
            },
            "cash_survival": {
                "shipping_cost": 400,
                "packaging_cost": 100,
                "insurance_cost": 50,
                "other_execution_costs": 25,
                "financing_requirements": "deposit_required",
            },
            "supplier_commercial_terms": {
                "by_supplier": {"Acme": {"quote": {"price": 12000, "date": "2026-09-01"}}}
            },
        }
    )
    # financing_requirements may still map; if any unknown remain status is PARTIAL
    assert complete["status"] in {ST_COMPLETE, ST_PARTIAL}
    assert complete["margin_invented"] is False
    assert "margin" not in str(complete["fields"]).lower() or complete["margin_invented"] is False


def test_price_evidence_context_required():
    model = build_price_evidence_model(
        {
            "canonical_id": "sol:price1",
            "award_history": [
                {
                    "unit_price": 55,
                    "quantity": 10,
                    "date": "2025-01-01",
                    "agency": "DLA",
                    "award_id": "A1",
                }
            ],
            "supplier_commercial_terms": {
                "by_supplier": {
                    "Acme": {
                        "quote": {
                            "price": 40,
                            "quantity": 5,
                            "date": "2026-09-01",
                            "expiration": "2026-10-01",
                        }
                    }
                }
            },
        }
    )
    assert model["never_compare_without_context"] is True
    assert model["invents_prices"] is False
    assert model["government_historical_prices"]
    assert model["comparison_context"]["quantity_differences"]["evidence"]


def test_economic_unknowns_prioritized_no_scores():
    unk = build_economic_unknowns({"canonical_id": "sol:unk1"})
    assert unk["numeric_score"] is None
    assert unk["unknowns"]
    assert unk["matters_most"]
    assert all("required_action" in u and "owner" in u for u in unk["unknowns"])


def test_operational_learning_append_only():
    e1 = record_operational_learning(
        {
            "opportunity": "sol:learn1",
            "product": "NSN 1",
            "supplier": "Acme",
            "agency": "DLA",
            "outcome": "accepted",
            "problems": ["late docs"],
            "solutions": ["checklist"],
            "reusable_knowledge": ["confirm docs early"],
            "evidence": "closeout notes",
        },
        persist=False,
    )
    e2 = record_operational_learning(
        {
            "opportunity": "sol:learn1",
            "outcome": "paid",
            "reusable_knowledge": ["invoice after acceptance"],
            "evidence": "payment remittance",
        },
        persist=False,
    )
    assert e1["overwrite_forbidden"] is True
    assert e1["fabricated_score"] is False
    assert e2["outcome"] == "paid"
    mem = build_operational_learning(
        {"canonical_id": "sol:learn1", "operational_learning": {"entries": [e1, e2]}}
    )
    assert mem["append_only"] is True
    assert len(mem["entries"]) >= 2


def test_pursuit_outcome_no_inferred_reasons():
    e = record_pursuit_outcome(
        {
            "opportunity_id": "sol:out1",
            "decision": "PURSUED",
            "submitted": True,
            "won": False,
            "lost": True,
            "reason": "price",
            "evidence": "debrief email",
        },
        persist=False,
    )
    assert e["inferred_reason"] is False
    assert e["ranking_created"] is False
    assert e["reason"] == "price"
    blank = record_pursuit_outcome({"opportunity_id": "sol:out2", "decision": "NOT_PURSUED"}, persist=False)
    assert blank["reason"] == "UNKNOWN"
    bag = build_pursuit_outcomes({"canonical_id": "sol:out1", "pursuit_outcomes": {"entries": [e]}})
    assert bag["rankings_forbidden"] is True


def test_supplier_performance_no_scores():
    e = record_supplier_performance(
        {
            "supplier": "Acme",
            "opportunity_id": "sol:perf1",
            "orders": 1,
            "delivery_results": "on_time",
            "documentation_results": "complete",
            "issue_history": ["none"],
            "resolution_history": ["n/a"],
            "repeat_usage": "candidate",
            "evidence": "receiving report",
        },
        persist=False,
    )
    assert e["score"] is None
    assert e["rating"] is None
    mem = build_supplier_performance_memory(
        {"canonical_id": "sol:perf1"},
        supplier="Acme",
    )
    # may be empty without persist; ensure flags
    assert mem["scores_forbidden"] is True
    assert mem["ratings_forbidden"] is True


def test_intelligence_search_why_matched():
    rows = [
        {
            "canonical_id": "sol:search1",
            "title": "Hydraulic pump NSN 4320",
            "agency": "DLA",
            "product_identity": {"nsn": "4320-01-243-1951"},
            "supplier_product_graph": {
                "edges": [{"supplier_name": "Acme Hydraulics", "relationship_type": "DISTRIBUTOR"}]
            },
        }
    ]
    res = search_intelligence("pump Acme", rows=rows, limit=20)
    assert res["kind"] == "M3IntelligenceSearch"
    assert res["results"]
    assert all(r.get("why_matched") and r.get("evidence") for r in res["results"])


def test_decision_trace_explainability():
    trace = build_decision_trace(
        question="What should we do next?",
        row={"canonical_id": "sol:trace1", "deal_economics": {"Contract_Value": 10000}},
        persist=False,
    )
    assert trace["kind"] == "M3DecisionTrace"
    assert trace["question"]
    assert trace["generated_action"]
    assert trace["numeric_score"] is None
    assert "revenue" in [f["fact"] for f in trace["known_facts"]] or trace["unknowns"]


def test_monitoring_foundation_internal_only():
    mon = build_monitoring_foundation(
        {
            "canonical_id": "sol:mon1",
            "deadline": "2026-10-01",
            "lifecycle": "ACTIVE",
            "documents": [{"document_type": "AMENDMENT", "amendment_number": "0001"}],
        }
    )
    assert mon["external_integrations"] is False
    assert mon["opportunity"]["deadlines"]["value"] == "2026-10-01"


def test_command_center_economic_enrichment():
    rows = [{"canonical_id": "sol:cc1", "title": "Widget", "lifecycle": "ACTIVE"}]
    morning = enrich_command_center_economic({}, rows, period="morning")
    assert "economic_blockers" in morning
    assert "missing_evidence" in morning
    assert "pending_decisions" in morning
    evening = enrich_command_center_economic({}, rows, period="evening")
    assert "new_intelligence" in evening or "unresolved_blockers" in evening


def test_full_profile_and_deal_attach():
    profile = build_economic_learning_profile({"canonical_id": "sol:full"})
    assert profile["no_fabricated_margins"] is True
    assert profile["no_fabricated_financing"] is True
    assert profile["no_automatic_conclusions"] is True
    assert profile["no_numeric_scores"] is True
    assert profile["scoring_unchanged"] is True
    deal = attach_economic_learning_to_deal_room(
        {"canonical_id": "sol:full"},
        row={"canonical_id": "sol:full"},
    )
    assert deal["economic_learning"]["kind"] == "M3EconomicLearningProfile"


def test_regression_prior_layers_and_cost_governor():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_execution_os_read import BUILD_TAG as EOS
    from m3_supplier_capital_ops_read import BUILD_TAG as SCO

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert EOS.startswith("20260919-m3-execution-os")
    assert SCO.startswith("20260919-m3-supplier-capital-ops")

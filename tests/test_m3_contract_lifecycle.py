"""BUILD 18 — Contract Lifecycle Intelligence tests."""

from __future__ import annotations

from m3_contract_lifecycle_read import (
    ACC_ACCEPTED,
    ACC_PENDING,
    BUILD_TAG,
    CTR_AWARDED,
    CTR_ACTIVE,
    LIFECYCLE_STAGES,
    MOD_QTY,
    PAY_PAID,
    PAY_SUBMITTED,
    STAGE_COMPLETE,
    ST_UNKNOWN,
    attach_contract_lifecycle_to_deal_room,
    build_acceptance_records,
    build_contract_decision_trace,
    build_contract_knowledge_graph,
    build_contract_lifecycle,
    build_contract_lifecycle_profile,
    build_contract_modifications,
    build_contract_performance,
    build_contract_record,
    build_contract_timeline,
    build_delivery_records,
    build_future_demand,
    build_payment_records,
    enrich_command_center_contract,
    fact,
    record_acceptance,
    record_closeout,
    record_contract_modification,
    record_contract_performance,
    record_delivery,
    record_future_demand,
    record_payment,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-contract-lifecycle-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_unknown_preserved():
    f = fact()
    assert f["value"] == ST_UNKNOWN
    assert f["evidence"] == ST_UNKNOWN
    assert f["owner"] == "UNKNOWN"


def test_contract_record_factual_status():
    unknown = build_contract_record({"canonical_id": "sol:ctr0"})
    assert unknown["status"] == ST_UNKNOWN

    awarded = build_contract_record(
        {
            "canonical_id": "sol:ctr1",
            "award_number": "SPE7M1-26-C-0001",
            "award_date": "2026-09-01",
            "agency": "DLA",
            "award_amount": 15000,
        }
    )
    assert awarded["status"] == CTR_AWARDED
    assert awarded["contract_number"]["value"] == "SPE7M1-26-C-0001"
    assert awarded["note"].lower().find("execution success") >= 0


def test_lifecycle_stages_and_transitions():
    life = build_contract_lifecycle(
        {
            "canonical_id": "sol:life1",
            "award_number": "C-1",
            "award_date": "2026-09-01",
            "lifecycle": "EXECUTION",
        }
    )
    assert len(life["stages"]) == len(LIFECYCLE_STAGES)
    opp = next(s for s in life["stages"] if s["stage"] == "OPPORTUNITY")
    award = next(s for s in life["stages"] if s["stage"] == "AWARD")
    assert opp["status"] == STAGE_COMPLETE
    assert award["status"] == STAGE_COMPLETE
    assert all("owner" in s and "open_actions" in s and "unknowns" in s for s in life["stages"])
    assert life["fabricated"] is False


def test_modification_tracking():
    e = record_contract_modification(
        {
            "contract_id": "C-1",
            "opportunity_id": "sol:mod1",
            "modification_number": "P00001",
            "change_type": MOD_QTY,
            "affected_quantities": "10 → 12",
            "evidence": "mod pdf",
        },
        persist=False,
    )
    assert e["change_type"] == MOD_QTY
    assert e["evidence"] == "mod pdf"

    mods = build_contract_modifications(
        {
            "canonical_id": "sol:mod1",
            "award_number": "C-1",
            "documents": [
                {
                    "document_type": "AMENDMENT",
                    "amendment_number": "0001",
                    "extracted_text": "Quantity change from 10 to 12 EA.",
                }
            ],
        }
    )
    assert mods["modifications"]
    assert any(m.get("change_type") == MOD_QTY for m in mods["modifications"])


def test_delivery_evidence_no_assumption():
    d = record_delivery(
        {
            "contract": "C-1",
            "product": "NSN 1",
            "supplier": "Acme",
            "quantity_expected": 10,
            "shipping_evidence": "tracking 1Z",
        },
        persist=False,
    )
    assert d["assumes_complete"] is False
    assert d["quantity_delivered"] == "UNKNOWN"
    assert d["delivery_date_actual"] == "UNKNOWN"

    bag = build_delivery_records(
        {
            "canonical_id": "sol:del1",
            "award_number": "C-1",
            "payment_readiness": {"shipment_evidence": "BOL-9"},
        }
    )
    assert bag["records"]
    assert all(r.get("assumes_complete") is False for r in bag["records"] if "assumes_complete" in r)


def test_acceptance_evidence_pending_until_documented():
    a = record_acceptance(
        {
            "contract": "C-1",
            "status": ACC_ACCEPTED,
            "accepted_by": "KO",
            "acceptance_date": "2026-09-10",
            "evidence_submitted": "DD250",
        },
        persist=False,
    )
    assert a["status"] == ACC_ACCEPTED

    bag = build_acceptance_records(
        {
            "canonical_id": "sol:acc1",
            "payment_readiness": {"acceptance_evidence": "email acceptance"},
        }
    )
    assert bag["records"]
    assert bag["records"][0]["status"] == ACC_PENDING


def test_payment_evidence_no_inferred_timing():
    p = record_payment(
        {
            "contract": "C-1",
            "invoice": "INV-1",
            "amount": 1000,
            "payment_status": PAY_SUBMITTED,
            "evidence": "wawf screenshot",
        },
        persist=False,
    )
    assert p["timing_inferred"] is False
    assert p["payment_date"] == "UNKNOWN"
    assert p["payment_status"] == PAY_SUBMITTED

    paid = record_payment(
        {"contract": "C-1", "invoice": "INV-1", "payment_status": PAY_PAID, "payment_date": "2026-09-15", "evidence": "remittance"},
        persist=False,
    )
    assert paid["payment_status"] == PAY_PAID
    bag = build_payment_records({"canonical_id": "sol:pay1", "award_number": "C-1"})
    assert "infer" in bag["note"].lower()


def test_performance_no_scores_append_only():
    e = record_contract_performance(
        {
            "contract": "C-1",
            "products": ["NSN 1"],
            "suppliers": ["Acme"],
            "issues": ["packaging"],
            "resolutions": ["repack"],
            "lessons": ["confirm packaging early"],
            "evidence": "closeout notes",
        },
        persist=False,
    )
    assert e["score"] is None
    assert e["ranking"] is None
    bag = build_contract_performance({"canonical_id": "sol:perf1", "award_number": "C-1", "contract_performance": {"entries": [e]}})
    assert bag["scores_forbidden"] is True
    assert bag["rankings_forbidden"] is True


def test_closeout_and_future_demand_no_predictions():
    c = record_closeout(
        {
            "contract": "C-1",
            "final_status": "CLOSED",
            "final_lessons": ["docs first"],
            "evidence": "closeout package",
        },
        persist=False,
    )
    assert c["final_status"] == "CLOSED"

    f = record_future_demand(
        {
            "previous_contract": "C-1",
            "agency": "DLA",
            "renewal_indicators": ["historical_repeat_buy"],
            "evidence": "two prior awards same NSN",
        },
        persist=False,
    )
    assert f["predicts_future_awards"] is False

    bag = build_future_demand(
        {
            "canonical_id": "sol:fut1",
            "award_number": "C-1",
            "award_history": [{"award_id": "A1"}, {"award_id": "A2"}],
        }
    )
    assert bag["predicts_future_awards"] is False
    assert bag["records"]
    assert all(r.get("predicts_future_awards") is False for r in bag["records"] if "predicts_future_awards" in r)


def test_knowledge_graph_and_timeline_require_evidence():
    row = {
        "canonical_id": "sol:kg1",
        "award_number": "C-9",
        "award_date": "2026-09-01",
        "agency": "DLA",
        "dla_product_structure": {"fields": {"nsn": {"value": "4320-01-243-1951"}}},
        "supplier_product_graph": {
            "edges": [{"supplier_name": "Acme", "relationship_type": "DISTRIBUTOR", "source": "graph"}]
        },
    }
    g = build_contract_knowledge_graph(row)
    assert g["nodes"]
    assert g["relationships"]
    assert all(e.get("evidence") for e in g["relationships"])

    tl = build_contract_timeline(row)
    assert tl["events"]
    assert all(e.get("source") and e.get("evidence") is not None for e in tl["events"])


def test_decision_trace_integration():
    trace = build_contract_decision_trace(
        question="What evidence proves execution status?",
        row={"canonical_id": "sol:tr1", "award_number": "C-1", "award_date": "2026-09-01"},
        persist=False,
    )
    assert trace["kind"] == "M3ContractDecisionTrace"
    assert trace["numeric_score"] is None
    assert trace["automatic_conclusion"] is False
    assert trace["action_taken"]
    assert "evidence_reviewed" in trace


def test_command_center_contract_enrichment():
    rows = [
        {
            "canonical_id": "sol:cc1",
            "title": "Pump",
            "award_number": "C-1",
            "award_date": "2026-09-01",
            "lifecycle": "EXECUTION",
            "expiration_date": "2027-01-01",
        }
    ]
    morning = enrich_command_center_contract({}, rows, period="morning")
    assert "contract_actions_due" in morning or "missing_execution_evidence" in morning
    assert "payment_unknowns" in morning
    evening = enrich_command_center_contract({}, rows, period="evening")
    assert "completed_milestones" in evening or "next_actions" in evening


def test_full_profile_and_deal_attach():
    profile = build_contract_lifecycle_profile(
        {"canonical_id": "sol:full", "award_number": "C-1", "award_date": "2026-09-01"}
    )
    assert profile["no_invented_performance"] is True
    assert profile["no_invented_payment_status"] is True
    assert profile["no_automatic_future_predictions"] is True
    assert profile["scoring_unchanged"] is True
    deal = attach_contract_lifecycle_to_deal_room(
        {"canonical_id": "sol:full"},
        row={"canonical_id": "sol:full", "award_number": "C-1"},
    )
    assert deal["contract_lifecycle"]["kind"] == "M3ContractLifecycleProfile"


def test_regression_prior_layers():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_economic_learning_read import BUILD_TAG as ECON
    from m3_execution_os_read import BUILD_TAG as EOS
    from m3_supplier_capital_ops_read import BUILD_TAG as SCO

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert ECON.startswith("20260919-m3-economic-learning")
    assert EOS.startswith("20260919-m3-execution-os")
    assert SCO.startswith("20260919-m3-supplier-capital-ops")

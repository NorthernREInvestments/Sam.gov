"""BUILD 16 — Supplier + Capital + Operations Intelligence tests."""

from __future__ import annotations

from m3_supplier_capital_ops_read import (
    ACQ_DLA,
    BUILD_TAG,
    GATE_ACCESS_BLOCKED,
    GATE_EXEC_UNKNOWN,
    GATE_PURSUE,
    GATE_RESEARCH,
    REL_MANUFACTURER,
    REL_UNKNOWN,
    ST_DETECTED,
    ST_EXPIRED,
    ST_NOT_APPLICABLE,
    ST_RESEARCH,
    ST_UNKNOWN,
    ST_VERIFIED,
    attach_supplier_capital_ops_to_deal_room,
    build_acquisition_path_intelligence,
    build_cyber_applicability,
    build_execution_readiness_gate,
    build_financing_path_intelligence,
    build_insurance_bonding_applicability,
    build_supplier_capital_ops_profile,
    build_supplier_commercial_terms,
    build_supplier_engagement_profile,
    build_supplier_relationship_memory,
    build_supply_chain_path_graph,
    enrich_command_center_sections,
    fact,
    record_supplier_commercial_terms_change,
    record_supplier_relationship_event,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-supplier-capital-ops-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_fact_unknown_preserved():
    f = fact()
    assert f["value"] == "UNKNOWN"
    assert f["status"] == ST_UNKNOWN
    assert f["evidence"] == "UNKNOWN"


def test_supplier_engagement_not_capable_without_evidence():
    row = {
        "canonical_id": "sol:sco1",
        "dla_product_structure": {
            "fields": {
                "nsn": {"value": "4320-01-243-1951", "evidence_source": "solicitation"},
                "part_number": {"value": "ABC-100", "evidence_source": "solicitation"},
            }
        },
        "demand_signal": {"agencies": ["DLA"], "awards": [{"agency": "DLA", "award_id": "A1", "quantity": 10}]},
    }
    eng = build_supplier_engagement_profile(row)
    assert eng["kind"] == "M3SupplierEngagementProfile"
    assert eng["product"]["nsn"]["value"] == "4320-01-243-1951"
    assert eng["supplier_capable"] is False
    assert len(eng["supplier_questions"]) >= 9
    assert all(q["status"] == ST_UNKNOWN for q in eng["supplier_questions"])
    assert eng["outreach_automated"] is False


def test_commercial_terms_quote_expiration_and_history():
    row = {
        "canonical_id": "sol:sco2",
        "supplier_commercial_terms": {
            "by_supplier": {
                "Acme": {
                    "quote": {
                        "price": 100,
                        "date": "2026-01-01",
                        "expiration": "2020-01-01",
                        "quantity": 5,
                        "uom": "EA",
                    },
                    "terms": {"net_terms": "Net 30"},
                    "lead_time": {"stated": "14 days", "evidence_source": "email"},
                }
            }
        },
    }
    terms = build_supplier_commercial_terms(row)
    acme = terms["suppliers"][0]
    assert acme["quote"]["status"] == ST_EXPIRED
    assert acme["quote"]["price"]["value"] == 100
    assert terms["overwrite_forbidden"] is True
    assert terms["invents_terms"] is False

    e1 = record_supplier_commercial_terms_change(
        opportunity_id="sol:sco2",
        supplier_name="Acme",
        field="price",
        previous_value=100,
        new_value=110,
        evidence="email",
        persist=False,
    )
    e2 = record_supplier_commercial_terms_change(
        opportunity_id="sol:sco2",
        supplier_name="Acme",
        field="price",
        previous_value=110,
        new_value=105,
        evidence="email2",
        persist=False,
    )
    assert e1["previous_value"] == 100
    assert e2["previous_value"] == 110


def test_supplier_relationship_memory_append_only_no_scores():
    e = record_supplier_relationship_event(
        {
            "supplier_name": "Acme",
            "opportunity_id": "sol:sco3",
            "stage": "QUOTE_RECEIVED",
            "outcome": "quoted",
            "evidence": "email.pdf",
            "notes": "Net 30 offered",
        },
        persist=False,
    )
    assert e["fabricated_score"] is False
    assert e["stage"] == "QUOTE_RECEIVED"
    mem = build_supplier_relationship_memory(
        {"canonical_id": "sol:sco3", "supplier_relationship_memory": {"entries": [e]}}
    )
    assert mem["append_only"] is True
    assert mem["fake_scores_forbidden"] is True
    assert len(mem["entries"]) >= 1


def test_financing_never_claims_financeable():
    fin = build_financing_path_intelligence(
        {
            "canonical_id": "sol:sco4",
            "deal_economics": {"Contract_Value": 25000, "Required_Acquisition_Cost": 18000},
        }
    )
    assert fin["is_financeable_claim"] is False
    assert len(fin["paths"]) >= 5
    assert all("unknowns" in p and "next_action" in p for p in fin["paths"])
    blob = str(fin).lower()
    assert "financeable." not in blob or "never" in blob
    assert fin["paths"][0]["status"] == ST_UNKNOWN


def test_acquisition_path_dla_and_no_vehicle_assumption():
    acq = build_acquisition_path_intelligence(
        {
            "canonical_id": "sol:sco5",
            "source": "DIBBS",
            "description": "DLA DIBBS solicitation for NSN pump",
        }
    )
    assert acq["path"]["value"] == ACQ_DLA
    assert acq["vehicle_access_equals_opportunity_access"] is False
    assert acq["company_eligibility"]["value"] == "UNKNOWN"


def test_cyber_detection_and_not_applicable():
    empty = build_cyber_applicability({"canonical_id": "sol:cyber0", "description": "Buy pumps"})
    assert empty["status"] == ST_NOT_APPLICABLE
    assert empty["full_compliance_engine"] is False

    hit = build_cyber_applicability(
        {
            "canonical_id": "sol:cyber1",
            "description": "CMMC Level 2 and DFARS 252.204-7012 apply. CUI handling required.",
        }
    )
    assert hit["status"] in {ST_DETECTED, ST_RESEARCH}
    assert hit["findings"]
    assert any(f["requirement"] == "CMMC" for f in hit["findings"])
    assert all(f.get("evidence_text") for f in hit["findings"])


def test_insurance_bonding_detection_no_auto_reject():
    empty = build_insurance_bonding_applicability({"canonical_id": "sol:ins0", "title": "Widgets"})
    assert empty["status"] == ST_UNKNOWN
    assert empty["auto_reject"] is False

    hit = build_insurance_bonding_applicability(
        {
            "canonical_id": "sol:ins1",
            "description": "Performance bond required. Cargo insurance must cover shipment.",
        }
    )
    assert hit["findings"]
    assert hit["auto_reject"] is False
    assert hit["status"] in {ST_DETECTED, ST_RESEARCH}
    assert any(f["detected_requirement"] == "BOND" for f in hit["findings"])


def test_supply_chain_unknown_not_promoted():
    graph = build_supply_chain_path_graph(
        {
            "canonical_id": "sol:chain1",
            "supplier_product_graph": {
                "edges": [
                    {
                        "supplier_name": "Mystery Co",
                        "relationship_type": "UNKNOWN",
                        "confidence": "UNKNOWN",
                    },
                    {
                        "supplier_name": "OEM Inc",
                        "relationship_type": "MANUFACTURER",
                        "confidence": "HIGH",
                        "source": "cage_lookup",
                    },
                ]
            },
            "dla_product_structure": {"fields": {"nsn": {"value": "4320-01-243-1951"}}},
        }
    )
    assert graph["kind"] == "M3SupplyChainPathGraph"
    unknown_edges = [r for r in graph["relationships"] if r["type"] == REL_UNKNOWN]
    assert unknown_edges
    assert all(r.get("promoted_from_unknown") is False for r in graph["relationships"])
    mfr = [r for r in graph["relationships"] if r["type"] == REL_MANUFACTURER]
    assert mfr
    assert "PRODUCT" in [p["step"] for p in graph["path"]]


def test_execution_readiness_gate_no_numeric_score():
    sparse = build_execution_readiness_gate({"canonical_id": "sol:gate0"})
    assert sparse["numeric_score"] is None
    assert sparse["decision"] in {GATE_RESEARCH, GATE_EXEC_UNKNOWN, GATE_ACCESS_BLOCKED, GATE_PURSUE}
    assert len(sparse["dimensions"]) == 7

    blocked = build_execution_readiness_gate(
        {
            "canonical_id": "sol:gate1",
            "acquisition_path": {"path": "GSA Schedule", "company_eligibility": "INELIGIBLE"},
            "product_identity": {"nsn": "1234"},
        }
    )
    assert blocked["decision"] == GATE_ACCESS_BLOCKED


def test_command_center_enrichment_morning_evening():
    rows = [
        {
            "canonical_id": "sol:cc1",
            "title": "Pump",
            "description": "CMMC applies. Performance bond required.",
            "deadline": "2026-10-01",
            "supplier_commercial_terms": {
                "by_supplier": {
                    "Acme": {
                        "quote": {"price": 10, "expiration": "2026-09-25", "status": "DETECTED"},
                        "terms": {"po_acceptance": "UNKNOWN"},
                    }
                }
            },
        }
    ]
    morning = enrich_command_center_sections({}, rows, period="morning")
    assert "financing_actions" in morning
    assert "compliance_changes" in morning
    assert "quote_expiration" in morning

    evening = enrich_command_center_sections({"deadlines": [{"deadline": "2026-10-01"}]}, rows, period="evening")
    assert "supplier_followups" in evening
    assert "next_day_priorities" in evening or "upcoming_deadlines" in evening


def test_full_profile_and_deal_attach():
    profile = build_supplier_capital_ops_profile({"canonical_id": "sol:full", "lifecycle": "ACTIVE"})
    assert profile["facts_only"] is True
    assert profile["no_fake_supplier_scores"] is True
    assert profile["no_assumed_financing"] is True
    assert profile["scoring_unchanged"] is True
    deal = attach_supplier_capital_ops_to_deal_room(
        {"canonical_id": "sol:full", "execution_os": {"kind": "M3ExecutionOSProfile"}},
        row={"canonical_id": "sol:full"},
    )
    assert deal["supplier_capital_ops"]["kind"] == "M3SupplierCapitalOpsProfile"
    assert "execution_readiness_gate" in deal["execution_os"]


def test_regression_execution_os_and_cost_governor():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_execution_os_read import build_execution_os_profile, BUILD_TAG as EOS_TAG

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert EOS_TAG.startswith("20260919-m3-execution-os")
    eos = build_execution_os_profile({"canonical_id": "sol:reg"})
    assert eos["execution_readiness_gate"]["numeric_score"] is None
    assert eos["engines_unchanged"] is True

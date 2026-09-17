"""REAL TINY end-to-end pipeline integration tests (no live HTTP)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from discovery.deadline_viability import VIABILITY_TOO_LATE, VIABILITY_UNKNOWN
from discovery.opportunity_gate import is_structurally_valid_opportunity
from discovery.tiny_end_to_end import (
    ACTION_GET_SUPPLIER_QUOTE,
    ACTION_READY_DEEP,
    ACTION_REJECT,
    ACTION_REVIEW_SOLICITATION,
    ACTION_VERIFY_DEADLINE,
    ACTION_VERIFY_SUPPLIER_TERMS,
    ECON_PROMISING,
    ECON_UNKNOWN,
    EXEC_HARD_FAIL,
    EXEC_PASS,
    PROFIT_BELOW,
    PROFIT_ESTIMATED_ABOVE,
    PROFIT_POTENTIALLY_ABOVE,
    PROFIT_UNKNOWN,
    build_operator_summary_card,
    dedupe_opportunities,
    determine_primary_next_action,
    evaluate_executability,
    evaluate_preliminary_economics,
    evaluate_profit_status,
    process_opportunity_pipeline,
    rank_funding_matches,
    run_tiny_end_to_end,
)
from funding_path_constants import MATCH_REJECT

FIXED_NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def _product_opp(**kw) -> dict:
    due = FIXED_NOW + timedelta(days=10)
    base = {
        "title": "Procurement of network switches and server hardware",
        "description": "Purchase of IT hardware equipment for agency data center",
        "solicitation_number": "RFQ-2026-1001",
        "external_id": "sciquest:1001",
        "agency": "State IT Department",
        "source_id": "state_ia",
        "detail_url": "https://example.gov/bid/1001",
        "deadline_raw": due.strftime("%m/%d/%Y"),
        "response_deadline": due.isoformat(),
        "status": "OPEN",
        "product_classification": "CORE_PRODUCT",
        "estimated_value": "$185,000",
        "estimated_value_status": "LISTED",
        "document_link_discovered": True,
        "buyer_type": "STATE",
        "jurisdiction": "STATE",
        "state_code": "IA",
    }
    base.update(kw)
    return base


def test_structural_gate_flows_through_pipeline():
    row = process_opportunity_pipeline(_product_opp(), now=FIXED_NOW)
    assert row["structural_gate"]["valid"] is True
    assert row.get("pipeline_rejected") is False
    assert row.get("deadline_viability") in {"GOOD", "PLENTY_OF_TIME", "RUSH"}


def test_raw_deadline_reaches_deadline_engine():
    row = process_opportunity_pipeline(_product_opp(deadline_raw="09/25/2026"), now=FIXED_NOW)
    assert row.get("deadline_runway_days") is not None
    assert row.get("deadline_badge") is not None


def test_product_classification_in_queue_card():
    row = process_opportunity_pipeline(_product_opp(), now=FIXED_NOW)
    card = row["operator_summary_card"]
    assert card["product_classification"] == "CORE_PRODUCT"
    assert card["primary_next_action"]


def test_too_late_excluded_from_normal_pursuit_action():
    row = process_opportunity_pipeline(
        _product_opp(deadline_raw=(FIXED_NOW + timedelta(days=1)).strftime("%m/%d/%Y")),
        now=FIXED_NOW,
    )
    assert row.get("deadline_viability") == VIABILITY_TOO_LATE
    action = determine_primary_next_action(row)
    assert action["primary_next_action"] == ACTION_REJECT


def test_unknown_deadline_requires_review():
    row = process_opportunity_pipeline(_product_opp(deadline_raw="select region Alabama"), now=FIXED_NOW)
    assert row.get("deadline_viability") == VIABILITY_UNKNOWN
    action = determine_primary_next_action(row)
    assert action["primary_next_action"] == ACTION_VERIFY_DEADLINE


def test_service_heavy_does_not_outrank_product_on_priority():
    product = process_opportunity_pipeline(
        _product_opp(estimated_value="$60,000", estimated_value_status="LISTED"),
        now=FIXED_NOW,
    )
    service = process_opportunity_pipeline(
        _product_opp(
            title="Professional consulting services for IT modernization",
            description="Staffing and consulting services only",
            product_classification="UNKNOWN",
            estimated_value="$500,000",
            estimated_value_status="LISTED",
        ),
        now=FIXED_NOW,
    )
    assert product["operator_priority_score"] > service["operator_priority_score"]


def test_unknown_supplier_cost_not_zero_in_funding_requirement():
    row = process_opportunity_pipeline(_product_opp(), now=FIXED_NOW)
    req = row["funding_requirement"]
    assert req.get("estimated_supplier_cost") is None
    assert req.get("estimated_total_cost_status") == "UNKNOWN"


def test_unknown_profit_not_automatic_failure():
    econ = evaluate_preliminary_economics(_product_opp())
    profit = evaluate_profit_status(_product_opp(), econ)
    assert profit["profit_status"] in {PROFIT_UNKNOWN, PROFIT_POTENTIALLY_ABOVE, PROFIT_ESTIMATED_ABOVE}
    assert profit["profit_status"] != PROFIT_BELOW


def test_preliminary_economics_can_trigger_deep_research():
    row = process_opportunity_pipeline(_product_opp(), now=FIXED_NOW)
    assert row.get("economics_status") in {ECON_PROMISING, ECON_UNKNOWN}
    assert row.get("primary_next_action") in {
        ACTION_GET_SUPPLIER_QUOTE,
        ACTION_READY_DEEP,
        ACTION_REVIEW_SOLICITATION,
    }


def test_funding_requirement_receives_opportunity_facts():
    row = process_opportunity_pipeline(_product_opp(), now=FIXED_NOW)
    req = row["funding_requirement"]
    assert req["solicitation_number"] == "RFQ-2026-1001"
    assert req["agency"] == "State IT Department"
    assert req.get("deadline_viability")


def test_lender_pg_required_is_not_hard_reject():
    from funding_path_constants import MATCH_REJECT
    from funding_source_kb import fixture_pg_required_lender

    m = rank_funding_matches(_product_opp(), {"estimated_contract_value": 100000})
    pg_match = next(
        (x for x in m["funding_matches"] if x.get("source_name") == fixture_pg_required_lender()["source_name"]),
        None,
    )
    if pg_match:
        assert pg_match["match_status"] != MATCH_REJECT


def test_funding_ranking_never_secured():
    row = process_opportunity_pipeline(_product_opp(), now=FIXED_NOW)
    assert row.get("funding_secured") is False
    for sheet in row.get("top_funding_call_sheets") or []:
        assert "funding_secured" not in sheet or sheet.get("funding_secured") is not True


def test_top_funding_matches_serialize_in_card():
    row = process_opportunity_pipeline(_product_opp(), now=FIXED_NOW)
    card = build_operator_summary_card(row)
    assert "top_apparent_funding_matches" in card
    if card["top_apparent_funding_matches"]:
        match = card["top_apparent_funding_matches"][0]
        assert "company" in match
        assert match.get("funding_secured") is False


def test_supplier_funding_action_when_product_unknown_cost():
    row = process_opportunity_pipeline(_product_opp(), now=FIXED_NOW)
    assert row.get("supplier_funding_action") == ACTION_VERIFY_SUPPLIER_TERMS


def test_government_financing_absent_is_needs_verification():
    row = process_opportunity_pipeline(_product_opp(), now=FIXED_NOW)
    assert row["government_financing"]["availability"] == "NEEDS_VERIFICATION"


def test_primary_next_action_exists_for_survivor():
    row = process_opportunity_pipeline(_product_opp(), now=FIXED_NOW)
    assert row.get("primary_next_action")


def test_provenance_retained_in_card():
    row = process_opportunity_pipeline(_product_opp(), now=FIXED_NOW)
    prov = row["operator_summary_card"]["provenance"]
    assert prov["source_system"] == "state_ia"
    assert prov["source_url"] == "https://example.gov/bid/1001"
    assert prov["raw_source_identifier"] == "sciquest:1001"


def test_dedupe_removes_duplicate_strong_keys():
    a = _product_opp(source_id="state_ia", external_id="sciquest:1001")
    b = _product_opp(source_id="state_mt", external_id="sciquest:1001", agency="State IT Department")
    deduped, removed = dedupe_opportunities([a, b])
    assert removed == 1
    assert len(deduped) == 1


def test_executability_hard_fail_on_clearance():
    ex = evaluate_executability(
        _product_opp(description="Requires TOP SECRET security clearance for all personnel")
    )
    assert ex["executability_status"] == EXEC_HARD_FAIL


def test_pa_form_labels_fail_structural_gate():
    gate = is_structurally_valid_opportunity(
        {
            "title": "Solicitation Search",
            "solicitation_number": None,
            "external_id": "x",
            "deadline_raw": None,
            "detail_url": "https://www.emarketplace.state.pa.us/Search.aspx",
            "agency": "PA",
            "status": "OPEN",
        }
    )
    assert gate["valid"] is False


def test_run_tiny_end_to_end_offline_with_transport():
    """Full integration path without live HTTP — uses injected transport if available."""

    class _FakeResponse:
        status_code = 200

        def __init__(self, text: str):
            self.text = text

    # Minimal: run with authorize_live=False returns zero HTTP from discovery
    result = run_tiny_end_to_end(authorize_live=False)
    assert result["LIVE_API_REQUESTS"] == 0
    assert result["SAM"] == 0
    assert result["OpenAI"] == 0
    assert result["lender_outreach_performed"] is False

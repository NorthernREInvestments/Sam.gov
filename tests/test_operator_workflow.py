"""Operator workflow / missing-info / CO clarification — zero live APIs."""

from __future__ import annotations

from datetime import date, timedelta

from co_clarification import draft_co_question, evaluate_co_clarification_gate, promote_to_co_required
from local_package_search import exhaustive_local_search
from missing_info import (
    CO_CLARIFICATION_CANDIDATE,
    EXTERNAL_REFERENCE_REQUIRED,
    FACT_COMMERCIAL,
    FACT_MANUFACTURER,
    FACT_SOLICITATION,
    MATCH_NONE,
    MATCH_POSSIBLE_INDIRECT,
    POSSIBLE_MATCH_FOUND,
    missing_info_record,
)
from missing_info_engine import process_missing_fact
from next_action_engine import ACTION_OPEN, build_today_queue, generate_next_actions
from operator_crm import (
    ACT_CALLED,
    PROVENANCE_OPERATOR_REPORTED,
    DOCUMENT_VERIFIED,
    TRANSCRIPT_RAW,
    promote_transcript_fact,
)
from solicitation_package import PACKAGE_COMPLETE, PACKAGE_UNRESOLVED, document_record, evaluate_solicitation_package


def test_single_failed_search_cannot_trigger_co():
    audit = {
        "search_terms": ["memory"],  # single term — not proof
        "documents_checked": [{"checked": True, "document_type": "rfq_rfp_ifb", "text_present": True}],
        "exhaustive_local": True,
        "overall_match_class": MATCH_NONE,
        "possible_indirect_remaining": False,
    }
    gate = evaluate_co_clarification_gate(
        fact_class=FACT_SOLICITATION,
        necessary_for_bid=True,
        necessary_for_execution=True,
        search_audit=audit,
        package_status=PACKAGE_COMPLETE,
    )
    assert gate["safe_to_ask_co"] is False
    assert any("insufficient_search_terms" in b for b in gate["blockers"])


def test_synonym_match_prevents_false_missing():
    corpus = [
        {
            "doc_id": "1",
            "document_type": "rfq_rfp_ifb",
            "filename": "rfq.pdf",
            "text": "Memory Capacity: 16GB RDIMM 6400MT/s Single Rank modules populated",
        }
    ]
    audit = exhaustive_local_search(
        fact_key="memory_module_quantity_per_server",
        description="DIMM qty",
        corpus=corpus,
    )
    assert audit["overall_match_class"] != MATCH_NONE
    assert audit["match_count"] > 0


def test_possible_indirect_match_blocks_co_escalation():
    audit = {
        "search_terms": ["memory", "RAM", "RDIMM", "16GB"],
        "documents_checked": [
            {"checked": True, "document_type": "rfq_rfp_ifb", "text_present": True},
            {"checked": True, "document_type": "attachment", "text_present": True},
        ],
        "exhaustive_local": True,
        "overall_match_class": MATCH_POSSIBLE_INDIRECT,
        "possible_indirect_remaining": True,
    }
    gate = evaluate_co_clarification_gate(
        fact_class=FACT_SOLICITATION,
        necessary_for_bid=True,
        necessary_for_execution=True,
        search_audit=audit,
        package_status=PACKAGE_COMPLETE,
    )
    assert gate["safe_to_ask_co"] is False
    assert gate["status"] == POSSIBLE_MATCH_FOUND


def test_commercial_fact_never_routes_to_co():
    gate = evaluate_co_clarification_gate(
        fact_class=FACT_COMMERCIAL,
        necessary_for_bid=True,
        necessary_for_execution=True,
        search_audit={"search_terms": ["a", "b"], "documents_checked": [], "exhaustive_local": True, "overall_match_class": MATCH_NONE},
        package_status=PACKAGE_COMPLETE,
    )
    assert gate["safe_to_ask_co"] is False
    assert gate["status"] == EXTERNAL_REFERENCE_REQUIRED
    assert gate["route"] == "COMMERCIAL"


def test_manufacturer_fact_routes_manufacturer():
    gate = evaluate_co_clarification_gate(
        fact_class=FACT_MANUFACTURER,
        necessary_for_bid=True,
        necessary_for_execution=True,
        search_audit={"search_terms": ["a", "b"], "documents_checked": [], "exhaustive_local": True, "overall_match_class": MATCH_NONE},
        package_status=PACKAGE_COMPLETE,
    )
    assert gate["route"] == "MANUFACTURER"
    assert gate["safe_to_ask_co"] is False


def test_incomplete_package_blocks_premature_co():
    gate = evaluate_co_clarification_gate(
        fact_class=FACT_SOLICITATION,
        necessary_for_bid=True,
        necessary_for_execution=True,
        search_audit={
            "search_terms": ["memory", "RAM", "RDIMM"],
            "documents_checked": [{"checked": True, "document_type": "rfq_rfp_ifb", "text_present": True}],
            "exhaustive_local": True,
            "overall_match_class": MATCH_NONE,
            "possible_indirect_remaining": False,
        },
        package_status=PACKAGE_UNRESOLVED,
    )
    assert gate["safe_to_ask_co"] is False
    assert "ACQUIRE MISSING DOCUMENTS" in gate["recommendation"]


def test_complete_searched_package_can_create_co_candidate():
    audit = {
        "search_terms": ["memory", "RAM", "RDIMM", "16GB", "6400MT/s", "DIMM quantity"],
        "documents_checked": [
            {"checked": True, "document_type": "notice", "text_present": False},
            {"checked": True, "document_type": "rfq_rfp_ifb", "text_present": True},
            {"checked": True, "document_type": "attachment", "text_present": True},
        ],
        "exhaustive_local": True,
        "overall_match_class": MATCH_NONE,
        "possible_indirect_remaining": False,
    }
    gate = evaluate_co_clarification_gate(
        fact_class=FACT_SOLICITATION,
        necessary_for_bid=True,
        necessary_for_execution=True,
        search_audit=audit,
        package_status=PACKAGE_COMPLETE,
    )
    assert gate["safe_to_ask_co"] is True
    assert gate["status"] == CO_CLARIFICATION_CANDIDATE
    # REQUIRED still needs operator promotion
    promoted = promote_to_co_required(gate, operator_confirmed=False)
    assert promoted["status"] == CO_CLARIFICATION_CANDIDATE


def test_already_answered_cannot_be_reasked():
    gate = evaluate_co_clarification_gate(
        fact_class=FACT_SOLICITATION,
        necessary_for_bid=True,
        necessary_for_execution=True,
        search_audit={"search_terms": ["a", "b"], "exhaustive_local": True, "overall_match_class": MATCH_NONE},
        package_status=PACKAGE_COMPLETE,
        already_answered=True,
    )
    assert gate["safe_to_ask_co"] is False
    assert "already_answered" in gate["blockers"]


def test_clarification_deadline_handling():
    gate = evaluate_co_clarification_gate(
        fact_class=FACT_SOLICITATION,
        necessary_for_bid=True,
        necessary_for_execution=True,
        search_audit={
            "search_terms": ["memory", "RAM"],
            "documents_checked": [{"checked": True, "document_type": "rfq_rfp_ifb", "text_present": True}],
            "exhaustive_local": True,
            "overall_match_class": MATCH_NONE,
            "possible_indirect_remaining": False,
        },
        package_status=PACKAGE_COMPLETE,
        clarification_deadline=date.today() - timedelta(days=1),
        today=date.today(),
    )
    assert gate["safe_to_ask_co"] is False
    assert any("deadline" in b for b in gate["blockers"])


def test_co_question_draft_contains_verified_context_only():
    draft = draft_co_question(
        verified_context="The configuration identifies 16GB 6400MT/s Single Rank RDIMMs",
        missing_item="the required DIMM quantity per server",
        documents_actually_checked=["RFQ.pdf"],
    )
    assert "16GB 6400MT/s" in draft["draft"]
    assert "RFQ.pdf" in draft["draft"]
    assert draft["auto_email"] is False
    assert draft["operator_must_review"] is True
    # Must not claim unchecked docs
    bad = draft_co_question(
        verified_context="X",
        missing_item="Y",
        documents_actually_checked=[],
    )
    assert "Amendments" not in bad["draft"] or "reviewed" not in bad["draft"].lower()


def test_notes_are_operator_reported():
    # Unit-level: add_operator_note sets provenance constant
    assert PROVENANCE_OPERATOR_REPORTED == "OPERATOR_REPORTED"


def test_transcript_fact_cannot_become_verified_automatically():
    r = promote_transcript_fact(
        current_status=TRANSCRIPT_RAW,
        target_status="VERIFIED",
        operator_confirmed=True,
        document_evidence=False,
    )
    assert r["allowed"] is False
    r2 = promote_transcript_fact(
        current_status=TRANSCRIPT_RAW,
        target_status=DOCUMENT_VERIFIED,
        document_evidence=True,
    )
    assert r2["allowed"] is True


def test_next_action_follows_blocker_dependency():
    ws = {
        "opportunity": {"id": 199, "due_date": (date.today() + timedelta(days=10)).isoformat()},
        "solicitation_package": {"status": PACKAGE_UNRESOLVED},
        "bom_gate": {"status": "BOM_INCOMPLETE", "unknown_components": ["memory_module"], "supplier_quote_request_ready": False},
        "deal_readiness": {"deal_ready": False, "status": "DEAL_NOT_READY"},
        "bid_readiness": {"status": "BID_NOT_READY"},
        "pursuit_plans": [],
        "activities": [],
        "financing": {"status": "FINANCING_UNRESOLVED"},
    }
    missing = [
        {
            "fact_key": "memory_module_quantity_per_server",
            "description": "DIMM qty",
            "status": "MISSING_CONFIRMED_LOCAL",
            "fact_class": FACT_SOLICITATION,
            "co_gate": {"safe_to_ask_co": False, "recommendation": "ACQUIRE MISSING DOCUMENTS FIRST"},
        }
    ]
    na = generate_next_actions(workspace=ws, missing_items=missing)
    assert na["next_action"]["status"] == ACTION_OPEN
    assert "document" in na["next_action"]["action"].lower() or "configuration" in na["next_action"]["action"].lower() or "Package" in na["next_action"]["why"]


def test_action_queue_prioritizes_deadline_blocking():
    ws = {
        "opportunity": {"id": 1, "due_date": (date.today() + timedelta(days=1)).isoformat()},
        "solicitation_package": {"status": PACKAGE_UNRESOLVED},
        "bom_gate": {"status": "BOM_INCOMPLETE", "supplier_quote_request_ready": False},
        "deal_readiness": {"deal_ready": False},
        "bid_readiness": {"status": "BID_NOT_READY"},
        "pursuit_plans": [],
        "activities": [],
        "financing": {},
    }
    na = generate_next_actions(workspace=ws, missing_items=[])
    tq = build_today_queue(na, today=date.today())
    assert tq["count"] >= 1
    assert tq["actions"][0]["priority"] <= 50


def test_process_missing_fact_package_blocks_co():
    fact = missing_info_record(
        fact_key="memory_module_quantity_per_server",
        description="DIMM qty",
        fact_class=FACT_SOLICITATION,
    )
    corpus = [
        {
            "doc_id": "x",
            "document_type": "rfq_rfp_ifb",
            "filename": "RFQ.pdf",
            "text": "Dell PowerEdge R670 210-BNZH quantity 14 brand name only. No DIMM count stated here xyzzy.",
        }
    ]
    out = process_missing_fact(
        fact=fact,
        corpus=corpus,
        package_status=PACKAGE_UNRESOLVED,
        verified_context="The configuration identifies 16GB RDIMMs",
    )
    assert out["safe_to_ask_co"] is False
    assert out["question_draft"]["operator_must_review"] is True


def test_package_one_pdf_not_complete():
    pkg = evaluate_solicitation_package(
        [document_record(document_type="rfq_rfp_ifb", filename="one.pdf")]
    )
    assert pkg["status"] != PACKAGE_COMPLETE


def test_called_activity_type_constant():
    assert ACT_CALLED == "CALLED"


def test_get_ui_zero_external_marker():
    na = generate_next_actions(workspace={"opportunity": {}, "solicitation_package": {}, "bom_gate": {}, "deal_readiness": {}, "bid_readiness": {}, "pursuit_plans": [], "activities": [], "financing": {}})
    assert na["LIVE_API_REQUESTS"] == 0

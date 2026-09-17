"""GovCon OS foundation — persistence, context packet, transcripts, warnings, views."""

from __future__ import annotations

from datetime import date, timedelta

from ai_operating_modes import MODE_LEAN, get_operating_mode, mode_config
from ask_about_deal import build_context_preview, create_ask_request
from award_lifecycle import (
    STATUS_AWARDED,
    STATUS_SUBMITTED,
    get_or_create_award_lifecycle,
    post_award_next_action,
    update_award_milestone,
)
from commercial_execution import seed_commercial_artifacts
from deal_context_packet import (
    FACT_CALCULATED,
    FACT_POLICY,
    FACT_TRANSCRIPT,
    FACT_VERIFIED,
    build_deal_context_packet,
)
from deal_readiness import BID_READY, DEAL_READY, evaluate_bid_readiness, evaluate_deal_readiness
from exception_engine import compute_warnings_from_workspace, persist_warnings
from funding_engine import build_funding_plan_snapshot, evaluate_pre_bid_funding_viability
from funding_persistence import load_persisted_funding, sync_funding_plan
from next_action_engine import generate_next_actions
from operator_crm import ACT_RECEIVED_TERMS, ACT_FOLLOW_UP_NEEDED, MANUAL_ACTIVITY_TYPES, promote_transcript_fact
from transcript_service import paste_transcript, record_transcript_statement, TRANSCRIPT_FINANCIER

SYNTHETIC_BOM = [
    {"component": "base_system", "value": "Cisco Catalyst 9300 Switch", "quantity": 20, "status": "VERIFIED"},
    {"component": "part_number", "value": "C9300-48P", "quantity": 20, "status": "VERIFIED"},
]


def test_ai_operating_mode_defaults_lean():
    assert get_operating_mode() == MODE_LEAN
    cfg = mode_config()
    assert cfg["mode"] == MODE_LEAN
    assert cfg["OpenAI"] == 0


def test_funding_plan_persistence_and_reload():
    from database import SessionLocal
    from models import Contract, DealState, FundingPlan

    session = SessionLocal()
    try:
        contract = session.query(Contract).first()
        if not contract:
            return
        view = build_funding_plan_snapshot(
            contract_id=contract.id,
            pursuits=[{"pg_required": None, "personal_credit_required": None, "borrower_cash_required": None}],
        )
        sync_funding_plan(session, contract_id=contract.id, funding_plan_view=view, pursuits=[])
        session.commit()
        row = session.query(FundingPlan).filter_by(contract_id=contract.id).first()
        assert row is not None
        assert row.pre_bid_status is not None
        reloaded = load_persisted_funding(session, contract.id)
        assert reloaded["id"] == row.id
        assert reloaded["pre_bid_status"] == row.pre_bid_status
    finally:
        session.rollback()
        session.close()


def test_deal_context_packet_classifications():
    from database import SessionLocal

    session = SessionLocal()
    try:
        packet = build_deal_context_packet(session, 999999)
        if packet.get("error"):
            return
        assert packet["policy"]["classification"] == FACT_POLICY
        assert packet["opportunity"]["classification"] in {FACT_VERIFIED, FACT_CALCULATED}
        assert packet["transcripts"]["classification"] == FACT_TRANSCRIPT
        assert packet["OpenAI"] == 0
    finally:
        session.close()


def test_transcript_intake_and_quo_id_nullable():
    from database import SessionLocal
    from models import CallTranscript, Contract

    session = SessionLocal()
    try:
        contract = session.query(Contract).first()
        if not contract:
            return
        out = paste_transcript(
            session,
            contract_id=contract.id,
            raw_transcript="Rep: No personal guarantee required on this program.",
            transcript_type=TRANSCRIPT_FINANCIER,
            organization_name="Test Lender",
            external_call_id=None,
        )
        session.commit()
        row = session.query(CallTranscript).filter_by(id=out["id"]).first()
        assert row.external_call_id is None
        assert row.analysis_status == "NOT_ANALYZED"
    finally:
        session.rollback()
        session.close()


def test_transcript_statement_not_verified():
    rec = record_transcript_statement(
        statement="Lender representative said no personal guarantee is required.",
        transcript_id=1,
        field="pg_required",
        value=False,
    )
    assert rec["extracted"]["verification"] == "UNVERIFIED"
    assert rec["extracted"]["status"] == "TRANSCRIPT_REPORTED"
    assert rec["may_promote_to_verified"] is False


def test_pg_warning_generated():
    ws = {
        "financing": {"pursuits": [{"pg_required": True, "provider_id": 1}]},
        "economics": {},
        "opportunity": {},
    }
    warns = compute_warnings_from_workspace(ws)
    assert any(w["warning_type"] == "FINANCING_PG_REQUIRED" for w in warns)


def test_personal_credit_warning():
    ws = {"financing": {"pursuits": [{"personal_credit_required": True}]}, "economics": {}, "opportunity": {}}
    assert any(w["warning_type"] == "FINANCING_PERSONAL_CREDIT_REQUIRED" for w in compute_warnings_from_workspace(ws))


def test_cash_requirement_warning():
    ws = {"financing": {"pursuits": [{"borrower_cash_required": True}]}, "economics": {}, "opportunity": {}}
    assert any(w["warning_type"] == "FINANCING_OWNER_CASH_REQUIRED" for w in compute_warnings_from_workspace(ws))


def test_profit_warning():
    ws = {"economics": {"actual_profit": 5000}, "financing": {}, "opportunity": {}, "deal_readiness": {}}
    assert any(w["warning_type"] == "COMMERCIAL_PROFIT_BELOW_MINIMUM" for w in compute_warnings_from_workspace(ws))


def test_quote_mismatch_warning():
    ws = {
        "quotes": [{"id": 1, "validation": {"issues": [{"code": "BOM_MISMATCH", "message": "Wrong quantity"}]}}],
        "economics": {},
        "financing": {},
        "opportunity": {},
    }
    assert any(w["warning_type"] == "QUOTE_BOM_MISMATCH" for w in compute_warnings_from_workspace(ws))


def test_oem_channel_warning():
    ws = {
        "quotes": [{"id": 2, "validation": {"issues": [{"code": "OEM_CHANNEL", "message": "Authorization unresolved"}]}}],
        "economics": {},
        "financing": {},
        "opportunity": {},
    }
    assert any(w["warning_type"] == "QUOTE_OEM_CHANNEL_UNRESOLVED" for w in compute_warnings_from_workspace(ws))


def test_deadline_blocker_warning():
    ws = {
        "opportunity": {"due_date": (date.today() + timedelta(days=3)).isoformat()},
        "deal_readiness": {"status": "NOT_READY"},
        "economics": {},
        "financing": {},
    }
    assert any(w["warning_type"] == "DEADLINE_APPROACHING_WITH_BLOCKER" for w in compute_warnings_from_workspace(ws))


def test_duplicate_warning_prevention():
    from database import SessionLocal
    from models import Contract, DealWarning

    session = SessionLocal()
    try:
        contract = session.query(Contract).first()
        if not contract:
            return
        computed = [
            {
                "warning_type": "FINANCING_PG_REQUIRED",
                "active_key": "default",
                "severity": "CRITICAL",
                "message": "PG required",
                "why_it_matters": "rule",
                "recommended_next_action": "find alt",
                "evidence_json": {},
            }
        ]
        persist_warnings(session, contract.id, computed)
        persist_warnings(session, contract.id, computed)
        session.commit()
        rows = (
            session.query(DealWarning)
            .filter_by(contract_id=contract.id, warning_type="FINANCING_PG_REQUIRED", status="ACTIVE")
            .all()
        )
        assert len(rows) == 1
    finally:
        session.rollback()
        session.close()


def test_warning_resolution_on_recompute():
    from database import SessionLocal
    from models import Contract

    session = SessionLocal()
    try:
        contract = session.query(Contract).first()
        if not contract:
            return
        persist_warnings(
            session,
            contract.id,
            [
                {
                    "warning_type": "FINANCING_PG_REQUIRED",
                    "active_key": "default",
                    "severity": "CRITICAL",
                    "message": "PG",
                    "why_it_matters": "x",
                    "recommended_next_action": "y",
                    "evidence_json": {},
                }
            ],
        )
        session.commit()
        persist_warnings(session, contract.id, [])
        session.commit()
        from models import DealWarning

        active = session.query(DealWarning).filter_by(contract_id=contract.id, status="ACTIVE").count()
        resolved = session.query(DealWarning).filter_by(contract_id=contract.id, status="RESOLVED").count()
        assert active == 0
        assert resolved >= 1
    finally:
        session.rollback()
        session.close()


def test_ask_about_deal_context_preview():
    from database import SessionLocal
    from models import Contract

    session = SessionLocal()
    try:
        contract = session.query(Contract).first()
        if not contract:
            return
        preview = build_context_preview(session, contract.id, question="Can I bid this yet?")
        assert preview["ai_status"] == "PREVIEW_ONLY"
        assert preview["OpenAI"] == 0
        assert "packet" in preview
    finally:
        session.close()


def test_ask_request_persisted_preview_only():
    from database import SessionLocal
    from models import AskAboutDealRequest, Contract

    session = SessionLocal()
    try:
        contract = session.query(Contract).first()
        if not contract:
            return
        out = create_ask_request(session, contract_id=contract.id, question="What am I missing?", enqueue_ai=False)
        session.commit()
        row = session.query(AskAboutDealRequest).filter_by(id=out["request_id"]).first()
        assert row.status == "PREVIEW_ONLY"
        assert row.response_json is None
    finally:
        session.rollback()
        session.close()


def test_crm_activity_types_extended():
    assert ACT_RECEIVED_TERMS in MANUAL_ACTIVITY_TYPES
    assert ACT_FOLLOW_UP_NEEDED in MANUAL_ACTIVITY_TYPES


def test_post_award_next_actions():
    assert post_award_next_action({"lifecycle_status": STATUS_SUBMITTED})["action"] == "CHECK AWARD"
    assert post_award_next_action({"lifecycle_status": STATUS_AWARDED})["action"] == "OBTAIN FINAL FUNDING APPROVAL"


def test_award_lifecycle_persistence():
    from database import SessionLocal
    from models import AwardLifecycle, Contract

    session = SessionLocal()
    try:
        contract = session.query(Contract).first()
        if not contract:
            return
        update_award_milestone(session, contract.id, lifecycle_status=STATUS_SUBMITTED, submission_reference="REF-1")
        session.commit()
        row = session.query(AwardLifecycle).filter_by(contract_id=contract.id).first()
        assert row.submission_reference == "REF-1"
    finally:
        session.rollback()
        session.close()


def test_deal_ready_still_blocked_with_pg():
    deal_rd = evaluate_deal_readiness(
        bom=[{"component": "x", "status": "VERIFIED"}],
        bom_gate={"status": "BOM_COMPLETE"},
        quotes=[{"validation_status": "QUOTE_VALID"}],
        required_quantity=1,
        financing={"status": "FINANCING_FAIL", "pursuits": [{"pg_required": True}]},
        economics={"actual_profit": 15000},
        availability_verified=True,
    )
    assert deal_rd["status"] != DEAL_READY


def test_bid_ready_independent_of_deal_ready():
    bid_rd = evaluate_bid_readiness(
        deal_readiness={"status": "NOT_READY", "deal_ready": False},
        solicitation_package={"status": "PACKAGE_COMPLETE", "required_forms_complete": True},
    )
    assert bid_rd["status"] in {BID_READY, "BID_NOT_READY"}


def test_generic_non_199_seed():
    seed = seed_commercial_artifacts(
        bom=SYNTHETIC_BOM,
        suppliers=[{"id": 1, "name": "Reseller"}],
        fob_draft=None,
        fob_safe_to_ask=False,
        due_date=date(2026, 12, 1),
        quantity=20,
        product_summary="Cisco switches",
    )
    pkt = seed["supplier_rfq_packet"]
    assert pkt["product"]["name"] == "Cisco Catalyst 9300 Switch"
    assert "PowerEdge" not in str(pkt)
    assert seed["LIVE_API_REQUESTS"] == 0


def test_global_views_zero_external():
    from database import SessionLocal
    from global_views import build_bids_view, build_contacts_view, build_funding_work_view, build_suppliers_view

    session = SessionLocal()
    try:
        for fn in (build_suppliers_view, build_contacts_view, build_funding_work_view, build_bids_view):
            out = fn(session)
            assert out["LIVE_API_REQUESTS"] == 0
    finally:
        session.close()


def test_no_ai_on_get_os_endpoints():
    from database import SessionLocal
    from os_service import build_global_today_queue, build_os_dashboard
    from global_views import build_funding_work_view
    from ai_operating_modes import mode_config

    session = SessionLocal()
    try:
        dash = build_os_dashboard(session)
        today = build_global_today_queue(session)
        funding = build_funding_work_view(session)
        mode = mode_config()
        assert dash["LIVE_API_REQUESTS"] == 0
        assert today["LIVE_API_REQUESTS"] == 0
        assert funding["LIVE_API_REQUESTS"] == 0
        assert mode["OpenAI"] == 0
    finally:
        session.close()

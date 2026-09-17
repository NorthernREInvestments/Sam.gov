"""Deal Workspace API — read + safe CRM mutations (never paid AI / external contact)."""

from __future__ import annotations
from application_clock import now_utc

from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import SessionLocal
from models import Contract, CrmActivity, SupplierContact

router = APIRouter(prefix="/api/deals", tags=["deal-workspace"])


def _get_contract_by_id(session, deal_id: int) -> Contract:
    row = session.query(Contract).filter_by(id=deal_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Deal/opportunity not found")
    return row


def _workspace(session, contract: Contract) -> dict[str, Any]:
    from deal_workspace import build_workspace_snapshot

    return build_workspace_snapshot(session, contract)


class ActivityBody(BaseModel):
    activity_type: str
    summary: str = ""
    supplier_id: int | None = None
    contact_id: int | None = None
    operator: str | None = None
    facts_learned: list[dict[str, Any]] = Field(default_factory=list)
    next_action: str | None = None
    next_action_date: str | None = None


class ContactBody(BaseModel):
    supplier_id: int
    name: str
    title_role: str | None = None
    phone: str | None = None
    email: str | None = None
    is_federal_team: bool | None = None
    notes: str | None = None


class CapabilityBody(BaseModel):
    capability_key: str
    value: Any = None
    status: str = "UNKNOWN"
    held: bool | None = None
    notes: str | None = None
    source: str | None = "operator"


# Literal paths BEFORE /{deal_id}
@router.get("/company-profile")
def get_company_profile() -> dict[str, Any]:
    from company_profile import ensure_default_company_profile

    session = SessionLocal()
    try:
        profile = ensure_default_company_profile(session)
        session.commit()
        return {"LIVE_API_REQUESTS": 0, "profile": profile}
    finally:
        session.close()


@router.post("/company-profile/capabilities")
def post_capability(body: CapabilityBody) -> dict[str, Any]:
    from company_profile import load_company_profile, upsert_capability

    session = SessionLocal()
    try:
        upsert_capability(
            session,
            body.capability_key,
            value=body.value,
            status=body.status,
            held=body.held,
            notes=body.notes,
            source=body.source or "operator",
        )
        session.commit()
        return {"LIVE_API_REQUESTS": 0, "profile": load_company_profile(session)}
    finally:
        session.close()


@router.get("/suppliers/{supplier_id}/profile")
def get_supplier_profile(supplier_id: int) -> dict[str, Any]:
    from operator_crm import build_supplier_relationship_profile

    session = SessionLocal()
    try:
        return build_supplier_relationship_profile(session, supplier_id)
    finally:
        session.close()


@router.get("/government-contacts/{contact_id}/profile")
def get_gov_profile(contact_id: int) -> dict[str, Any]:
    from operator_crm import build_co_relationship_profile

    session = SessionLocal()
    try:
        return build_co_relationship_profile(session, contact_id)
    finally:
        session.close()


@router.get("/{deal_id}")
def get_deal(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        return _workspace(session, c)
    finally:
        session.close()


@router.get("/{deal_id}/documents")
def get_documents(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {
            "LIVE_API_REQUESTS": 0,
            "documents": ws["documents"],
            "solicitation_package": ws["solicitation_package"],
        }
    finally:
        session.close()


@router.get("/{deal_id}/requirements")
def get_requirements(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {"LIVE_API_REQUESTS": 0, "requirements": ws["requirements"]}
    finally:
        session.close()


@router.get("/{deal_id}/bom")
def get_bom(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {
            "LIVE_API_REQUESTS": 0,
            "bom": ws["bom"],
            "bom_gate": ws["bom_gate"],
            "supplier_quote_request_ready": ws["bom_gate"].get("supplier_quote_request_ready"),
        }
    finally:
        session.close()


@router.get("/{deal_id}/suppliers")
def get_suppliers(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {
            "LIVE_API_REQUESTS": 0,
            "suppliers": ws["suppliers"],
            "pursuit_plans": ws["pursuit_plans"],
            "negotiation_suggestions": ws["negotiation_suggestions"],
        }
    finally:
        session.close()


@router.get("/{deal_id}/activities")
def get_activities(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {"LIVE_API_REQUESTS": 0, "activities": ws["activities"]}
    finally:
        session.close()


@router.get("/{deal_id}/quotes")
def get_quotes(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {"LIVE_API_REQUESTS": 0, "quotes": ws["quotes"]}
    finally:
        session.close()


@router.get("/{deal_id}/financing")
def get_financing(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {"LIVE_API_REQUESTS": 0, "financing": ws["financing"]}
    finally:
        session.close()


@router.get("/{deal_id}/readiness")
def get_readiness(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {
            "LIVE_API_REQUESTS": 0,
            "deal_readiness": ws["deal_readiness"],
            "bid_readiness": ws["bid_readiness"],
            "solicitation_package": ws["solicitation_package"],
            "bom_gate": ws["bom_gate"],
        }
    finally:
        session.close()


@router.get("/{deal_id}/bid-package")
def get_bid_package(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {
            "LIVE_API_REQUESTS": 0,
            "bid_package": ws["bid_package"],
            "bid_readiness": ws["bid_readiness"],
        }
    finally:
        session.close()


@router.post("/{deal_id}/activities")
def post_activity(deal_id: int, body: ActivityBody) -> dict[str, Any]:
    """Operator CRM note — never triggers paid AI or external contact."""
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        facts = []
        for f in body.facts_learned or []:
            if isinstance(f, dict):
                f = dict(f)
                f.setdefault("verification_status", "OPERATOR_REPORTED")
                facts.append(f)
        nad = None
        if body.next_action_date:
            try:
                nad = date.fromisoformat(body.next_action_date[:10])
            except ValueError:
                nad = None
        row = CrmActivity(
            contract_id=c.id,
            supplier_id=body.supplier_id,
            contact_id=body.contact_id,
            activity_type=body.activity_type,
            summary=body.summary,
            facts_learned_json=facts,
            operator=body.operator,
            next_action=body.next_action,
            next_action_date=nad,
            activity_at=now_utc(),
        )
        session.add(row)
        session.commit()
        return {
            "LIVE_API_REQUESTS": 0,
            "paid_ai": False,
            "external_contact": False,
            "id": row.id,
            "activity_type": row.activity_type,
        }
    finally:
        session.close()


@router.post("/{deal_id}/contacts")
def post_contact(deal_id: int, body: ContactBody) -> dict[str, Any]:
    session = SessionLocal()
    try:
        _get_contract_by_id(session, deal_id)
        row = SupplierContact(
            supplier_id=body.supplier_id,
            name=body.name,
            title_role=body.title_role,
            phone=body.phone,
            email=body.email,
            is_federal_team=body.is_federal_team,
            notes=body.notes,
            verification_status="OPERATOR_REPORTED",
        )
        session.add(row)
        session.commit()
        return {"LIVE_API_REQUESTS": 0, "id": row.id, "name": row.name}
    finally:
        session.close()


@router.post("/{deal_id}/reconcile-local")
def reconcile_local(deal_id: int) -> dict[str, Any]:
    """Local-only workspace seed (Opp 199). Zero external calls."""
    from deal_workspace import OPP199_ID, reconcile_opportunity_199_workspace

    if deal_id != OPP199_ID:
        raise HTTPException(
            status_code=400, detail="reconcile-local currently supports opportunity 199 only"
        )
    session = SessionLocal()
    try:
        result = reconcile_opportunity_199_workspace(session)
        session.commit()
        return result
    finally:
        session.close()


class QuickLogBody(BaseModel):
    activity_type: str = "CALLED"
    supplier_id: int | None = None
    contact_id: int | None = None
    government_contact_id: int | None = None
    organization_name: str | None = None
    operator: str | None = None
    what_happened: str = ""
    outcome: str | None = None
    next_action: str | None = None
    next_action_at: str | None = None


class NoteBody(BaseModel):
    entity_type: str = "opportunity"
    entity_id: int | None = None
    text: str
    author: str | None = None


class GovContactBody(BaseModel):
    name: str
    agency: str | None = None
    office: str | None = None
    title: str | None = None
    phone: str | None = None
    email: str | None = None
    documented_instructions: str | None = None


@router.post("/{deal_id}/quick-log")
def post_quick_log(deal_id: int, body: QuickLogBody) -> dict[str, Any]:
    """One-click Called / Email / Follow-up — auto timestamp, no AI/external."""
    from operator_crm import log_manual_activity

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        result = log_manual_activity(
            session,
            contract_id=c.id,
            activity_type=body.activity_type,
            operator=body.operator,
            supplier_id=body.supplier_id,
            contact_id=body.contact_id,
            government_contact_id=body.government_contact_id,
            what_happened=body.what_happened,
            outcome=body.outcome,
            next_action=body.next_action,
            next_action_at=body.next_action_at,
            organization_name=body.organization_name,
        )
        session.commit()
        return result
    finally:
        session.close()


@router.post("/{deal_id}/notes")
def post_note(deal_id: int, body: NoteBody) -> dict[str, Any]:
    from operator_crm import add_operator_note

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        result = add_operator_note(
            session,
            entity_type=body.entity_type,
            entity_id=body.entity_id if body.entity_id is not None else c.id,
            text=body.text,
            author=body.author,
            contract_id=c.id,
        )
        session.commit()
        return result
    finally:
        session.close()


@router.get("/{deal_id}/missing-info")
def get_missing_info(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {
            "LIVE_API_REQUESTS": 0,
            "missing_info": ws.get("missing_info"),
            "package_status": (ws.get("solicitation_package") or {}).get("status"),
        }
    finally:
        session.close()


@router.post("/{deal_id}/missing-info/run-local")
def run_missing_info_local(deal_id: int) -> dict[str, Any]:
    """Exhaustive local package search only — zero paid/external."""
    from missing_info_engine import run_missing_info_pass

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        result = run_missing_info_pass(session, c.id, persist=True)
        session.commit()
        return result
    finally:
        session.close()


@router.get("/{deal_id}/today")
def get_today_queue(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {
            "LIVE_API_REQUESTS": 0,
            "next_action": ws.get("next_action"),
            "today_queue": ws.get("today_queue"),
            "action_queue": ws.get("action_queue"),
        }
    finally:
        session.close()


@router.post("/{deal_id}/government-contacts")
def post_gov_contact(deal_id: int, body: GovContactBody) -> dict[str, Any]:
    from models import GovernmentContact

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        row = GovernmentContact(
            contract_id=c.id,
            name=body.name,
            agency=body.agency or c.agency,
            office=body.office,
            title=body.title,
            phone=body.phone,
            email=body.email,
            documented_instructions=body.documented_instructions,
            contact_provenance="OPERATOR_REPORTED",
        )
        session.add(row)
        session.commit()
        return {"LIVE_API_REQUESTS": 0, "id": row.id, "name": row.name, "provenance": "OPERATOR_REPORTED"}
    finally:
        session.close()


class QuoteEntryBody(BaseModel):
    supplier_id: int | None = None
    supplier_name: str | None = None
    quote_number: str | None = None
    quote_date: str | None = None
    expires: str | None = None
    quantity: float | str | None = None
    unit_price: float | str | None = None
    extended_product_price: float | str | None = None
    freight: float | str | None = None
    other_required_charges: float | str | None = None
    total: float | str | None = None
    payment_terms: str | None = None
    availability: str | None = None
    lead_time: str | None = None
    delivery_confirmed: bool | None = None
    oem_letter_available: bool | None = None
    federal_channel_confirmed: bool | None = None
    upfront_payment_required: bool | None = None
    document_notes: str | None = None
    bom_match: bool | None = None
    memory_modules_per_server_quoted: int | None = None
    storage_drives_per_server_quoted: int | None = None
    verification_status: str = "OPERATOR_ENTERED"


class QuoteDocumentBody(BaseModel):
    supplier_id: int | None = None
    offer_id: int | None = None
    filename: str
    content_hash: str | None = None
    quote_number: str | None = None


class FinancingEntryBody(BaseModel):
    provider: str | None = None
    provider_id: int | None = None
    contact: str | None = None
    date: str | None = None
    transaction_eligible: bool | str | None = None
    pg_required: bool | str | None = None
    personal_credit_required: bool | str | None = None
    cash_contribution: float | str | None = None
    supplier_paid_direct: bool | str | None = None
    minimum: float | str | None = None
    maximum: float | str | None = None
    fees: str | None = None
    recourse: str | None = None
    funding_time: str | None = None
    status: str | None = None
    notes: str | None = None


class ProposedBidBody(BaseModel):
    amount: float | None = None
    entered_by: str | None = None
    reason: str | None = None


class SupplierQuickBody(BaseModel):
    supplier_id: int
    activity_type: str = "CALLED"
    what_happened: str = ""
    outcome: str | None = None
    next_action: str | None = None
    next_action_at: str | None = None
    operator: str | None = None


@router.post("/{deal_id}/quotes")
def post_quote(deal_id: int, body: QuoteEntryBody) -> dict[str, Any]:
    from commercial_ops import save_operator_quote

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        result = save_operator_quote(session, contract_id=c.id, raw=body.model_dump())
        session.commit()
        return result
    finally:
        session.close()


@router.post("/{deal_id}/quote-documents")
def post_quote_document(deal_id: int, body: QuoteDocumentBody) -> dict[str, Any]:
    from commercial_ops import attach_quote_document

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        result = attach_quote_document(
            session,
            contract_id=c.id,
            supplier_id=body.supplier_id,
            offer_id=body.offer_id,
            filename=body.filename,
            content_hash=body.content_hash,
            quote_number=body.quote_number,
        )
        session.commit()
        return result
    finally:
        session.close()


@router.post("/{deal_id}/financing-entry")
def post_financing_entry(deal_id: int, body: FinancingEntryBody) -> dict[str, Any]:
    from commercial_ops import save_financing_entry

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        result = save_financing_entry(session, contract_id=c.id, raw=body.model_dump())
        session.commit()
        return result
    finally:
        session.close()


@router.post("/{deal_id}/proposed-bid")
def post_proposed_bid(deal_id: int, body: ProposedBidBody) -> dict[str, Any]:
    from commercial_ops import save_proposed_bid

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        result = save_proposed_bid(
            session,
            contract_id=c.id,
            amount=body.amount,
            entered_by=body.entered_by,
            reason=body.reason,
        )
        session.commit()
        return result
    finally:
        session.close()


@router.post("/{deal_id}/supplier-action")
def post_supplier_action(deal_id: int, body: SupplierQuickBody) -> dict[str, Any]:
    """One-click supplier CRM from supplier card."""
    from commercial_ops import mark_quote_requested
    from operator_crm import ACT_QUOTE_RECEIVED, ACT_QUOTE_REQUESTED, log_manual_activity

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        atype = body.activity_type.upper()
        if atype == ACT_QUOTE_REQUESTED:
            result = mark_quote_requested(session, contract_id=c.id, supplier_id=body.supplier_id)
        else:
            result = log_manual_activity(
                session,
                contract_id=c.id,
                activity_type=atype,
                supplier_id=body.supplier_id,
                operator=body.operator,
                what_happened=body.what_happened or atype,
                outcome=body.outcome,
                next_action=body.next_action
                or ("Enter quote" if atype == ACT_QUOTE_RECEIVED else None),
                next_action_at=body.next_action_at,
            )
        session.commit()
        result["offer_quote_entry"] = atype == ACT_QUOTE_RECEIVED
        return result
    finally:
        session.close()


@router.get("/{deal_id}/commercial")
def get_commercial(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {
            "LIVE_API_REQUESTS": 0,
            "next_action": ws.get("next_action"),
            "commercial_status": ws.get("commercial_status"),
            "commercial_plan": ws.get("commercial_plan"),
            "supplier_rfq_packet": ws.get("supplier_rfq_packet"),
            "supplier_call_sheets": ws.get("supplier_call_sheets"),
            "supplier_call_script": ws.get("supplier_call_script"),
            "negotiation_playbook": ws.get("negotiation_playbook"),
            "quote_comparison": ws.get("quote_comparison"),
            "financing_packet": ws.get("financing_packet"),
            "co_clarification_card": ws.get("co_clarification_card"),
            "proposed_bid": ws.get("proposed_bid"),
            "economics": ws.get("economics"),
            "external_calls": ws.get("external_calls"),
        }
    finally:
        session.close()


@router.get("/{deal_id}/warnings")
def get_deal_warnings(deal_id: int) -> dict[str, Any]:
    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        return {"warnings": ws.get("warnings") or [], "LIVE_API_REQUESTS": 0}
    finally:
        session.close()


@router.get("/{deal_id}/transcripts")
def get_deal_transcripts(deal_id: int) -> dict[str, Any]:
    from transcript_service import list_transcripts

    session = SessionLocal()
    try:
        _get_contract_by_id(session, deal_id)
        return {"transcripts": list_transcripts(session, deal_id), "LIVE_API_REQUESTS": 0}
    finally:
        session.close()


class TranscriptPasteBody(BaseModel):
    raw_transcript: str
    transcript_type: str = "SUPPLIER"
    operator: str | None = None
    organization_name: str | None = None
    supplier_id: int | None = None
    contact_id: int | None = None
    provider_id: int | None = None
    government_contact_id: int | None = None
    external_call_id: str | None = None


@router.post("/{deal_id}/transcripts")
def paste_deal_transcript(deal_id: int, body: TranscriptPasteBody) -> dict[str, Any]:
    from transcript_service import paste_transcript

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        result = paste_transcript(
            session,
            contract_id=c.id,
            raw_transcript=body.raw_transcript,
            transcript_type=body.transcript_type,
            operator=body.operator,
            organization_name=body.organization_name,
            supplier_id=body.supplier_id,
            contact_id=body.contact_id,
            provider_id=body.provider_id,
            government_contact_id=body.government_contact_id,
            external_call_id=body.external_call_id,
            enqueue_ai=False,
        )
        session.commit()
        return result
    finally:
        session.close()


class AskAboutDealBody(BaseModel):
    question: str
    operator: str | None = None


@router.post("/{deal_id}/ask-about-deal")
def ask_about_deal(deal_id: int, body: AskAboutDealBody) -> dict[str, Any]:
    from ask_about_deal import create_ask_request

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        result = create_ask_request(
            session,
            contract_id=c.id,
            question=body.question,
            operator=body.operator,
            enqueue_ai=False,
        )
        session.commit()
        return result
    finally:
        session.close()


@router.get("/{deal_id}/context-preview")
def get_context_preview(deal_id: int, question: str | None = None) -> dict[str, Any]:
    from ask_about_deal import build_context_preview

    session = SessionLocal()
    try:
        _get_contract_by_id(session, deal_id)
        return build_context_preview(session, deal_id, question=question)
    finally:
        session.close()


@router.get("/{deal_id}/funding-plan")
def get_durable_funding_plan(deal_id: int) -> dict[str, Any]:
    from funding_persistence import load_persisted_funding

    session = SessionLocal()
    try:
        _get_contract_by_id(session, deal_id)
        persisted = load_persisted_funding(session, deal_id)
        return {"funding_plan": persisted, "LIVE_API_REQUESTS": 0}
    finally:
        session.close()


class FundingPathAnalyzeBody(BaseModel):
    opportunity: dict[str, Any] = Field(default_factory=dict)
    economics: dict[str, Any] = Field(default_factory=dict)
    deadline: dict[str, Any] = Field(default_factory=dict)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    supplier: dict[str, Any] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)
    deal_qualified: bool = True


@router.post("/funding-path/analyze")
def analyze_funding_path(body: FundingPathAnalyzeBody) -> dict[str, Any]:
    """Deterministic Funding Path Intelligence — no lender outreach, no paid APIs."""
    from funding_path_intelligence import run_funding_path_workflow

    return run_funding_path_workflow(
        opportunity=body.opportunity,
        economics=body.economics,
        deadline=body.deadline,
        sources=body.sources,
        supplier=body.supplier,
        extras=body.extras,
        deal_qualified=body.deal_qualified,
    )


@router.get("/{deal_id}/funding-path")
def get_deal_funding_path(deal_id: int) -> dict[str, Any]:
    """Serialize funding-path intelligence from workspace snapshot (no outreach)."""
    from funding_path_intelligence import run_funding_path_workflow
    from funding_source_kb import production_knowledge_base_seed

    session = SessionLocal()
    try:
        c = _get_contract_by_id(session, deal_id)
        ws = _workspace(session, c)
        opp = ws.get("opportunity") or {}
        eco = ws.get("economics") or {}
        return {
            **run_funding_path_workflow(
                opportunity=opp,
                economics={
                    "estimated_bid_value": (eco.get("proposed_bid_price") or {}).get("value")
                    or eco.get("estimated_value"),
                    "estimated_supplier_cost": eco.get("supplier_cost")
                    or (eco.get("costs") or {}).get("supplier", {}).get("value"),
                    "estimated_freight": (eco.get("costs") or {}).get("freight", {}).get("value"),
                    "financing_fee_estimate": (eco.get("costs") or {}).get("financing", {}).get("value"),
                },
                deadline={
                    "deadline_viability": opp.get("deadline_viability"),
                    "deadline_runway_days": opp.get("deadline_runway_days"),
                },
                sources=[],  # production KB has zero verified lenders by default
                extras={"funding_stage": "PRE_BID"},
                deal_qualified=True,
            ),
            "production_knowledge_base": production_knowledge_base_seed(),
        }
    finally:
        session.close()

"""Manual-first CRM: one-click logging, notes, Quo-ready transcript architecture."""

from __future__ import annotations
from application_clock import now_utc

from datetime import date, datetime, timezone
from typing import Any

# Manual activity types (no phone/email integration required)
ACT_CALLED = "CALLED"
ACT_LEFT_VOICEMAIL = "LEFT_VOICEMAIL"
ACT_NO_ANSWER = "NO_ANSWER"
ACT_SPOKE_WITH_CONTACT = "SPOKE_WITH_CONTACT"
ACT_EMAILED = "EMAILED"
ACT_RECEIVED_EMAIL = "RECEIVED_EMAIL"
ACT_QUOTE_REQUESTED = "QUOTE_REQUESTED"
ACT_QUOTE_RECEIVED = "QUOTE_RECEIVED"
ACT_FOLLOWUP = "FOLLOWUP"
ACT_NOTE = "NOTE"
ACT_OTHER = "OTHER"
ACT_RECEIVED_TERMS = "RECEIVED_TERMS"
ACT_FOLLOW_UP_NEEDED = "FOLLOW_UP_NEEDED"
ACT_QUO_CALL_IMPORTED = "QUO_CALL_IMPORTED"  # future — schema ready

MANUAL_ACTIVITY_TYPES = frozenset(
    {
        ACT_CALLED,
        ACT_LEFT_VOICEMAIL,
        ACT_NO_ANSWER,
        ACT_SPOKE_WITH_CONTACT,
        ACT_EMAILED,
        ACT_RECEIVED_EMAIL,
        ACT_QUOTE_REQUESTED,
        ACT_QUOTE_RECEIVED,
        ACT_RECEIVED_TERMS,
        ACT_FOLLOWUP,
        ACT_FOLLOW_UP_NEEDED,
        ACT_NOTE,
        ACT_OTHER,
        ACT_QUO_CALL_IMPORTED,
    }
)

PROVENANCE_VERIFIED_CONTACT = "VERIFIED_CONTACT_FACT"
PROVENANCE_OPERATOR_REPORTED = "OPERATOR_REPORTED"
PROVENANCE_SYSTEM_OBSERVATION = "SYSTEM_OBSERVATION"
PROVENANCE_AI_ASSESSMENT = "AI_ASSESSMENT"

# Quo / transcript pipeline statuses — never auto-VERIFIED
TRANSCRIPT_RAW = "TRANSCRIPT_RAW"
AI_EXTRACTED_PROPOSAL = "AI_EXTRACTED_PROPOSAL"
OPERATOR_CONFIRMED = "OPERATOR_CONFIRMED"
DOCUMENT_VERIFIED = "DOCUMENT_VERIFIED"

NOTE_ENTITY_TYPES = frozenset(
    {
        "opportunity",
        "supplier",
        "supplier_contact",
        "government_contact",
        "financing_provider",
        "quote",
        "bid_package",
    }
)


def utc_now() -> datetime:
    return now_utc()


def log_manual_activity(
    session: Any,
    *,
    contract_id: int,
    activity_type: str,
    operator: str | None = None,
    supplier_id: int | None = None,
    contact_id: int | None = None,
    government_contact_id: int | None = None,
    what_happened: str | None = None,
    outcome: str | None = None,
    next_action: str | None = None,
    next_action_at: datetime | date | str | None = None,
    organization_name: str | None = None,
) -> dict[str, Any]:
    """
    One-click CRM log: auto-timestamp, update last_contact, optional follow-up.
    NEVER triggers AI or external calls.
    """
    from models import CrmActivity, GovernmentContact, KnowledgeSupplier, SupplierContact

    atype = str(activity_type or ACT_OTHER).upper()
    if atype not in MANUAL_ACTIVITY_TYPES:
        atype = ACT_OTHER
    now = utc_now()

    nad: date | None = None
    if isinstance(next_action_at, datetime):
        nad = next_action_at.date()
    elif isinstance(next_action_at, date):
        nad = next_action_at
    elif next_action_at:
        try:
            nad = date.fromisoformat(str(next_action_at)[:10])
        except ValueError:
            nad = None

    summary_parts = [what_happened or "", f"Outcome: {outcome}" if outcome else ""]
    summary = " | ".join(p for p in summary_parts if p).strip() or atype

    facts = [
        {
            "field": "activity_type",
            "value": atype,
            "verification_status": PROVENANCE_OPERATOR_REPORTED,
        }
    ]
    if outcome:
        facts.append(
            {
                "field": "outcome",
                "value": outcome,
                "verification_status": PROVENANCE_OPERATOR_REPORTED,
            }
        )

    row = CrmActivity(
        contract_id=contract_id,
        supplier_id=supplier_id,
        contact_id=contact_id,
        activity_type=atype,
        summary=summary,
        facts_learned_json=facts,
        operator=operator,
        next_action=next_action,
        next_action_date=nad,
        activity_at=now,
    )
    # Optional government contact link via facts until FK column exists on all DBs
    if government_contact_id:
        facts.append(
            {
                "field": "government_contact_id",
                "value": government_contact_id,
                "verification_status": PROVENANCE_OPERATOR_REPORTED,
            }
        )
        row.facts_learned_json = facts
    if organization_name:
        facts.append(
            {
                "field": "organization",
                "value": organization_name,
                "verification_status": PROVENANCE_OPERATOR_REPORTED,
            }
        )
        row.facts_learned_json = facts

    session.add(row)
    session.flush()

    # Update last_contact_at on supplier / contacts
    if supplier_id:
        sup = session.query(KnowledgeSupplier).filter_by(id=supplier_id).first()
        if sup is not None:
            sup.last_contact_at = now
            if nad:
                from datetime import datetime as dt

                sup.next_followup_at = dt.combine(nad, dt.min.time()).replace(tzinfo=timezone.utc)
    if contact_id:
        # contact row may get notes only; last contact stored on supplier
        pass
    if government_contact_id:
        gc = session.query(GovernmentContact).filter_by(id=government_contact_id).first()
        if gc is not None:
            gc.last_contact_at = now
            if nad:
                gc.next_followup_at = nad

    if atype == ACT_QUOTE_REQUESTED and supplier_id:
        from models import DealState

        deal = session.query(DealState).filter_by(contract_id=contract_id).first()
        if deal is not None:
            checkpoint = dict(deal.funnel_checkpoint_json or {})
            commercial = dict(checkpoint.get("commercial") or {})
            commercial["quote_requested"] = True
            commercial["quote_requested_at"] = now.isoformat()
            commercial["quote_requested_supplier_id"] = supplier_id
            checkpoint["commercial"] = commercial
            deal.funnel_checkpoint_json = checkpoint

    return {
        "id": row.id,
        "activity_type": atype,
        "activity_at": now.isoformat(),
        "timestamp_auto": True,
        "operator": operator,
        "next_action": next_action,
        "next_action_date": nad.isoformat() if nad else None,
        "LIVE_API_REQUESTS": 0,
        "paid_ai": False,
        "external_contact": False,
    }


def add_operator_note(
    session: Any,
    *,
    entity_type: str,
    entity_id: int,
    text: str,
    author: str | None = None,
    contract_id: int | None = None,
) -> dict[str, Any]:
    from models import OperatorNote

    et = str(entity_type or "").lower()
    if et not in NOTE_ENTITY_TYPES:
        et = "opportunity"
    row = OperatorNote(
        entity_type=et,
        entity_id=entity_id,
        contract_id=contract_id,
        text=text or "",
        author=author,
        provenance=PROVENANCE_OPERATOR_REPORTED,
    )
    session.add(row)
    session.flush()
    return {
        "id": row.id,
        "entity_type": et,
        "entity_id": entity_id,
        "provenance": PROVENANCE_OPERATOR_REPORTED,
        "created_at": row.created_at.isoformat() if row.created_at else utc_now().isoformat(),
        "LIVE_API_REQUESTS": 0,
    }


def promote_transcript_fact(
    *,
    current_status: str,
    target_status: str,
    operator_confirmed: bool = False,
    document_evidence: bool = False,
) -> dict[str, Any]:
    """
    Transcript-derived facts NEVER become VERIFIED automatically.
    VERIFIED (DOCUMENT_VERIFIED) requires document evidence path.
    """
    cur = str(current_status or TRANSCRIPT_RAW)
    tgt = str(target_status or "")
    if tgt in {"VERIFIED", DOCUMENT_VERIFIED}:
        if not document_evidence:
            return {
                "allowed": False,
                "status": cur,
                "reason": "transcript_fact_cannot_become_verified_automatically",
            }
        return {"allowed": True, "status": DOCUMENT_VERIFIED}
    if tgt == OPERATOR_CONFIRMED:
        if not operator_confirmed:
            return {"allowed": False, "status": cur, "reason": "operator_confirmation_required"}
        return {"allowed": True, "status": OPERATOR_CONFIRMED}
    if tgt == AI_EXTRACTED_PROPOSAL:
        return {"allowed": True, "status": AI_EXTRACTED_PROPOSAL, "is_fact": False}
    return {"allowed": True, "status": TRANSCRIPT_RAW, "is_fact": False}


def build_supplier_relationship_profile(session: Any, supplier_id: int) -> dict[str, Any]:
    """Factual aggregation only — no fabricated relationship score."""
    from models import CrmActivity, SupplierOffer, SupplierPursuitPlan, KnowledgeSupplier

    sup = session.query(KnowledgeSupplier).filter_by(id=supplier_id).first()
    if not sup:
        return {"error": "supplier_not_found", "LIVE_API_REQUESTS": 0}
    activities = session.query(CrmActivity).filter_by(supplier_id=supplier_id).all()
    offers = session.query(SupplierOffer).filter_by(supplier_id=supplier_id).all()
    plans = session.query(SupplierPursuitPlan).filter_by(supplier_id=supplier_id).all()
    quote_requested = sum(1 for a in activities if a.activity_type == ACT_QUOTE_REQUESTED)
    quote_received = sum(1 for a in activities if a.activity_type == ACT_QUOTE_RECEIVED)
    opp_ids = sorted(
        {
            *(a.contract_id for a in activities if a.contract_id),
            *(o.contract_id for o in offers if o.contract_id),
            *(p.contract_id for p in plans if p.contract_id),
        }
    )
    # Average turnaround if both request and receive timestamps exist (CALCULATED)
    turnaround_days = None
    req_times = [a.activity_at for a in activities if a.activity_type == ACT_QUOTE_REQUESTED and a.activity_at]
    recv_times = [a.activity_at for a in activities if a.activity_type == ACT_QUOTE_RECEIVED and a.activity_at]
    if req_times and recv_times:
        deltas = []
        for r in req_times:
            later = [x for x in recv_times if x and x >= r]
            if later:
                deltas.append((min(later) - r).total_seconds() / 86400.0)
        if deltas:
            turnaround_days = {
                "value": round(sum(deltas) / len(deltas), 2),
                "status": "CALCULATED",
                "n": len(deltas),
            }

    return {
        "supplier_id": supplier_id,
        "name": sup.name,
        "website": sup.website,
        "manufacturer_relationship": {
            "value": sup.manufacturer_relationship,
            "status": sup.verification_status or "UNKNOWN",
        },
        "opportunities_worked": opp_ids,
        "quotes_requested": quote_requested,
        "quotes_received": quote_received,
        "average_quote_turnaround_days": turnaround_days,
        "last_contact_at": sup.last_contact_at.isoformat() if sup.last_contact_at else None,
        "next_followup_at": sup.next_followup_at.isoformat() if getattr(sup, "next_followup_at", None) else None,
        "federal_capability": getattr(sup, "federal_capability", None),
        "relationship_score": None,  # intentionally absent — do not fabricate
        "LIVE_API_REQUESTS": 0,
    }


def build_co_relationship_profile(session: Any, government_contact_id: int) -> dict[str, Any]:
    from models import CrmActivity, GovernmentContact, OperatorNote

    gc = session.query(GovernmentContact).filter_by(id=government_contact_id).first()
    if not gc:
        return {"error": "government_contact_not_found", "LIVE_API_REQUESTS": 0}
    # Activities that mention this contact
    acts = session.query(CrmActivity).all()
    linked = []
    for a in acts:
        facts = a.facts_learned_json or []
        if any(
            isinstance(f, dict) and f.get("field") == "government_contact_id" and f.get("value") == government_contact_id
            for f in facts
        ):
            linked.append(a)
        elif a.contract_id and gc.contract_id and a.contract_id == gc.contract_id:
            if a.activity_type in {ACT_EMAILED, ACT_CALLED, ACT_RECEIVED_EMAIL, ACT_SPOKE_WITH_CONTACT}:
                linked.append(a)
    notes = (
        session.query(OperatorNote)
        .filter_by(entity_type="government_contact", entity_id=government_contact_id)
        .all()
    )
    response_times = None  # calculable later from send/receive pairs
    return {
        "id": gc.id,
        "name": gc.name,
        "agency": gc.agency,
        "office": gc.office,
        "title": gc.title,
        "phone": {"value": gc.phone, "status": gc.contact_provenance or PROVENANCE_OPERATOR_REPORTED},
        "email": {"value": gc.email, "status": gc.contact_provenance or PROVENANCE_OPERATOR_REPORTED},
        "opportunities": [gc.contract_id] if gc.contract_id else [],
        "communication_history_count": len(linked),
        "clarification_history": gc.clarification_history_json or [],
        "observed_response_times": response_times,
        "last_contact_at": gc.last_contact_at.isoformat() if gc.last_contact_at else None,
        "next_followup_at": gc.next_followup_at.isoformat() if gc.next_followup_at else None,
        "documented_instructions": gc.documented_instructions,
        "notes_count": len(notes),
        "relationship_score": None,
        "LIVE_API_REQUESTS": 0,
    }


def build_financier_relationship_profile(session: Any, provider_id: int) -> dict[str, Any]:
    """Financier history across deals — factual aggregation only."""
    from models import CrmActivity, FinancingPursuit, FinancingProvider, FinancingTerm

    prov = session.query(FinancingProvider).filter_by(id=provider_id).first()
    if not prov:
        return {"error": "provider_not_found", "LIVE_API_REQUESTS": 0}
    pursuits = session.query(FinancingPursuit).filter_by(provider_id=provider_id).all()
    terms = session.query(FinancingTerm).filter_by(provider_id=provider_id).all()
    opp_ids = sorted({p.contract_id for p in pursuits if p.contract_id})
    activities = session.query(CrmActivity).all()
    linked = [
        a
        for a in activities
        if any(
            (f.get("field") == "provider_id" and f.get("value") == provider_id)
            for f in (a.facts_learned_json or [])
        )
    ]
    return {
        "provider_id": provider_id,
        "name": prov.name,
        "opportunities_pursued": opp_ids,
        "terms_on_file": len(terms),
        "pursuits_count": len(pursuits),
        "crm_touchpoints": len(linked),
        "profile": prov.profile_json or {},
        "verification_status": prov.verification_status,
        "last_verified_at": prov.last_verified_at.isoformat() if prov.last_verified_at else None,
        "LIVE_API_REQUESTS": 0,
    }

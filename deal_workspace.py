"""Deal Workspace assembly + local reconciliation helpers (zero external calls)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bid_package import bid_package_from_workspace, empty_bid_package
from bom_gate import evaluate_bom_completeness
from application_clock import now_utc
from company_profile import ensure_default_company_profile, load_company_profile
from deal_context import (
    format_deadline_close_label,
    oem_letter_required_from_requirements,
    required_quantity_from_bom,
)
from deal_readiness import evaluate_bid_readiness, evaluate_deal_readiness
from quote_validation import default_negotiation_suggestions
from requirement_register import REQ_UNKNOWN, REQ_VERIFIED_REQUIREMENT, requirement_record
from solicitation_package import PACKAGE_UNRESOLVED, document_record, evaluate_solicitation_package

# Regression fixture IDs — production logic must not branch on these; reconcile script only.
OPP199_ID = 199
OPP199_NOTICE = "610bea2df5c1436994e316259e29a9a6"


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def build_workspace_snapshot(session: Any, contract: Any) -> dict[str, Any]:
    """Read-only aggregate for Deal Workspace GET — no paid/external calls."""
    from models import (
        BidPackage,
        ContractAttachment,
        CrmActivity,
        DealState,
        FinancingPursuit,
        FinancingTerm,
        KnowledgeSupplier,
        RequirementRegisterItem,
        SolicitationDocument,
        SupplierOffer,
        SupplierPursuitPlan,
    )

    profile = load_company_profile(session)
    deal = session.query(DealState).filter_by(contract_id=contract.id).first()
    checkpoint = (deal.funnel_checkpoint_json if deal else None) or {}
    bom = checkpoint.get("bom") if isinstance(checkpoint.get("bom"), list) else []
    if not bom and isinstance(checkpoint.get("stage3"), dict):
        bom = checkpoint["stage3"].get("bom") or []

    docs = session.query(SolicitationDocument).filter_by(contract_id=contract.id).all()
    doc_dicts = [
        document_record(
            document_type=d.document_type,
            source=d.source,
            url=d.url,
            retrieved_at=_iso(d.retrieved_at),
            version_amendment=d.version_amendment,
            content_hash=d.content_hash,
            text_extraction_status=d.text_extraction_status or "UNKNOWN",
            superseded=bool(d.superseded),
            current=bool(d.current),
            required_for_bid=d.required_for_bid,
            review_status=d.review_status or "UNKNOWN",
            filename=d.filename,
            local_attachment_id=d.local_attachment_id,
        )
        for d in docs
    ]
    # Fallback: attachment inventory without inventing completeness
    if not doc_dicts:
        atts = session.query(ContractAttachment).filter_by(contract_id=contract.id).all()
        for a in atts:
            doc_dicts.append(
                document_record(
                    document_type="attachment",
                    source=a.source or "local_attachment",
                    filename=a.filename,
                    retrieved_at=_iso(a.downloaded_at),
                    text_extraction_status="EXTRACTED" if a.extracted_text else "UNKNOWN",
                    local_attachment_id=a.id,
                    current=True,
                    required_for_bid=None,
                    review_status="UNKNOWN",
                )
            )

    pkg = evaluate_solicitation_package(
        doc_dicts,
        amendments_expected=None,
        amendments_accounted=None,
        required_types_missing=None,
    )
    # Prefer evidence-driven completeness from checkpoint when present
    if isinstance(checkpoint.get("package_completeness"), dict):
        pkg = dict(checkpoint["package_completeness"])
        if "status" not in pkg and checkpoint["package_completeness"].get("status"):
            pkg = checkpoint["package_completeness"]


    reqs = (
        session.query(RequirementRegisterItem)
        .filter_by(contract_id=contract.id)
        .order_by(RequirementRegisterItem.id.asc())
        .all()
    )
    req_dicts = [
        {
            "id": r.id,
            "requirement_key": r.requirement_key,
            "description": r.description,
            "type": r.requirement_type,
            "required": r.required,
            "status": r.status,
            "source_document": r.source_document,
            "source_location": r.source_location,
            "evidence": r.evidence,
            "verified_status": r.verified_status,
            "owner": r.owner,
            "due_date": r.due_date.isoformat() if r.due_date else None,
            "satisfied_by": r.satisfied_by,
            "notes": r.notes,
        }
        for r in reqs
    ]

    bom_gate = evaluate_bom_completeness(bom)
    req_qty = required_quantity_from_bom(bom)

    offers = session.query(SupplierOffer).filter_by(contract_id=contract.id).all()
    from commercial_quotes import (
        bom_memory_per_server,
        bom_storage_per_server,
        compare_supplier_quotes,
        validate_quote_against_bom,
    )

    mem_req = bom_memory_per_server(bom)
    stor_req = bom_storage_per_server(bom)
    quote_dicts = []
    for o in offers:
        details = o.quote_details_json if isinstance(o.quote_details_json, dict) else {}
        qrow = {
            "id": o.id,
            "supplier_id": o.supplier_id,
            "quote_number": o.quote_number,
            "unit_price": float(o.unit_price) if o.unit_price is not None else None,
            "extended_price": float(o.extended_price) if o.extended_price is not None else None,
            "quantity": float(o.quantity_basis) if o.quantity_basis is not None else None,
            "quantity_basis": float(o.quantity_basis) if o.quantity_basis is not None else None,
            "verification_status": o.verification_status,
            "validation_status": o.validation_status,
            "bom_match": o.bom_match,
            "expiration_date": o.expiration_date.isoformat() if o.expiration_date else None,
            "source_type": o.source_type,
            "temporal_class": o.temporal_class,
            "availability": o.availability,
            "lead_time": o.lead_time,
            "shipping_included": o.shipping_included,
            "source_url": o.source_url,
            "notes": o.notes,
            "quote_details": details,
            "freight": details.get("freight"),
            "freight_status": details.get("freight_status"),
            "total": details.get("total"),
            "payment_terms": details.get("payment_terms"),
            "oem_letter_available": details.get("oem_letter_available"),
            "federal_channel_confirmed": details.get("federal_channel_confirmed"),
            "upfront_payment_required": details.get("upfront_payment_required"),
            "memory_modules_per_server_quoted": details.get("memory_modules_per_server_quoted"),
            "storage_drives_per_server_quoted": details.get("storage_drives_per_server_quoted"),
            "supplier_name": None,
        }
        if details.get("validation"):
            qrow["validation"] = details["validation"]
        else:
            qrow["validation"] = validate_quote_against_bom(
                {**qrow, **details},
                required_bom=bom,
                required_quantity=req_qty or qrow.get("quantity") or 1,
                required_memory_per_server=mem_req,
                required_storage_per_server=stor_req,
            )
            qrow["validation_status"] = qrow["validation"].get("status")
        quote_dicts.append(qrow)

    supplier_ids = {o.supplier_id for o in offers if o.supplier_id}
    pursuits = session.query(SupplierPursuitPlan).filter_by(contract_id=contract.id).all()
    for p in pursuits:
        if p.supplier_id:
            supplier_ids.add(p.supplier_id)
    suppliers = []
    if supplier_ids:
        for s in session.query(KnowledgeSupplier).filter(KnowledgeSupplier.id.in_(supplier_ids)).all():
            suppliers.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "website": s.website,
                    "manufacturer_relationship": s.manufacturer_relationship,
                    "verification_status": s.verification_status,
                    "federal_capability": s.federal_capability,
                    "relationship_strength": s.relationship_strength,
                    "source": s.source,
                    "notes": s.notes,
                    "phone": None,  # only surface if verified elsewhere — never invent
                    "last_contact_at": _iso(getattr(s, "last_contact_at", None)),
                    "next_followup_at": _iso(getattr(s, "next_followup_at", None)),
                }
            )
    name_by_id = {s["id"]: s["name"] for s in suppliers}
    for q in quote_dicts:
        q["supplier_name"] = name_by_id.get(q.get("supplier_id"))

    pursuit_dicts = [
        {
            "id": p.id,
            "supplier_id": p.supplier_id,
            "priority": p.priority,
            "contact_objective": p.contact_objective,
            "quantity": float(p.quantity) if p.quantity is not None else None,
            "destination": p.destination,
            "delivery_deadline": p.delivery_deadline,
            "authorization_requirement": p.authorization_requirement,
            "negotiation_status": p.negotiation_status,
            "negotiation_suggestions": p.negotiation_suggestions_json,
            "next_action": p.next_action,
            "quote_request_ready": bool(p.quote_request_ready),
            "questions": p.questions_json,
        }
        for p in pursuits
    ]

    activities = (
        session.query(CrmActivity)
        .filter_by(contract_id=contract.id)
        .order_by(CrmActivity.id.desc())
        .limit(100)
        .all()
    )
    activity_dicts = [
        {
            "id": a.id,
            "activity_type": a.activity_type,
            "summary": a.summary,
            "supplier_id": a.supplier_id,
            "contact_id": a.contact_id,
            "operator": a.operator,
            "facts_learned": a.facts_learned_json,
            "next_action": a.next_action,
            "next_action_date": a.next_action_date.isoformat() if a.next_action_date else None,
            "activity_at": _iso(a.activity_at),
        }
        for a in activities
    ]

    fin_pursuits = session.query(FinancingPursuit).filter_by(contract_id=contract.id).all()
    fin_terms = session.query(FinancingTerm).filter_by(contract_id=contract.id).all()
    financing_view = {
        "status": "FINANCING_UNRESOLVED",
        "pursuits": [
            {
                "id": f.id,
                "provider_id": f.provider_id,
                "approval_status": f.approval_status,
                "pg_required": f.pg_required,
                "personal_credit_required": f.personal_credit_required,
                "borrower_cash_required": f.borrower_cash_required,
                "supplier_direct_payment": f.supplier_direct_payment,
                "verification_status": f.verification_status,
                "requested_amount": float(f.requested_amount) if f.requested_amount is not None else None,
                "notes": f.notes,
            }
            for f in fin_pursuits
        ],
        "terms": [
            {
                "id": t.id,
                "provider_id": t.provider_id,
                "pg_required": t.pg_required,
                "personal_credit_required": t.personal_credit_required,
                "cash_deposit_required": t.cash_deposit_required,
                "verification_status": t.verification_status,
                "is_generic_marketing": t.is_generic_marketing,
                "fee": float(t.fee) if t.fee is not None else None,
            }
            for t in fin_terms
        ],
    }
    # Derive financing status from pursuits/terms — never invent PASS
    if any(f.approval_status == "FINANCING_PASS" and f.verification_status == "VERIFIED" for f in fin_pursuits):
        financing_view["status"] = "FINANCING_PASS"
    elif fin_pursuits or fin_terms:
        financing_view["status"] = "FINANCING_UNRESOLVED"

    economics = (deal.economics_json if deal else None) or {}
    channel_status = None
    delivery_status = None
    compliance = {}
    if isinstance(checkpoint.get("stage3"), dict):
        s3 = checkpoint["stage3"]
        channel_status = (s3.get("channel") or {}).get("status") if isinstance(s3.get("channel"), dict) else s3.get("channel_status")
        delivery_status = (s3.get("delivery") or {}).get("status") if isinstance(s3.get("delivery"), dict) else s3.get("delivery_status")
        compliance = s3.get("compliance") or {}
    # Also peek funnel checkpoint top-level
    channel_status = channel_status or checkpoint.get("channel_status")
    delivery_status = delivery_status or checkpoint.get("delivery_status")

    costs = economics.get("costs") if isinstance(economics.get("costs"), dict) else {}
    deal_rd = evaluate_deal_readiness(
        bom=bom,
        bom_gate=bom_gate,
        quotes=quote_dicts,
        required_quantity=req_qty,
        channel_status=channel_status or "CHANNEL_UNRESOLVED",
        delivery_status=delivery_status or "DELIVERY_UNRESOLVED",
        freight_cost=costs.get("freight"),
        installation_cost=costs.get("installation"),
        subcontract_cost=costs.get("subcontract"),
        compliance=compliance or {"baa": "UNKNOWN", "taa": "UNKNOWN"},
        financing=financing_view,
        economics=economics,
        company_profile=profile,
        availability_verified=False,
    )

    bp_row = session.query(BidPackage).filter_by(contract_id=contract.id).first()
    bid_pkg = (
        bp_row.package_json
        if bp_row and isinstance(bp_row.package_json, dict)
        else empty_bid_package(
            solicitation_number=getattr(contract, "solicitation_number", None),
            notice_id=contract.notice_id,
            contract_id=contract.id,
        )
    )
    if deal and deal.operator_bid_amount is not None:
        bid_pkg = bid_package_from_workspace(
            solicitation_number=None,
            notice_id=contract.notice_id,
            contract_id=contract.id,
            operator_bid_amount=float(deal.operator_bid_amount),
            deadline=contract.due_date.isoformat() if contract.due_date else None,
        )

    bid_rd = evaluate_bid_readiness(
        deal_readiness=deal_rd,
        solicitation_package=pkg,
        documents=doc_dicts,
        amendments_accounted=None,
        amendments_expected=None,
        requirements=req_dicts,
        bid_package=bid_pkg,
        oem_letter_required=oem_letter_required_from_requirements(req_dicts),
        oem_letters_attached=False,
        mandatory_attachments_present=False,
        submission_method_verified=False,
        deadline_verified=bool(contract.due_date),
        forms_complete=False,
        signatures_complete=False,
        pricing_complete=False,
    )

    # Missing-info items (read persisted; do not re-run search on every GET)
    from models import MissingInfoItem, GovernmentContact, OperatorNote

    missing_rows = (
        session.query(MissingInfoItem).filter_by(contract_id=contract.id).order_by(MissingInfoItem.id).all()
    )
    missing_items = [
        {
            "id": m.id,
            "fact_key": m.fact_key,
            "description": m.description,
            "fact_class": m.fact_class,
            "status": m.status,
            "safe_to_ask_co": m.safe_to_ask_co,
            "confidence_absent": m.confidence_absent,
            "co_gate": m.co_gate_json,
            "question_draft": m.question_draft_json,
            "search_audit": {
                "documents_checked_count": (m.search_audit_json or {}).get("documents_checked_count"),
                "search_terms": (m.search_audit_json or {}).get("search_terms"),
                "overall_match_class": (m.search_audit_json or {}).get("overall_match_class"),
                "match_count": (m.search_audit_json or {}).get("match_count"),
                "possible_indirect_remaining": (m.search_audit_json or {}).get("possible_indirect_remaining"),
            }
            if m.search_audit_json
            else None,
            "second_pass_status": m.second_pass_status,
        }
        for m in missing_rows
    ]

    snap_partial = {
        "opportunity": {
            "id": contract.id,
            "notice_id": contract.notice_id,
            "title": contract.title,
            "agency": contract.agency,
            "due_date": contract.due_date.isoformat() if contract.due_date else None,
            "set_aside": contract.set_aside,
            "core_fit": deal.core_fit if deal else None,
            "pipeline_stage": deal.pipeline_stage if deal else None,
        },
        "solicitation_package": pkg,
        "bom_gate": bom_gate,
        "deal_readiness": deal_rd,
        "bid_readiness": bid_rd,
        "pursuit_plans": pursuit_dicts,
        "activities": activity_dicts,
        "financing": financing_view,
        "quotes": quote_dicts,
        "economics": economics,
        "channel_status": channel_status,
        "commercial": (checkpoint.get("commercial") if isinstance(checkpoint, dict) else None) or {},
        "deadline_urgency": None,  # filled below before regenerate if needed
    }
    from next_action_engine import build_today_queue, generate_next_actions

    # Deadline urgency (display only — no auto-reject)
    deadline_urgency = None
    if contract.due_date:
        from datetime import datetime, timezone as tz

        try:
            from zoneinfo import ZoneInfo

            close_local = datetime(
                contract.due_date.year,
                contract.due_date.month,
                contract.due_date.day,
                17,
                0,
                0,
                tzinfo=ZoneInfo("America/Los_Angeles"),
            )
            hours = (close_local.astimezone(tz.utc) - now_utc()).total_seconds() / 3600.0
        except Exception:
            hours = None
        deadline_urgency = {
            "stored_deadline": contract.due_date.isoformat(),
            "close_label": format_deadline_close_label(contract) or contract.due_date.isoformat(),
            "hours_remaining_approx": round(hours, 2) if hours is not None else None,
            "urgent": hours is not None and hours < 48,
        }

    snap_partial["deadline_urgency"] = deadline_urgency
    next_actions = generate_next_actions(workspace=snap_partial, missing_items=missing_items)
    today_queue = build_today_queue(next_actions)

    gov_contacts = session.query(GovernmentContact).filter_by(contract_id=contract.id).all()
    notes = (
        session.query(OperatorNote)
        .filter_by(contract_id=contract.id)
        .order_by(OperatorNote.id.desc())
        .limit(50)
        .all()
    )

    quote_comparison = compare_supplier_quotes(quote_dicts)

    commercial_artifacts = checkpoint.get("commercial_artifacts") if isinstance(checkpoint, dict) else None
    commercial_meta = dict((checkpoint.get("commercial") if isinstance(checkpoint, dict) else None) or {})
    if deal and deal.operator_bid_amount is not None:
        commercial_meta.setdefault(
            "proposed_bid",
            {
                "amount": float(deal.operator_bid_amount),
                "fact_class": "COMPANY_PROPOSED_BID_PRICE",
                "is_government_verified_value": False,
            },
        )

    # Commercial status summary for operator
    has_valid_quote = any(
        (q.get("validation") or {}).get("status") == "QUOTE_VALID" for q in quote_dicts
    )
    commercial_status = {
        "label": "NEEDS_SUPPLIER_QUOTES" if not has_valid_quote else "QUOTES_IN_PROGRESS",
        "has_valid_quote": has_valid_quote,
        "quote_requested": bool(commercial_meta.get("quote_requested")),
        "channel_status": channel_status or commercial_meta.get("channel_status") or "CHANNEL_UNRESOLVED",
        "financing_status": financing_view.get("status"),
        "proposed_bid_set": deal.operator_bid_amount is not None if deal else False,
    }

    from deal_lifecycle import map_internal_to_lifecycle, pipeline_bucket
    from funding_engine import build_funding_plan_snapshot
    from funding_research import build_funding_research_plan

    funding_plan = build_funding_plan_snapshot(
        contract_id=contract.id,
        pursuits=financing_view.get("pursuits"),
        terms=financing_view.get("terms"),
        quotes=quote_dicts,
        economics=economics,
        persisted=(checkpoint.get("funding_plan") if isinstance(checkpoint, dict) else None),
    )
    lifecycle = map_internal_to_lifecycle(
        decision=deal.decision if deal else None,
        pipeline_stage=deal.pipeline_stage if deal else None,
        deal_readiness=deal_rd,
        bid_readiness=bid_rd,
        commercial_status=commercial_status,
        funding_status=funding_plan.get("pre_bid_status"),
    )
    pipeline_b = pipeline_bucket(
        lifecycle=lifecycle,
        workspace={
            "solicitation_package": pkg,
            "bom_gate": bom_gate,
            "commercial_status": commercial_status,
            "deal_readiness": deal_rd,
            "bid_readiness": bid_rd,
            "funding_plan": funding_plan,
            "next_action": next_actions.get("next_action"),
        },
    )
    funding_research = build_funding_research_plan(
        funding_plan=funding_plan,
        providers=[{"id": p.get("provider_id"), "name": p.get("provider_id")} for p in financing_view.get("pursuits") or []],
    )

    from funding_persistence import load_persisted_funding, sync_funding_plan
    from exception_engine import recompute_deal_warnings
    from transcript_service import list_transcripts
    from award_lifecycle import award_lifecycle_to_dict, get_or_create_award_lifecycle, post_award_next_action
    from ai_operating_modes import get_operating_mode, mode_config

    fp_sync = dict(funding_plan)
    fp_sync["_quotes"] = quote_dicts
    sync_funding_plan(
        session,
        contract_id=contract.id,
        funding_plan_view=fp_sync,
        pursuits=financing_view.get("pursuits"),
    )
    persisted_fp = load_persisted_funding(session, contract.id)
    if persisted_fp:
        funding_plan["persisted"] = persisted_fp
        funding_plan["durable_id"] = persisted_fp.get("id")

    award_row = get_or_create_award_lifecycle(session, contract.id)
    award_view = award_lifecycle_to_dict(award_row)
    transcripts = list_transcripts(session, contract.id)

    result = {
        "LIVE_API_REQUESTS": 0,
        "paid_ai_on_load": False,
        "external_calls": {
            "SAM": 0,
            "OpenAI": 0,
            "web": 0,
            "USAspending": 0,
            "supplier": 0,
            "financing": 0,
            "email": 0,
            "Quo": 0,
        },
        "opportunity": snap_partial["opportunity"],
        "company_profile": profile,
        "documents": doc_dicts,
        "solicitation_package": pkg,
        "requirements": req_dicts,
        "bom": bom,
        "bom_gate": bom_gate,
        "suppliers": suppliers,
        "pursuit_plans": pursuit_dicts,
        "activities": activity_dicts,
        "quotes": quote_dicts,
        "quote_comparison": quote_comparison,
        "financing": financing_view,
        "economics": economics,
        "deal_readiness": deal_rd,
        "bid_readiness": bid_rd,
        "bid_package": bid_pkg,
        "negotiation_suggestions": default_negotiation_suggestions(),
        "missing_info": missing_items,
        "next_action": next_actions.get("next_action"),
        "action_queue": next_actions.get("queue"),
        "today_queue": today_queue,
        "deadline_urgency": deadline_urgency,
        "package_manifest": checkpoint.get("package_manifest") or [],
        "commercial": commercial_meta,
        "commercial_status": commercial_status,
        "commercial_artifacts": commercial_artifacts,
        "commercial_plan": (commercial_artifacts or {}).get("commercial_plan"),
        "supplier_rfq_packet": (commercial_artifacts or {}).get("supplier_rfq_packet"),
        "supplier_call_sheets": (commercial_artifacts or {}).get("supplier_call_sheets"),
        "supplier_call_script": (commercial_artifacts or {}).get("supplier_call_script"),
        "negotiation_playbook": (commercial_artifacts or {}).get("negotiation_playbook"),
        "financing_packet": (commercial_artifacts or {}).get("financing_packet"),
        "co_clarification_card": (commercial_artifacts or {}).get("co_clarification_card"),
        "proposed_bid": commercial_meta.get("proposed_bid"),
        "lifecycle": lifecycle,
        "pipeline_bucket": pipeline_b,
        "funding_plan": funding_plan,
        "funding_research": funding_research,
        "award_lifecycle": award_view,
        "post_award_next_action": post_award_next_action(award_view),
        "transcripts": transcripts,
        "ai_operating_mode": get_operating_mode(),
        "ai_mode_config": mode_config(),
        "government_contacts": [
            {
                "id": g.id,
                "name": g.name,
                "agency": g.agency,
                "email": g.email,
                "phone": g.phone,
                "contact_provenance": g.contact_provenance,
            }
            for g in gov_contacts
        ],
        "notes": [
            {
                "id": n.id,
                "entity_type": n.entity_type,
                "entity_id": n.entity_id,
                "text": n.text,
                "author": n.author,
                "provenance": n.provenance,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in notes
        ],
    }
    result["warnings"] = recompute_deal_warnings(session, contract.id, result)
    na_final = generate_next_actions(workspace=result, missing_items=missing_items)
    result["next_action"] = na_final.get("next_action")
    result["action_queue"] = na_final.get("queue")
    result["today_queue"] = build_today_queue(na_final)
    return result


def reconcile_opportunity_199_workspace(session: Any) -> dict[str, Any]:
    """Regression reconcile — delegates to fixtures (Opp 199 only)."""
    from fixtures.reconcile_opp199 import reconcile_opportunity_199_workspace as _reconcile

    return _reconcile(session)



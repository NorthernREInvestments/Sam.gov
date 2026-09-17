"""Global nav views — suppliers, contacts, funding work, bids, awards."""

from __future__ import annotations
from application_clock import now_utc, today_local

from typing import Any

from award_lifecycle import award_lifecycle_to_dict, get_or_create_award_lifecycle
from funding_engine import FUNDING_BLOCKED, FUNDING_NEEDS_CONTACT, FUNDING_VIABLE, FUNDING_WAITING_ON_PROVIDER
from provider_knowledge import provider_profile_view


def build_suppliers_view(session: Any, *, search: str | None = None) -> dict[str, Any]:
    from models import CrmActivity, KnowledgeSupplier, SupplierContact, SupplierOffer

    q = session.query(KnowledgeSupplier)
    if search:
        q = q.filter(KnowledgeSupplier.name.ilike(f"%{search}%"))
    suppliers = q.order_by(KnowledgeSupplier.name.asc()).limit(200).all()
    rows = []
    for s in suppliers:
        offers = session.query(SupplierOffer).filter_by(supplier_id=s.id).all()
        activities = (
            session.query(CrmActivity).filter_by(supplier_id=s.id).order_by(CrmActivity.activity_at.desc()).limit(1).all()
        )
        contacts = session.query(SupplierContact).filter_by(supplier_id=s.id).all()
        open_deals = sorted({o.contract_id for o in offers if o.contract_id})
        rows.append(
            {
                "id": s.id,
                "name": s.name,
                "products": s.manufacturer_relationship,
                "public_sector": s.federal_capability,
                "contacts": [{"id": c.id, "name": c.name, "phone": c.phone, "email": c.email} for c in contacts],
                "open_deals": open_deals,
                "quote_count": len(offers),
                "last_contact": activities[0].activity_at.isoformat() if activities and activities[0].activity_at else None,
                "next_followup": s.next_followup_at.isoformat() if s.next_followup_at else None,
            }
        )
    return {"suppliers": rows, "count": len(rows), "LIVE_API_REQUESTS": 0}


def build_contacts_view(session: Any, *, search: str | None = None) -> dict[str, Any]:
    from models import FinancierContact, GovernmentContact, SupplierContact

    contacts: list[dict[str, Any]] = []
    for sc in session.query(SupplierContact).limit(300).all():
        contacts.append(
            {
                "id": sc.id,
                "name": sc.name,
                "organization": sc.supplier.name if sc.supplier else None,
                "role": sc.title_role,
                "phone": sc.phone,
                "email": sc.email,
                "type": "SUPPLIER",
                "last_contact": None,
                "next_followup": None,
            }
        )
    for gc in session.query(GovernmentContact).limit(300).all():
        contacts.append(
            {
                "id": gc.id,
                "name": gc.name,
                "organization": gc.agency,
                "role": gc.title,
                "phone": gc.phone,
                "email": gc.email,
                "type": "GOVERNMENT",
                "last_contact": gc.last_contact_at.isoformat() if gc.last_contact_at else None,
                "next_followup": gc.next_followup_at.isoformat() if gc.next_followup_at else None,
            }
        )
    for fc in session.query(FinancierContact).limit(300).all():
        contacts.append(
            {
                "id": fc.id,
                "name": fc.name,
                "organization": fc.provider_id,
                "role": fc.title_role,
                "phone": fc.phone,
                "email": fc.email,
                "type": "FINANCIER",
                "last_contact": fc.last_contact_at.isoformat() if fc.last_contact_at else None,
                "next_followup": fc.next_followup_at.isoformat() if fc.next_followup_at else None,
            }
        )
    if search:
        s = search.lower()
        contacts = [c for c in contacts if s in str(c.get("name", "")).lower() or s in str(c.get("organization", "")).lower()]
    return {"contacts": contacts, "count": len(contacts), "LIVE_API_REQUESTS": 0}


def build_funding_work_view(session: Any) -> dict[str, Any]:
    from funding_persistence import load_persisted_funding
    from models import Contract, DealState, FinancingProvider, FinancingPursuit

    buckets = {
        "needs_research": [],
        "waiting_on_providers": [],
        "conditional": [],
        "viable": [],
        "blocked": [],
    }
    deals = session.query(DealState, Contract).join(Contract, DealState.contract_id == Contract.id).all()
    for deal, contract in deals:
        if deal.core_fit != "CORE_PRODUCT":
            continue
        persisted = load_persisted_funding(session, contract.id)
        status = (persisted or {}).get("pre_bid_status") or "NOT_RESEARCHED"
        entry = {
            "contract_id": contract.id,
            "title": contract.title,
            "agency": contract.agency,
            "pre_bid_status": status,
        }
        if status in {FUNDING_BLOCKED}:
            buckets["blocked"].append(entry)
        elif status == FUNDING_VIABLE:
            buckets["viable"].append(entry)
        elif status == FUNDING_WAITING_ON_PROVIDER:
            buckets["waiting_on_providers"].append(entry)
        elif status in {"VIABLE_CONDITIONAL_ON_AWARD", "VIABLE_CONDITIONAL"}:
            buckets["conditional"].append(entry)
        elif status in {FUNDING_NEEDS_CONTACT, "NOT_RESEARCHED", "INSUFFICIENT_EVIDENCE"}:
            buckets["needs_research"].append(entry)

    providers = session.query(FinancingProvider).order_by(FinancingProvider.name.asc()).limit(100).all()
    pursuits = session.query(FinancingPursuit).filter(FinancingPursuit.contract_id.isnot(None)).count()
    return {
        "buckets": buckets,
        "providers": [provider_profile_view(p) for p in providers],
        "active_pursuits": pursuits,
        "headline": "What funding work do I need to do?",
        "LIVE_API_REQUESTS": 0,
    }


def build_bids_view(session: Any) -> dict[str, Any]:
    from datetime import date, timedelta

    from deal_lifecycle import LIFECYCLE_BID_READY, LIFECYCLE_SUBMITTED
    from os_service import _lite_workspace

    buckets = {
        "BUILDING": [],
        "BID_READY": [],
        "DUE_SOON": [],
        "SUBMITTED": [],
        "LOST": [],
        "WON": [],
    }
    from models import Contract, DealState

    soon = today_local() + timedelta(days=7)
    deals = session.query(DealState, Contract).join(Contract, DealState.contract_id == Contract.id).all()
    for deal, contract in deals:
        if deal.core_fit != "CORE_PRODUCT":
            continue
        ws = _lite_workspace(session, contract, deal)
        lc = ws.get("lifecycle")
        econ = (deal.economics_json if deal else None) or {}
        row = {
            "contract_id": contract.id,
            "title": contract.title,
            "agency": contract.agency,
            "deadline": contract.due_date.isoformat() if contract.due_date else None,
            "bid_amount": float(deal.operator_bid_amount) if deal and deal.operator_bid_amount else None,
            "actual_profit": econ.get("actual_profit"),
            "deal_ready": (ws.get("deal_readiness") or {}).get("status"),
            "bid_ready": (ws.get("bid_readiness") or {}).get("status"),
            "submission_status": deal.decision if deal else None,
            "next_action": (ws.get("next_action") or {}).get("action"),
        }
        if lc == LIFECYCLE_SUBMITTED:
            buckets["SUBMITTED"].append(row)
        elif lc in {"LOST", "REJECTED"}:
            buckets["LOST"].append(row)
        elif lc in {"AWARDED", "PERFORMING", "PAID"}:
            buckets["WON"].append(row)
        elif (ws.get("bid_readiness") or {}).get("status") == LIFECYCLE_BID_READY:
            buckets["BID_READY"].append(row)
        else:
            buckets["BUILDING"].append(row)
        if contract.due_date and contract.due_date <= soon:
            buckets["DUE_SOON"].append(row)
    return {"buckets": buckets, "LIVE_API_REQUESTS": 0}


def build_awards_view(session: Any) -> dict[str, Any]:
    from models import AwardLifecycle, Contract, DealState

    rows = []
    lifecycles = session.query(AwardLifecycle).all()
    by_contract = {a.contract_id: a for a in lifecycles}
    deals = session.query(DealState, Contract).join(Contract, DealState.contract_id == Contract.id).all()
    for deal, contract in deals:
        if deal.decision not in {"SUBMITTED", "AWARDED", "WON"} and contract.id not in by_contract:
            continue
        row = get_or_create_award_lifecycle(session, contract.id)
        rows.append(
            {
                **award_lifecycle_to_dict(row),
                "title": contract.title,
                "agency": contract.agency,
            }
        )
    session.flush()
    return {"awards": rows, "count": len(rows), "LIVE_API_REQUESTS": 0}

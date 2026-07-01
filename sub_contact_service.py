"""Sub contact workflow — outreach tracking, quotes, wage compliance, email templates."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, joinedload

from models import Contract, ContractSub, Sub, SubContact
from proposal_service import calculate_bid_pricing
from settings_store import get_owner_settings
from sub_constants import (
    DEFAULT_SUB_CONTACT_STATUS,
    SUB_CONTACT_STATUSES,
    contact_status_to_legacy,
    legacy_status_to_contact,
)

HOURS_PER_MONTH = Decimal("173.33")
QUOTE_TARGET = 3
FOLLOWUP_HOURS = 48


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dec(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _sub_source(sub: Sub | None) -> str:
    if not sub:
        return "Manual Entry"
    pid = (sub.place_id or "").strip()
    if pid.startswith("manual_"):
        return "Manual Entry"
    return "Google Places"


def _flags_from_legacy_status(status: str) -> dict[str, Any]:
    contact_status = legacy_status_to_contact(status)
    called = contact_status != "Not Contacted"
    reached = contact_status in ("Contacted", "Quote Received", "Selected")
    voicemail = contact_status == "Voicemail Left"
    quote_received = contact_status in ("Quote Received", "Selected")
    return {
        "status": contact_status,
        "called": called,
        "reached": reached,
        "voicemail_left": voicemail,
        "quote_received": quote_received,
        "is_selected": contact_status == "Selected",
    }


def _apply_status_automation(row: SubContact) -> None:
    """Derive status from toggles when not explicitly set."""
    if row.is_selected:
        row.status = "Selected"
        return
    if row.quote_received:
        row.status = "Quote Received"
        return
    if row.voicemail_left:
        row.status = "Voicemail Left"
        return
    if row.called:
        row.status = "Contacted"
        return
    if row.status not in SUB_CONTACT_STATUSES:
        row.status = DEFAULT_SUB_CONTACT_STATUS


def estimate_employees(contract: Contract) -> int:
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    pws = analysis.get("pws_extraction") if isinstance(analysis.get("pws_extraction"), dict) else {}
    for key in ("estimated_employees", "employee_count", "fte_count"):
        val = pws.get(key) or analysis.get(key)
        if val is not None:
            try:
                return max(1, int(val))
            except (TypeError, ValueError):
                pass
    sqft = contract.square_footage
    if sqft:
        return max(1, round(sqft / 15000))
    return 2


def wage_requirements_for_contract(contract: Contract) -> dict[str, Any]:
    hourly = _dec(contract.wage_determination_rate)
    employees = estimate_employees(contract)
    min_monthly = None
    if hourly is not None:
        min_monthly = round(float(HOURS_PER_MONTH) * hourly * employees * 1.15, 2)
    return {
        "wage_determination_number": contract.wage_determination_number,
        "hourly_rate": hourly,
        "estimated_employees": employees,
        "minimum_monthly_quote": min_monthly,
        "warning_text": (
            f"Subs must pay employees at least ${hourly:.2f} per hour per applicable wage determination. "
            "Ensure all quotes reflect this requirement."
            if hourly is not None
            else "Wage determination rate not yet extracted — confirm SCA rates before accepting quotes."
        ),
    }


def wage_compliance_status(monthly_quote: float | None, min_monthly: float | None) -> dict[str, str]:
    if monthly_quote is None or min_monthly is None or min_monthly <= 0:
        return {"level": "neutral", "message": "Enter quote to check wage compliance."}
    ratio = monthly_quote / min_monthly
    if ratio >= 1.0:
        return {"level": "green", "message": "Quote covers minimum wage requirements."}
    if ratio >= 0.9:
        return {"level": "yellow", "message": "Quote is within 10% below minimum wage floor."}
    return {"level": "red", "message": "Quote is significantly below minimum wage requirements."}


def _extract_pws_scope_snippet(contract: Contract, max_chars: int = 2500) -> str:
    text = contract.attachment_text or contract.description or ""
    if not text:
        analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
        pws = analysis.get("pws_extraction") if isinstance(analysis.get("pws_extraction"), dict) else {}
        parts = [
            pws.get("scope_summary"),
            pws.get("performance_work_statement"),
            analysis.get("plain_english_summary"),
        ]
        joined = "\n\n".join(str(p).strip() for p in parts if p)
        return joined[:max_chars] if joined else "See attached solicitation for full performance requirements."

    patterns = [
        r"(?is)(performance\s+work\s+statement.{0,4000})",
        r"(?is)(statement\s+of\s+work.{0,4000})",
        r"(?is)(scope\s+of\s+(?:work|services).{0,3000})",
    ]
    for pat in patterns:
        match = re.search(pat, text)
        if match:
            snippet = re.sub(r"\s+", " ", match.group(1)).strip()
            if len(snippet) > 200:
                return snippet[:max_chars]
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:max_chars]


def _service_type_label(contract: Contract) -> str:
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    return (
        analysis.get("sub_type_needed")
        or contract.building_type
        or "janitorial"
    )


def _quote_deadline(contract: Contract) -> str:
    if contract.due_date:
        deadline = contract.due_date - timedelta(days=2)
        return deadline.strftime("%B %d, %Y")
    return "two days before our bid deadline (TBD)"


def generate_scope_email(session: Session, contact_id: int) -> dict[str, str]:
    row = (
        session.query(SubContact)
        .options(joinedload(SubContact.contract))
        .filter_by(id=contact_id)
        .first()
    )
    if not row:
        raise ValueError("Sub contact not found")
    contract = row.contract
    if not contract:
        raise ValueError("Contract not found")

    owner = get_owner_settings()
    owner_name = owner.get("owner_name") or "Mark Graham II"
    owner_email = "markg@northernreinvestments.com"
    owner_phone = owner.get("business_phone") or ""
    company = owner.get("legal_business_name") or "Northern RE Investments LLC"

    scope = _extract_pws_scope_snippet(contract)
    freq = contract.cleaning_frequency_per_week
    freq_line = f"{freq} time(s) per week" if freq else "per the solicitation schedule"
    sqft = f"{contract.square_footage:,} sq ft" if contract.square_footage else "see solicitation"
    building = contract.building_type or "federal facility"
    location = contract.location or f"{row.city or ''}, {row.state or ''}".strip(", ")
    special = contract.special_requirements or []
    special_lines = "\n".join(f"- {s}" for s in special[:8]) if special else "- Standard commercial service requirements per PWS"

    subject = f"Quote request — {contract.title or 'federal contract'} ({location})"
    body = f"""Subject: {subject}

Hello,

My name is {owner_name} with {company}. We are preparing a bid on a federal contract and would like to request a quote from your company for subcontract performance.

WHAT THE GOVERNMENT NEEDS DONE
{scope}

LOCATION & BUILDING
{location}
Building type: {building}
Approximate size: {sqft}

SERVICE FREQUENCY
{freq_line}

SPECIAL REQUIREMENTS
{special_lines}

QUOTE DEADLINE
Please provide your quote by {_quote_deadline(contract)}.

REFERENCES
Please include three client references with name, phone or email, service performed, and dates of service.

PAYMENT TERMS
Please confirm you can accept payment terms of Net 45 from invoice date, contingent on government payment to the prime contractor.

CONTACT
{owner_name}
{owner_email}
{owner_phone}

Thank you for your time — we look forward to hearing from you.

{owner_name}
{company}
"""
    return {"subject": subject, "body": body.strip(), "to": row.email or ""}


def generate_followup_email(session: Session, contact_id: int) -> dict[str, str]:
    row = (
        session.query(SubContact)
        .options(joinedload(SubContact.contract))
        .filter_by(id=contact_id)
        .first()
    )
    if not row:
        raise ValueError("Sub contact not found")
    contract = row.contract
    owner = get_owner_settings()
    owner_name = owner.get("owner_name") or "Mark Graham II"
    owner_phone = owner.get("business_phone") or ""
    company = owner.get("legal_business_name") or "Northern RE Investments LLC"
    location = contract.location if contract else f"{row.city or ''}, {row.state or ''}".strip(", ")
    service = _service_type_label(contract) if contract else "service"

    subject = f"Following up — quote request for {location}"
    body = f"""Subject: {subject}

Hello,

I'm following up on my earlier message regarding a quote for {service} services at a federal facility in {location}.

If this is something your company handles, I'd appreciate the opportunity to connect. Please reply with your monthly quote or call me at {owner_phone}.

Thank you,
{owner_name}
{company}
"""
    return {"subject": subject, "body": body.strip(), "to": row.email or ""}


def generate_voicemail_script(session: Session, contact_id: int) -> dict[str, str]:
    row = (
        session.query(SubContact)
        .options(joinedload(SubContact.contract))
        .filter_by(id=contact_id)
        .first()
    )
    if not row:
        raise ValueError("Sub contact not found")
    contract = row.contract
    owner = get_owner_settings()
    owner_phone = owner.get("business_phone") or ""
    company = owner.get("legal_business_name") or "Northern RE Investments LLC"
    service = _service_type_label(contract) if contract else "service"
    location = contract.location if contract else f"{row.city or ''}, {row.state or ''}".strip(", ")

    script = (
        f"Hi, this is Mark Graham calling from {company}. I'm reaching out to get a quote for "
        f"{service} services at a federal facility in {location}. If this is something your company "
        f"handles I'd love to connect. Please give me a call back at {owner_phone}. Thank you."
    )
    return {"script": script}


def sub_contact_to_dict(row: SubContact, contract: Contract | None = None) -> dict[str, Any]:
    contract = contract or row.contract
    wage = wage_requirements_for_contract(contract) if contract else {}
    monthly = _dec(row.quote_amount)
    compliance = wage_compliance_status(monthly, wage.get("minimum_monthly_quote"))
    followup = needs_followup(row)
    return {
        "id": row.id,
        "contract_id": row.contract_id,
        "sub_id": row.sub_id,
        "contract_sub_id": row.contract_sub_id,
        "company_name": row.company_name,
        "phone": row.phone,
        "email": row.email,
        "website": row.website,
        "address": row.address,
        "city": row.city,
        "state": row.state,
        "rating": _dec(row.rating),
        "source": row.source,
        "distance_miles": _dec(row.distance_miles),
        "called": row.called,
        "call_date": row.call_date.isoformat() if row.call_date else None,
        "reached": row.reached,
        "voicemail_left": row.voicemail_left,
        "email_sent": row.email_sent,
        "email_sent_date": row.email_sent_date.isoformat() if row.email_sent_date else None,
        "quote_received": row.quote_received,
        "quote_amount": monthly,
        "quote_date": row.quote_date.isoformat() if row.quote_date else None,
        "annual_quote": round(monthly * 12, 2) if monthly is not None else None,
        "payment_terms_confirmed": row.payment_terms_confirmed,
        "insurance_verified": row.insurance_verified,
        "insurance_expiration_date": row.insurance_expiration_date.isoformat()
        if row.insurance_expiration_date
        else None,
        "insurance_coverage_amount": _dec(row.insurance_coverage_amount),
        "references_requested": row.references_requested,
        "references_received": row.references_received,
        "references": row.references_json or [],
        "is_selected": row.is_selected,
        "status": row.status,
        "notes": row.notes,
        "claude_score": row.claude_score,
        "claude_reason": row.claude_reason,
        "wage_compliance": compliance,
        "needs_followup": followup,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def needs_followup(row: SubContact) -> bool:
    if row.quote_received or row.status in ("Quote Received", "Selected", "Not Selected"):
        return False
    if row.status not in ("Contacted", "Voicemail Left"):
        return False
    anchor = row.call_date or row.email_sent_date or row.updated_at
    if not anchor:
        return False
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return _now() - anchor >= timedelta(hours=FOLLOWUP_HOURS)


def contact_progress(contacts: list[SubContact]) -> dict[str, Any]:
    total = len(contacts)
    called = sum(1 for c in contacts if c.called)
    reached = sum(1 for c in contacts if c.reached)
    quoted = sum(1 for c in contacts if c.quote_received)
    return {
        "total": total,
        "called": called,
        "reached": reached,
        "quoted": quoted,
        "quote_target": QUOTE_TARGET,
        "quote_progress_pct": min(100, round(quoted / QUOTE_TARGET * 100)) if QUOTE_TARGET else 0,
    }


def pre_bid_checklist(contract: Contract, contacts: list[SubContact], session: Session) -> dict[str, Any]:
    from pricing import get_full_pricing_intel

    quoted = [c for c in contacts if c.quote_received and c.quote_amount]
    selected = next((c for c in contacts if c.is_selected), None)
    pricing = get_full_pricing_intel(contract, session)
    regional = pricing.get("regional_benchmark") or {}
    incumbent_researched = bool(regional.get("average_annual_award") or regional.get("likely_incumbent"))

    items = [
        {
            "key": "three_quotes",
            "label": "Minimum 3 quotes received",
            "complete": len(quoted) >= 3,
            "detail": f"{len(quoted)} of 3",
        },
        {
            "key": "incumbent",
            "label": "Incumbent price researched",
            "complete": incumbent_researched,
            "detail": "USAspending" if incumbent_researched else "Run pricing intel",
            "link": "pricing",
        },
        {
            "key": "wage_rates",
            "label": "Wage determination rates confirmed",
            "complete": contract.wage_determination_rate is not None,
            "detail": contract.wage_determination_number or "Not extracted",
        },
        {
            "key": "sub_selected",
            "label": "Sub selected",
            "complete": selected is not None,
            "detail": selected.company_name if selected else "None",
        },
        {
            "key": "payment_terms",
            "label": "Payment terms confirmed with selected sub",
            "complete": bool(selected and selected.payment_terms_confirmed),
            "detail": "Net 45 confirmed" if selected and selected.payment_terms_confirmed else "Pending",
        },
        {
            "key": "references",
            "label": "References received from selected sub",
            "complete": bool(selected and selected.references_received),
            "detail": "Received" if selected and selected.references_received else "Pending",
        },
    ]
    all_complete = all(i["complete"] for i in items)
    bypassed = contract.sub_checklist_bypassed_at is not None
    return {
        "items": items,
        "all_complete": all_complete,
        "bypassed": bypassed,
        "bypassed_at": contract.sub_checklist_bypassed_at.isoformat()
        if contract.sub_checklist_bypassed_at
        else None,
        "can_proceed": all_complete or bypassed,
        "block_message": "Complete the checklist above before generating your proposal.",
    }


def quote_comparison(session: Session, contract: Contract, contacts: list[SubContact]) -> list[dict[str, Any]]:
    from pricing import get_full_pricing_intel

    pricing = get_full_pricing_intel(contract, session)
    regional = pricing.get("regional_benchmark") or {}
    hist_avg = regional.get("average_annual_award")
    wage = wage_requirements_for_contract(contract)
    rows = []
    for c in contacts:
        if not c.quote_received or c.quote_amount is None:
            continue
        monthly = float(c.quote_amount)
        annual = monthly * 12
        bid18 = calculate_bid_pricing(monthly, 18.0)["base_year_bid"]
        bid20 = calculate_bid_pricing(monthly, 20.0)["base_year_bid"]
        compliance = wage_compliance_status(monthly, wage.get("minimum_monthly_quote"))
        competitiveness = {"level": "neutral", "message": ""}
        if hist_avg:
            if bid20 <= hist_avg * 1.05:
                competitiveness = {"level": "green", "message": "Bid at 20% margin is at or below historical average."}
            elif bid20 <= hist_avg * 1.15:
                competitiveness = {"level": "yellow", "message": "Bid at 20% margin is slightly above historical average."}
            else:
                competitiveness = {"level": "red", "message": "Bid at 20% margin is well above historical average."}
        rows.append(
            {
                "id": c.id,
                "contract_sub_id": c.contract_sub_id,
                "company_name": c.company_name,
                "monthly_quote": monthly,
                "annual_quote": annual,
                "wage_compliance": compliance,
                "bid_at_18_margin": bid18,
                "bid_at_20_margin": bid20,
                "historical_avg_annual": hist_avg,
                "competitiveness": competitiveness,
                "is_selected": c.is_selected,
            }
        )
    return sorted(rows, key=lambda r: r["monthly_quote"])


def sync_sub_contact_to_contract_sub(session: Session, contact: SubContact) -> None:
    if not contact.contract_sub_id:
        return
    link = session.get(ContractSub, contact.contract_sub_id)
    if not link:
        return
    link.status = contact_status_to_legacy(contact.status)
    link.contact_notes = contact.notes
    link.quote_amount = contact.quote_amount
    if contact.quote_date:
        link.quote_date = contact.quote_date.date() if isinstance(contact.quote_date, datetime) else contact.quote_date
    else:
        link.quote_date = None
    link.date_status_updated = _now()
    contract = session.get(Contract, contact.contract_id)
    if contract:
        if contact.is_selected and contact.quote_amount is not None:
            contract.selected_sub_quote = contact.quote_amount
        elif not any(
            c.is_selected for c in session.query(SubContact).filter_by(contract_id=contract.id).all()
        ):
            contract.selected_sub_quote = None


def sync_sub_to_master(session: Session, contact: SubContact) -> None:
    if not contact.sub_id:
        return
    sub = session.get(Sub, contact.sub_id)
    if not sub:
        return
    if contact.phone:
        sub.phone = contact.phone
    if contact.email:
        sub.business_email = contact.email
    if contact.address:
        sub.address = contact.address
    if contact.city:
        sub.city = contact.city
    if contact.state:
        sub.state = contact.state
    sub.date_last_updated = _now()


def create_sub_contact_from_link(
    session: Session,
    contract: Contract,
    link: ContractSub,
    sub: Sub | None,
) -> SubContact:
    existing = session.query(SubContact).filter_by(contract_sub_id=link.id).first()
    if existing:
        return existing
    if sub is None:
        sub = session.get(Sub, link.sub_id)
    flags = _flags_from_legacy_status(link.status)
    row = SubContact(
        contract_id=contract.id,
        sub_id=link.sub_id,
        contract_sub_id=link.id,
        company_name=sub.business_name if sub else "Unknown",
        phone=sub.phone if sub else None,
        email=sub.business_email if sub else None,
        website=sub.website if sub else None,
        address=sub.address if sub else None,
        city=sub.city if sub else None,
        state=sub.state if sub else None,
        rating=sub.rating if sub else None,
        source=_sub_source(sub),
        distance_miles=link.distance_miles,
        called=flags["called"],
        reached=flags["reached"],
        voicemail_left=flags["voicemail_left"],
        quote_received=flags["quote_received"] or link.quote_amount is not None,
        quote_amount=link.quote_amount,
        quote_date=datetime.combine(link.quote_date, datetime.min.time(), tzinfo=timezone.utc)
        if link.quote_date
        else None,
        is_selected=flags["is_selected"],
        status=flags["status"],
        notes=link.contact_notes,
        claude_score=link.claude_score,
        claude_reason=link.claude_reason,
    )
    session.add(row)
    session.flush()
    return row


def migrate_contract_subs_to_sub_contacts() -> int:
    from database import SessionLocal

    session = SessionLocal()
    try:
        existing = session.query(SubContact).count()
        if existing > 0:
            return 0
        links = (
            session.query(ContractSub)
            .options(joinedload(ContractSub.sub), joinedload(ContractSub.contract))
            .all()
        )
        count = 0
        for link in links:
            contract = link.contract
            if not contract:
                continue
            create_sub_contact_from_link(session, contract, link, link.sub)
            count += 1
        session.commit()
        return count
    finally:
        session.close()


def list_sub_contacts_for_contract(session: Session, notice_id: str) -> dict[str, Any]:
    from sub_serializers import contract_sub_summary
    from usaspending_client import extract_work_location

    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")

    links = (
        session.query(ContractSub)
        .options(joinedload(ContractSub.sub))
        .filter_by(contract_id=contract.id)
        .all()
    )
    for link in links:
        create_sub_contact_from_link(session, contract, link, link.sub)
    session.flush()

    contacts = (
        session.query(SubContact)
        .filter_by(contract_id=contract.id)
        .order_by(SubContact.claude_score.desc().nulls_last(), SubContact.distance_miles.asc().nulls_last())
        .all()
    )
    work = extract_work_location(
        contract.location,
        contract.sam_raw if isinstance(contract.sam_raw, dict) else None,
    )
    summary = contract_sub_summary(contract, session)
    summary["city"] = work.get("city") or work.get("label")
    wage = wage_requirements_for_contract(contract)
    progress = contact_progress(contacts)
    checklist = pre_bid_checklist(contract, contacts, session)
    payload_contacts = [sub_contact_to_dict(c, contract) for c in contacts]
    return {
        "notice_id": notice_id,
        "contract_title": contract.title,
        "agency": contract.agency,
        "summary": summary,
        "selected_sub_quote": _dec(contract.selected_sub_quote),
        "wage_requirements": wage,
        "progress": progress,
        "pre_bid_checklist": checklist,
        "contacts": payload_contacts,
        "subs": payload_contacts,
        "quote_comparison": quote_comparison(session, contract, contacts),
    }


def get_sub_contact_detail(session: Session, contact_id: int) -> dict[str, Any]:
    row = (
        session.query(SubContact)
        .options(joinedload(SubContact.contract), joinedload(SubContact.sub))
        .filter_by(id=contact_id)
        .first()
    )
    if not row:
        raise ValueError("Sub contact not found")
    contract = row.contract
    payload = sub_contact_to_dict(row, contract)
    if row.sub:
        payload["google_maps_url"] = row.sub.google_maps_url
        payload["owner_name"] = row.sub.owner_name
        payload["owner_title"] = row.sub.owner_title
        payload["license_number"] = row.sub.license_number
        payload["insurance_carrier"] = row.sub.insurance_carrier
    if row.contract_sub_id and row.is_selected:
        from agreement_service import agreement_for_link, agreement_to_dict, build_agreement_config

        agreement_row = agreement_for_link(session, row.contract_sub_id)
        agreement_info = agreement_to_dict(agreement_row, row.contract_sub)
        if not agreement_info.get("has_agreement"):
            try:
                preview = build_agreement_config(session, row.contract_sub_id)
                agreement_info["missing_fields"] = preview.get("missing_fields") or []
            except ValueError:
                pass
        payload["agreement"] = agreement_info
        payload["agreement_signature_status"] = (
            row.contract_sub.agreement_signature_status if row.contract_sub else "Agreement Not Generated"
        )
    return payload


def update_sub_contact(session: Session, contact_id: int, payload: dict[str, Any]) -> SubContact:
    row = session.get(SubContact, contact_id)
    if not row:
        raise ValueError("Sub contact not found")

    bool_fields = (
        "called",
        "reached",
        "voicemail_left",
        "email_sent",
        "quote_received",
        "payment_terms_confirmed",
        "insurance_verified",
        "references_requested",
        "references_received",
        "is_selected",
    )
    for key in bool_fields:
        if key in payload:
            setattr(row, key, bool(payload[key]))

    str_fields = (
        "company_name",
        "phone",
        "email",
        "website",
        "address",
        "city",
        "state",
        "source",
        "notes",
        "status",
    )
    for key in str_fields:
        if key in payload:
            setattr(row, key, payload[key] or None)

    if "rating" in payload:
        val = payload["rating"]
        row.rating = Decimal(str(val)) if val not in (None, "") else None
    if "quote_amount" in payload:
        val = payload["quote_amount"]
        row.quote_amount = Decimal(str(val)) if val not in (None, "") else None
        if row.quote_amount is not None:
            row.quote_received = True
    if "insurance_coverage_amount" in payload:
        val = payload["insurance_coverage_amount"]
        row.insurance_coverage_amount = Decimal(str(val)) if val not in (None, "") else None
    if "references" in payload:
        row.references_json = payload["references"] or []

    if "call_date" in payload:
        raw = payload["call_date"]
        row.call_date = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    if "email_sent_date" in payload:
        raw = payload["email_sent_date"]
        row.email_sent_date = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    if "quote_date" in payload:
        raw = payload["quote_date"]
        if not raw:
            row.quote_date = None
        elif "T" in raw:
            row.quote_date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            row.quote_date = datetime.combine(date.fromisoformat(raw), datetime.min.time(), tzinfo=timezone.utc)
    if "insurance_expiration_date" in payload:
        raw = payload["insurance_expiration_date"]
        row.insurance_expiration_date = date.fromisoformat(raw) if raw else None

    now = _now()
    if payload.get("called") is True and not row.call_date:
        row.call_date = now
    if payload.get("email_sent") is True and not row.email_sent_date:
        row.email_sent_date = now
    if payload.get("quote_received") is True and not row.quote_date:
        row.quote_date = now

    if "status" not in payload:
        _apply_status_automation(row)
    elif row.status not in SUB_CONTACT_STATUSES:
        raise ValueError(f"Invalid status. Choose one of: {', '.join(SUB_CONTACT_STATUSES)}")

    if "called" in payload and payload["called"]:
        row.status = "Contacted"
    if "voicemail_left" in payload and payload["voicemail_left"]:
        row.status = "Voicemail Left"
    if "quote_received" in payload and payload["quote_received"]:
        row.status = "Quote Received"

    if payload.get("is_selected") is True or payload.get("select") is True:
        select_sub_contact(session, row.id)
        session.refresh(row)
        return row

    sync_sub_contact_to_contract_sub(session, row)
    sync_sub_to_master(session, row)
    session.commit()
    session.refresh(row)
    return row


def select_sub_contact(session: Session, contact_id: int) -> SubContact:
    row = session.get(SubContact, contact_id)
    if not row:
        raise ValueError("Sub contact not found")
    others = session.query(SubContact).filter(
        SubContact.contract_id == row.contract_id,
        SubContact.id != row.id,
    ).all()
    for other in others:
        if other.is_selected:
            other.is_selected = False
            if other.quote_received:
                other.status = "Not Selected"
            elif other.called:
                other.status = "Contacted"
            else:
                other.status = "Not Contacted"
        elif other.status == "Selected":
            other.status = "Not Selected" if other.quote_received else "Contacted"
        sync_sub_contact_to_contract_sub(session, other)

    row.is_selected = True
    row.status = "Selected"
    if row.quote_amount is not None:
        row.quote_received = True
    contract = session.get(Contract, row.contract_id)
    if contract and row.quote_amount is not None:
        contract.selected_sub_quote = row.quote_amount

    sync_sub_contact_to_contract_sub(session, row)
    sync_sub_to_master(session, row)
    session.commit()
    session.refresh(row)
    return row


def deselect_sub_contact(session: Session, contact_id: int) -> SubContact:
    row = session.get(SubContact, contact_id)
    if not row:
        raise ValueError("Sub contact not found")
    row.is_selected = False
    row.status = "Quote Received" if row.quote_received else ("Contacted" if row.called else "Not Contacted")
    contract = session.get(Contract, row.contract_id)
    if contract:
        contract.selected_sub_quote = None
    sync_sub_contact_to_contract_sub(session, row)
    session.commit()
    session.refresh(row)
    return row


def mark_email_sent(session: Session, contact_id: int, *, sent: bool = True) -> SubContact:
    row = session.get(SubContact, contact_id)
    if not row:
        raise ValueError("Sub contact not found")
    if sent:
        row.email_sent = True
        row.email_sent_date = _now()
    else:
        row.email_sent = False
        row.email_sent_date = None
    session.commit()
    session.refresh(row)
    return row


def bypass_pre_bid_checklist(session: Session, notice_id: str) -> Contract:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")
    contract.sub_checklist_bypassed_at = _now()
    session.commit()
    session.refresh(contract)
    return contract


def references_for_selected_sub(session: Session, contract_id: int) -> list[dict[str, Any]]:
    row = (
        session.query(SubContact)
        .filter_by(contract_id=contract_id, is_selected=True)
        .first()
    )
    if not row or not row.references_json:
        return []
    return list(row.references_json)

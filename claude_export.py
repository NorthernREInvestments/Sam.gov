"""Export the full GovTracker database for upload to Claude Projects."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from models import AppSetting, Contract, ContractSub, Proposal, Sub, SubcontractAgreement
from settings_store import get_all_settings
from sub_serializers import contract_sub_to_dict, contract_sub_summary, sub_to_dict


CLAUDE_INSTRUCTIONS = """This file is a full snapshot export from GovTracker — a federal contract pipeline tool.

How to use it:
- contracts[]: every opportunity (SAM.gov data, Claude analysis, PWS fields, pricing intel, workflow status)
- subs[]: subcontractor network (Google Places + manual entries)
- contract_subs[]: sub shortlists, quotes, contact notes, and agreement status per contract
- proposals[]: generated proposal drafts and sections
- subcontract_agreements[]: subcontract agreement HTML (PDFs are omitted; has_pdf indicates a stored PDF exists)
- attachment_text on each contract: plain text extracted from solicitation PDFs when available

Key fields:
- notice_id: unique contract identifier
- analysis.plain_english_summary: Claude screening summary
- analysis.proposal_requirements: Section L/M/PWS extraction when present
- pricing_intel / pricing_intelligence: regional benchmarks and bid guidance
- pws: square footage, cleaning frequency, wage determination, etc.

This is a point-in-time export — re-export from GovTracker Settings when data changes."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _contract_record(row: Contract, session: Session, *, attachment_text: str | None) -> dict[str, Any]:
    from naics_labels import naics_display, naics_label, tier_label
    from proposal_defaults import resolve_contract_margin
    from pws_fields import pws_snapshot
    from sam_enrich import is_scrape_complete
    from usaspending_client import extract_work_location
    from workflow_status import compute_card_pipeline, compute_workflow_status

    today = date.today()
    days_left = (row.due_date - today).days if row.due_date else None
    analysis = row.analysis if isinstance(row.analysis, dict) else {}
    sam_raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}

    work = extract_work_location(row.location, sam_raw)
    sub_summary = contract_sub_summary(row, session)
    sub_summary["city"] = work.get("city") or work.get("label")
    workflow = compute_workflow_status(row, session)
    pipeline = compute_card_pipeline(row, session)

    attachments = sam_raw.get("opportunityAttachments")
    piee_attachments = sam_raw.get("pieeAttachments") if isinstance(sam_raw.get("pieeAttachments"), list) else []
    if isinstance(attachments, list) and attachments:
        doc_access = sam_raw.get("documentAccess") or {}
        external_links = sam_raw.get("opportunityLinks") or []
        sam_attachments = attachments + piee_attachments
    elif piee_attachments:
        doc_access = sam_raw.get("documentAccess") or {}
        external_links = sam_raw.get("opportunityLinks") or []
        sam_attachments = piee_attachments
    else:
        sam_attachments = analysis.get("sam_attachments") or []
        doc_access = sam_raw.get("documentAccess") or analysis.get("document_access") or {}
        external_links = sam_raw.get("opportunityLinks") or analysis.get("external_links") or []

    selected_quote = float(row.selected_sub_quote) if row.selected_sub_quote is not None else None
    effective_margin = resolve_contract_margin(row)
    estimated_annual_bid = None
    if selected_quote and selected_quote > 0:
        estimated_annual_bid = round(selected_quote / (1 - effective_margin / 100.0), 2)

    clearance_flag = analysis.get("security_clearance_required")
    if clearance_flag is True:
        security_clearance_required = True
    elif clearance_flag is False:
        security_clearance_required = False
    else:
        from comparable_scope import detect_clearance_level

        security_clearance_required = bool(detect_clearance_level(row.description or ""))

    return {
        "notice_id": row.notice_id,
        "title": row.title,
        "agency": row.agency,
        "location": row.location,
        "naics_code": row.naics_code,
        "naics_label": naics_label(row.naics_code),
        "naics_display": naics_display(row.naics_code),
        "tier": row.tier,
        "tier_label": tier_label(row.tier),
        "set_aside": row.set_aside,
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "days_until_due": days_left,
        "link": row.link,
        "estimated_value": row.estimated_value,
        "description": row.description,
        "status": row.status,
        "analysis": analysis,
        "pursue": analysis.get("pursue"),
        "score": analysis.get("score"),
        "text_score": analysis.get("text_score") or analysis.get("score"),
        "screening_stage": analysis.get("screening_stage")
        or ("full" if analysis.get("plain_english_summary") else None),
        "skip_reason": analysis.get("skip_reason"),
        "reason": analysis.get("reason"),
        "plain_english_summary": analysis.get("plain_english_summary") or analysis.get("executive_summary"),
        "executive_summary": analysis.get("executive_summary"),
        "pricing_intelligence": analysis.get("pricing_intelligence"),
        "pricing_intel": row.pricing_intel,
        "pws": pws_snapshot(row),
        "square_footage": row.square_footage,
        "building_type": row.building_type,
        "cleaning_frequency_per_week": float(row.cleaning_frequency_per_week)
        if row.cleaning_frequency_per_week is not None
        else None,
        "awarded_amount": float(row.awarded_amount) if row.awarded_amount is not None else None,
        "sub_type_needed": analysis.get("sub_type_needed"),
        "sub_summary": sub_summary,
        "selected_sub_quote": selected_quote,
        "margin_percentage": float(row.margin_percentage) if row.margin_percentage is not None else None,
        "effective_margin_pct": effective_margin,
        "estimated_annual_bid": estimated_annual_bid,
        "sub_search_status": row.sub_search_status,
        "sub_search_radius_miles": row.sub_search_radius_miles,
        "red_flags": analysis.get("red_flags") or [],
        "security_clearance_required": security_clearance_required,
        "document_access": doc_access,
        "external_links": external_links,
        "sam_attachments": sam_attachments,
        "scrape_complete": is_scrape_complete(sam_raw),
        "workflow": workflow,
        "pipeline": pipeline,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_updated_at": row.last_updated_at.isoformat() if row.last_updated_at else None,
        "sam_raw": sam_raw,
        "special_requirements": row.special_requirements,
        "wage_determination_number": row.wage_determination_number,
        "wage_determination_rate": float(row.wage_determination_rate)
        if row.wage_determination_rate is not None
        else None,
        "price_per_sqft_per_year": float(row.price_per_sqft_per_year)
        if row.price_per_sqft_per_year is not None
        else None,
        "price_per_sqft_per_visit": float(row.price_per_sqft_per_visit)
        if row.price_per_sqft_per_visit is not None
        else None,
        "pricing_region": row.pricing_region,
        "attachment_text": attachment_text,
        "attachment_text_chars": len(attachment_text) if attachment_text else 0,
    }


def _agreement_record(row: SubcontractAgreement) -> dict[str, Any]:
    return {
        "id": row.id,
        "contract_id": row.contract_id,
        "sub_id": row.sub_id,
        "contract_sub_id": row.contract_sub_id,
        "version": row.version,
        "agreement_html": row.agreement_html,
        "config_json": row.config_json,
        "has_pdf": bool(row.pdf_bytes),
        "generated_at": row.generated_at.isoformat() if row.generated_at else None,
        "date_updated": row.date_updated.isoformat() if row.date_updated else None,
    }


def _attachment_text_for_contract(row: Contract) -> tuple[str | None, str | None]:
    """Return (text, error_note). Downloads PDFs from stored URLs only — no SAM or Claude API."""
    try:
        from claude_client import contract_attachment_text

        text = contract_attachment_text(row, max_pdfs=12).strip()
        if not text:
            return None, "no_extractable_pdf_text"
        if len(text) > 250_000:
            return (
                text[:250_000]
                + f"\n\n[Truncated at 250,000 characters — {len(text):,} total in attachments.]",
                "truncated",
            )
        return text, None
    except Exception as exc:
        return None, f"attachment_text_error: {exc}"


def build_claude_export(session: Session, *, include_attachment_text: bool = True) -> dict[str, Any]:
    """Assemble a Claude-friendly JSON document from the full database."""
    from api_budget import get_usage_snapshot
    from pricing import get_pricing_dashboard
    from proposal_service import proposal_to_dict

    contracts = session.query(Contract).order_by(Contract.id).all()
    subs = session.query(Sub).order_by(Sub.id).all()
    links = session.query(ContractSub).order_by(ContractSub.id).all()
    proposals = session.query(Proposal).order_by(Proposal.id).all()
    agreements = session.query(SubcontractAgreement).order_by(SubcontractAgreement.id).all()
    settings_rows = session.query(AppSetting).order_by(AppSetting.key).all()

    notice_by_id = {c.id: c.notice_id for c in contracts}

    contract_records: list[dict[str, Any]] = []
    attachment_notes: list[dict[str, Any]] = []
    for row in contracts:
        att_text = None
        att_note = None
        if include_attachment_text:
            att_text, att_note = _attachment_text_for_contract(row)
            if att_note:
                attachment_notes.append({"notice_id": row.notice_id, "note": att_note})
        contract_records.append(_contract_record(row, session, attachment_text=att_text))

    sub_stats = _sub_outreach_stats(session)

    return {
        "export_format": "govtracker-claude-export",
        "export_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "instructions_for_claude": CLAUDE_INSTRUCTIONS,
        "settings": get_all_settings(),
        "app_settings": {row.key: row.value for row in settings_rows},
        "api_budget_today": get_usage_snapshot(),
        "pricing_dashboard": get_pricing_dashboard(session),
        "counts": {
            "contracts": len(contracts),
            "subs": len(subs),
            "contract_subs": len(links),
            "proposals": len(proposals),
            "subcontract_agreements": len(agreements),
        },
        "attachment_export_notes": attachment_notes,
        "contracts": contract_records,
        "subs": [sub_to_dict(s, stats=sub_stats.get(s.id)) for s in subs],
        "contract_subs": [
            {
                **contract_sub_to_dict(link),
                "notice_id": notice_by_id.get(link.contract_id),
            }
            for link in links
        ],
        "proposals": [proposal_to_dict(p) for p in proposals],
        "subcontract_agreements": [_agreement_record(a) for a in agreements],
    }


def _sub_outreach_stats(session: Session) -> dict[int, dict[str, Any]]:
    from sqlalchemy import func

    rows = (
        session.query(
            ContractSub.sub_id,
            func.count(ContractSub.id),
            func.max(ContractSub.date_status_updated),
        )
        .group_by(ContractSub.sub_id)
        .all()
    )
    stats: dict[int, dict[str, Any]] = {}
    for sub_id, count, last_touch in rows:
        stats[sub_id] = {
            "contracts_linked": int(count),
            "last_contact_at": last_touch.isoformat() if last_touch else None,
        }
    return stats


def export_claude_json_bytes(session: Session, *, include_attachment_text: bool = True) -> tuple[bytes, str]:
    """Return UTF-8 JSON bytes and a suggested filename."""
    payload = build_claude_export(session, include_attachment_text=include_attachment_text)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"govtracker-claude-export-{stamp}.json"
    body = json.dumps(payload, indent=2, default=_json_default, ensure_ascii=False)
    return body.encode("utf-8"), filename

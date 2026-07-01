"""Proposal writer — bid math, config assembly, generation orchestration."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, joinedload

from models import Contract, ContractSub, Proposal, Sub
from proposal_defaults import DEFAULT_OWNER_SETTINGS, PROPOSAL_SECTIONS, PROPOSAL_STATUSES, SECTION_TITLES
from settings_store import get_owner_settings

QUOTED_STATUSES = ("Quote Received", "Selected")


def _selected_sub_references(session: Session, contract_id: int) -> list[dict[str, Any]]:
    from sub_contact_service import references_for_selected_sub

    return references_for_selected_sub(session, contract_id)


def _dec(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def calculate_bid_pricing(
    sub_quote: float,
    margin_pct: float,
    option_years: int = 4,
    increase_pct: float = 3.0,
) -> dict[str, Any]:
    margin = max(10.0, min(35.0, margin_pct))
    sub = float(sub_quote)
    base_bid = sub / (1 - margin / 100.0) if margin < 100 else sub
    profit = base_bid - sub

    years: dict[str, float] = {"base_year": round(base_bid, 2)}
    prev = base_bid
    mult = 1 + increase_pct / 100.0
    for i in range(1, max(0, option_years) + 1):
        prev = prev * mult
        years[f"option_year_{i}"] = round(prev, 2)

    total = round(sum(years.values()), 2)
    return {
        "sub_quote": round(sub, 2),
        "margin_percentage": margin,
        "base_year_bid": years["base_year"],
        "base_year_profit": round(profit, 2),
        "option_year_increase_pct": increase_pct,
        "option_years": years,
        "total_all_years": total,
    }


def bid_range_status(base_bid: float, pricing: dict[str, Any] | None) -> dict[str, str]:
    """Green/yellow/red vs internal or regional recommended range."""
    if not pricing:
        return {"level": "neutral", "message": ""}
    internal = pricing.get("internal") or {}
    low = internal.get("recommended_bid_low")
    high = internal.get("recommended_bid_high")
    if not low and not high:
        regional = pricing.get("regional_benchmark") or {}
        avg = regional.get("average_annual_award")
        if avg:
            low = avg * 0.85
            high = avg * 1.15
    if not low or not high:
        return {"level": "neutral", "message": "No regional benchmark available for comparison."}
    if base_bid <= high:
        if base_bid >= low:
            return {"level": "green", "message": "Within recommended bid range."}
        return {"level": "green", "message": "Below recommended range — aggressive bid."}
    if base_bid <= high * 1.15:
        return {
            "level": "yellow",
            "message": "Slightly above regional average — still competitive.",
        }
    return {
        "level": "red",
        "message": "Significantly above regional average. Consider reducing margin to improve win probability.",
    }


def quoted_subs_for_contract(session: Session, notice_id: str) -> dict[str, Any]:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")
    links = (
        session.query(ContractSub)
        .options(joinedload(ContractSub.sub))
        .filter(
            ContractSub.contract_id == contract.id,
            ContractSub.status.in_(QUOTED_STATUSES),
            ContractSub.quote_amount.isnot(None),
        )
        .all()
    )
    subs = []
    for link in links:
        sub = link.sub
        subs.append(
            {
                "contract_sub_id": link.id,
                "sub_id": sub.id if sub else None,
                "business_name": sub.business_name if sub else "Unknown",
                "rating": _dec(sub.rating) if sub else None,
                "review_count": sub.review_count if sub else None,
                "quote_amount": _dec(link.quote_amount),
                "distance_miles": _dec(link.distance_miles),
                "contact_notes": link.contact_notes,
                "status": link.status,
                "is_selected": link.status == "Selected",
            }
        )
    return {
        "notice_id": notice_id,
        "contract_title": contract.title,
        "has_quotes": len(subs) > 0,
        "subs": subs,
    }


def _extract_solicitation_meta(contract: Contract) -> dict[str, Any]:
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    sam = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    pws = analysis.get("pws_extraction") if isinstance(analysis.get("pws_extraction"), dict) else {}
    sol = analysis.get("solicitation_meta") if isinstance(analysis.get("solicitation_meta"), dict) else {}

    solicitation_number = (
        sol.get("solicitation_number")
        or sam.get("solicitationNumber")
        or sam.get("noticeId")
        or contract.notice_id
    )
    return {
        "solicitation_number": solicitation_number,
        "contracting_officer_name": sol.get("contracting_officer_name") or analysis.get("contracting_officer_name"),
        "contracting_officer_email": sol.get("contracting_officer_email") or analysis.get("contracting_officer_email"),
        "submission_method": sol.get("submission_method") or analysis.get("submission_method"),
        "submission_deadline": contract.due_date.isoformat() if contract.due_date else None,
        "base_year_start": sol.get("base_year_start") or pws.get("base_year_start"),
        "base_year_end": sol.get("base_year_end") or pws.get("base_year_end"),
        "option_years": analysis.get("option_years") or sol.get("option_years") or 4,
        "place_of_performance": contract.location,
        "agency_address": sol.get("agency_address") or analysis.get("agency_address"),
    }


def _solicitation_meta_complete(analysis: dict[str, Any]) -> bool:
    sol = analysis.get("solicitation_meta") if isinstance(analysis.get("solicitation_meta"), dict) else {}
    for key in ("contracting_officer_name", "contracting_officer_email", "submission_method"):
        if str(sol.get(key) or analysis.get(key) or "").strip():
            continue
        return False
    return True


CLEANING_NAICS = frozenset({"561720", "561790", "561740", "561210"})
SETTINGS_ONLY_FIELDS = frozenset(
    {"address_line_1", "city", "zip", "uei", "cage_code", "ein", "sam_expiration"}
)


def sync_config_from_contract(config: dict[str, Any], contract: Contract) -> dict[str, Any]:
    """Merge latest attachment extraction and contract scope into proposal config."""
    config = dict(config)
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    sol = _extract_solicitation_meta(contract)
    drawing = analysis.get("drawing_sqft_extraction") if isinstance(analysis.get("drawing_sqft_extraction"), dict) else {}

    sec_a = dict(config.get("section_a") or {})
    auto_fields = {
        "contract_title": contract.title,
        "solicitation_number": sol.get("solicitation_number"),
        "agency_name": contract.agency,
        "contracting_officer_name": sol.get("contracting_officer_name"),
        "contracting_officer_email": sol.get("contracting_officer_email"),
        "submission_method": sol.get("submission_method"),
        "submission_deadline": sol.get("submission_deadline"),
        "base_year_start": sol.get("base_year_start"),
        "base_year_end": sol.get("base_year_end"),
        "place_of_performance": contract.location or sol.get("place_of_performance"),
        "agency_address": sol.get("agency_address"),
    }
    for key, value in auto_fields.items():
        if value is not None and str(value).strip() and not str(sec_a.get(key) or "").strip():
            sec_a[key] = value
    config["section_a"] = sec_a

    config["proposal_requirements"] = (
        analysis.get("proposal_requirements") if isinstance(analysis.get("proposal_requirements"), dict) else {}
    )
    config["pws_extraction"] = (
        analysis.get("pws_extraction") if isinstance(analysis.get("pws_extraction"), dict) else {}
    )
    config["contract"] = {
        "notice_id": contract.notice_id,
        "square_footage": contract.square_footage,
        "building_type": contract.building_type,
        "cleaning_frequency_per_week": float(contract.cleaning_frequency_per_week)
        if contract.cleaning_frequency_per_week is not None
        else None,
        "special_requirements": contract.special_requirements,
        "plain_english_summary": analysis_plain(contract),
        "naics_code": contract.naics_code,
    }
    config["attachment_context"] = {
        "pdfs_read": int(analysis.get("pdfs_sent_to_claude") or 0),
        "attachments_reviewed": analysis.get("attachments_reviewed") or [],
        "square_footage_estimated": bool(drawing.get("estimated")),
        "drawing_sqft_notes": drawing.get("calculation_notes"),
    }
    return config


def build_proposal_readiness(contract: Contract, config: dict[str, Any]) -> dict[str, Any]:
    """Structured checklist — critical gaps block generation; settings gaps block submission only."""
    missing = detect_missing_fields(config, contract=contract)
    critical = [m for m in missing if m.get("field") not in SETTINGS_ONLY_FIELDS]
    warnings = [m for m in missing if m.get("field") in SETTINGS_ONLY_FIELDS]

    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    req = config.get("proposal_requirements") if isinstance(config.get("proposal_requirements"), dict) else {}
    factors = req.get("section_m_evaluation_factors") or []
    pws_reqs = req.get("pws_requirements") or []
    pdfs = int(analysis.get("pdfs_sent_to_claude") or 0)
    att = config.get("attachment_context") or {}
    estimated = att.get("square_footage_estimated")

    sqft_label = "Square footage missing"
    if contract.square_footage:
        sqft_label = f"Square footage: {int(contract.square_footage):,} sq ft"
        if estimated:
            sqft_label += " (estimated from floor plans)"

    freq_label = "Cleaning frequency missing"
    if contract.cleaning_frequency_per_week is not None:
        freq_label = f"Cleaning frequency: {contract.cleaning_frequency_per_week:g} days/week"

    checks: list[dict[str, Any]] = [
        {"ok": pdfs > 0, "label": f"Solicitation PDFs read ({pdfs})" if pdfs else "Solicitation PDFs not read", "field": "pdfs"},
        {
            "ok": isinstance(factors, list) and len(factors) >= 1,
            "label": f"Section M factors ({len(factors)})" if factors else "Section M evaluation factors missing",
            "field": "section_m_evaluation_factors",
        },
        {
            "ok": isinstance(pws_reqs, list) and len(pws_reqs) >= 3,
            "label": f"PWS requirements ({len(pws_reqs)})" if len(pws_reqs) >= 3 else "PWS requirements incomplete",
            "field": "pws_requirements",
        },
        {"ok": bool(contract.square_footage), "label": sqft_label, "field": "square_footage"},
        {
            "ok": contract.cleaning_frequency_per_week is not None
            or str(contract.naics_code or "") not in CLEANING_NAICS,
            "label": freq_label,
            "field": "cleaning_frequency_per_week",
        },
        {
            "ok": bool(str(sec_a_val := (config.get("section_a") or {}).get("contracting_officer_name") or "").strip()),
            "label": f"Contracting Officer: {sec_a_val}" if sec_a_val else "Contracting Officer name missing",
            "field": "contracting_officer_name",
        },
        {
            "ok": bool(str((config.get("section_a") or {}).get("submission_method") or "").strip()),
            "label": f"Submission: {(config.get('section_a') or {}).get('submission_method')}"
            if (config.get("section_a") or {}).get("submission_method")
            else "Submission method missing",
            "field": "submission_method",
        },
        {
            "ok": bool((config.get("sub") or {}).get("quote_amount")),
            "label": f"Sub quote: ${float((config.get('sub') or {}).get('quote_amount') or 0):,.2f}",
            "field": "sub_quote",
        },
    ]
    for m in warnings:
        checks.append({"ok": False, "label": f"Settings — {m['label']}", "field": m["field"]})

    return {
        "ready_to_generate": len(critical) == 0,
        "ready_to_submit": len(missing) == 0,
        "critical_count": len(critical),
        "warning_count": len(warnings),
        "checks": checks,
        "critical": critical,
        "warnings": warnings,
    }


def _contract_pws_missing(contract: Contract) -> bool:
    from pws_fields import contract_pws_missing

    return contract_pws_missing(contract)


def _proposal_requirements_complete(analysis: dict[str, Any]) -> bool:
    req = analysis.get("proposal_requirements") if isinstance(analysis.get("proposal_requirements"), dict) else {}
    factors = req.get("section_m_evaluation_factors") or []
    pws_reqs = req.get("pws_requirements") or []
    has_m = isinstance(factors, list) and len(factors) >= 1
    has_pws = isinstance(pws_reqs, list) and len(pws_reqs) >= 3
    return has_m and has_pws


def ensure_proposal_requirements(session: Session, contract: Contract, *, force: bool = False) -> dict[str, Any]:
    """Extract Section L/M and PWS requirements from solicitation PDFs for proposal writing."""
    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
    existing = analysis.get("proposal_requirements") if isinstance(analysis.get("proposal_requirements"), dict) else {}

    if _proposal_requirements_complete(analysis) and not force:
        return existing

    from api_budget import ScreenBudgetExceeded, can_screen, record_screen_usage
    from claude_client import extract_proposal_requirements

    if not can_screen():
        return existing

    extracted = extract_proposal_requirements(contract)
    if extracted:
        analysis["proposal_requirements"] = extracted
        contract.analysis = analysis
        session.commit()
        session.refresh(contract)
        if not record_screen_usage():
            raise ScreenBudgetExceeded()
        return extracted
    return existing


def ensure_proposal_context(session: Session, contract: Contract, *, force: bool = False) -> None:
    """Load solicitation metadata, scope, and L/M/PWS requirements from attachments before proposal writing."""
    ensure_solicitation_meta(session, contract, force=force)
    session.refresh(contract)
    ensure_proposal_requirements(session, contract, force=force)
    session.refresh(contract)


def ensure_solicitation_meta(session: Session, contract: Contract, *, force: bool = False) -> dict[str, Any]:
    """Fill solicitation_meta and PWS scope from analysis or extract from bid PDFs on demand."""
    from pws_fields import apply_pws_extraction

    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
    sol = dict(analysis.get("solicitation_meta") or {}) if isinstance(analysis.get("solicitation_meta"), dict) else {}

    if _solicitation_meta_complete(analysis) and not _contract_pws_missing(contract) and not force:
        return sol

    from api_budget import ScreenBudgetExceeded, can_screen, record_screen_usage
    from claude_client import extract_solicitation_meta

    if not can_screen():
        return sol

    extracted = extract_solicitation_meta(contract)
    if extracted:
        pws = extracted.pop("pws_extraction", None)
        pkg = extracted.pop("submission_package", None)
        sol.update({k: v for k, v in extracted.items() if v})
        analysis["solicitation_meta"] = sol
        for key in (
            "contracting_officer_name",
            "contracting_officer_email",
            "submission_method",
            "base_year_start",
            "base_year_end",
            "agency_address",
            "solicitation_number",
            "questions_deadline",
        ):
            if sol.get(key) and not analysis.get(key):
                analysis[key] = sol[key]
        if isinstance(pws, dict) and pws:
            analysis["pws_extraction"] = pws
        if isinstance(pkg, dict):
            analysis["submission_package"] = pkg
        contract.analysis = analysis
        from claude_client import contract_attachment_text
        from pws_fields import supplement_pws_from_pdf_text

        supplement_pws_from_pdf_text(analysis, contract_attachment_text(contract))
        apply_pws_extraction(contract, analysis)
        from submission_package import apply_submission_package

        apply_submission_package(contract, session, analysis=analysis)
        if not record_screen_usage():
            raise ScreenBudgetExceeded()

    if contract.square_footage is None and can_screen():
        from claude_client import try_extract_sqft_from_drawings

        if try_extract_sqft_from_drawings(contract, analysis):
            contract.analysis = analysis
            apply_pws_extraction(contract, analysis)
        if not record_screen_usage():
            raise ScreenBudgetExceeded()

    if extracted or contract.square_footage is not None:
        session.commit()
        session.refresh(contract)
    return sol


def build_proposal_config(
    session: Session,
    notice_id: str,
    *,
    contract_sub_id: int,
    margin_pct: float | None = None,
    option_increase_pct: float | None = None,
) -> dict[str, Any]:
    from pricing import get_full_pricing_intel

    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")

    ensure_proposal_context(session, contract)

    link = (
        session.query(ContractSub)
        .options(joinedload(ContractSub.sub))
        .filter_by(id=contract_sub_id, contract_id=contract.id)
        .first()
    )
    if not link or not link.quote_amount:
        raise ValueError("Selected sub must have a quote amount")
    if link.status not in QUOTED_STATUSES:
        raise ValueError("Sub must have status Quote Received or Selected")

    owner = get_owner_settings()
    if margin_pct is not None:
        margin = margin_pct
    else:
        from proposal_defaults import resolve_contract_margin

        margin = resolve_contract_margin(contract, owner)
    increase = option_increase_pct if option_increase_pct is not None else float(
        owner.get("default_option_year_increase_pct", 3)
    )
    option_years = int(_extract_solicitation_meta(contract).get("option_years") or 4)
    pricing_math = calculate_bid_pricing(
        float(link.quote_amount), margin, option_years=option_years, increase_pct=increase
    )
    full_pricing = get_full_pricing_intel(contract, session)
    range_status = bid_range_status(pricing_math["base_year_bid"], full_pricing)

    sol = _extract_solicitation_meta(contract)
    sub = link.sub
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    config = {
        "section_a": {
            "contract_title": contract.title,
            "solicitation_number": sol["solicitation_number"],
            "agency_name": contract.agency,
            "contracting_officer_name": sol["contracting_officer_name"],
            "contracting_officer_email": sol["contracting_officer_email"],
            "submission_method": sol["submission_method"],
            "submission_deadline": sol["submission_deadline"],
            "base_year_start": sol["base_year_start"],
            "base_year_end": sol["base_year_end"],
            "option_years": option_years,
            "place_of_performance": sol["place_of_performance"],
            "agency_address": sol["agency_address"],
        },
        "section_b": owner,
        "section_c": {**pricing_math, "bid_range_status": range_status, "pricing": full_pricing},
        "section_d": {
            "include_past_performance": True,
            "include_capability_statement": True,
            "writing_tone": "Professional",
            "technical_detail": "Detailed",
            "sub_references": _selected_sub_references(session, contract.id),
        },
        "pws_extraction": analysis.get("pws_extraction") if isinstance(analysis.get("pws_extraction"), dict) else {},
        "proposal_requirements": analysis.get("proposal_requirements")
        if isinstance(analysis.get("proposal_requirements"), dict)
        else {},
        "sub": {
            "contract_sub_id": link.id,
            "sub_id": sub.id if sub else None,
            "business_name": sub.business_name if sub else None,
            "rating": _dec(sub.rating) if sub else None,
            "review_count": sub.review_count if sub else None,
            "quote_amount": _dec(link.quote_amount),
            "distance_miles": _dec(link.distance_miles),
            "notes": link.contact_notes,
            "references": _selected_sub_references(session, contract.id),
        },
        "contract": {
            "notice_id": notice_id,
            "square_footage": contract.square_footage,
            "building_type": contract.building_type,
            "cleaning_frequency_per_week": float(contract.cleaning_frequency_per_week)
            if contract.cleaning_frequency_per_week is not None
            else None,
            "special_requirements": contract.special_requirements,
            "plain_english_summary": analysis_plain(contract),
            "multiple_pricing_encouraged": bool(contract.multiple_pricing_encouraged),
            "pricing_schedule_required": bool(contract.pricing_schedule_required),
        },
        "pricing_tiers_instruction": (
            "Generate THREE pricing tiers in the price schedule: (1) base requirement meeting minimum PWS, "
            "(2) enhanced option with additional services, (3) premium option — each with margin built into "
            "the bid amounts shown in config."
            if contract.multiple_pricing_encouraged
            else "Generate a single price schedule only — multiple pricing options were not encouraged."
        ),
    }
    config = sync_config_from_contract(config, contract)
    config["readiness"] = build_proposal_readiness(contract, config)
    config["missing_fields"] = detect_missing_fields(config, contract=contract)
    return config


def analysis_plain(contract: Contract) -> str:
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    return analysis.get("plain_english_summary") or analysis.get("executive_summary") or ""


def detect_missing_fields(
    config: dict[str, Any],
    *,
    contract: Contract | None = None,
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    owner = config.get("section_b") or {}
    required_owner = [
        ("address_line_1", "Business address", "settings"),
        ("city", "City", "settings"),
        ("zip", "ZIP code", "settings"),
        ("uei", "UEI number", "settings"),
        ("cage_code", "CAGE code", "settings"),
        ("ein", "EIN", "settings"),
        ("sam_expiration", "SAM registration expiration", "settings"),
    ]
    for key, label, where in required_owner:
        if not str(owner.get(key) or "").strip():
            missing.append({"field": key, "label": label, "where": where})

    sec_a = config.get("section_a") or {}
    for key, label in [
        ("contracting_officer_name", "Contracting Officer name"),
        ("contracting_officer_email", "Contracting Officer email"),
        ("submission_method", "Submission method"),
    ]:
        if not str(sec_a.get(key) or "").strip():
            missing.append({"field": key, "label": label, "where": "solicitation"})

    req = config.get("proposal_requirements") if isinstance(config.get("proposal_requirements"), dict) else {}
    factors = req.get("section_m_evaluation_factors") or []
    pws_reqs = req.get("pws_requirements") or []
    if not isinstance(factors, list) or len(factors) < 1:
        missing.append(
            {
                "field": "section_m_evaluation_factors",
                "label": "Section M evaluation factors (from solicitation PDFs)",
                "where": "solicitation",
            }
        )
    if not isinstance(pws_reqs, list) or len(pws_reqs) < 3:
        missing.append(
            {
                "field": "pws_requirements",
                "label": "PWS performance requirements (from solicitation PDFs)",
                "where": "solicitation",
            }
        )

    if contract is not None:
        from screening_pipeline import has_attachments_ready, pdfs_expected_on_contract, pdfs_read_in_analysis

        analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
        if not has_attachments_ready(contract):
            missing.append(
                {
                    "field": "attachments",
                    "label": "Solicitation attachments not downloaded",
                    "where": "solicitation",
                }
            )
        elif pdfs_expected_on_contract(contract) and not pdfs_read_in_analysis(analysis):
            missing.append(
                {
                    "field": "pdfs",
                    "label": "Solicitation PDFs not read from attachments",
                    "where": "solicitation",
                }
            )
        elif int(analysis.get("pdfs_sent_to_claude") or 0) < 1:
            missing.append(
                {
                    "field": "pdfs",
                    "label": "No solicitation PDFs sent to Claude yet",
                    "where": "solicitation",
                }
            )

        naics = str(contract.naics_code or "")
        if naics in CLEANING_NAICS:
            if not contract.square_footage:
                missing.append(
                    {
                        "field": "square_footage",
                        "label": "Square footage (from PWS or floor-plan drawings)",
                        "where": "solicitation",
                    }
                )
            if contract.cleaning_frequency_per_week is None:
                missing.append(
                    {
                        "field": "cleaning_frequency_per_week",
                        "label": "Cleaning frequency (from PWS)",
                        "where": "solicitation",
                    }
                )

    sub = config.get("sub") or {}
    if not sub.get("quote_amount"):
        missing.append({"field": "sub_quote", "label": "Subcontractor quote amount", "where": "subs"})

    return missing


def generate_proposal(
    session: Session,
    notice_id: str,
    config: dict[str, Any],
) -> Proposal:
    from claude_client import generate_proposal_content

    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")

    ensure_proposal_context(session, contract)
    session.refresh(contract)

    config = sync_config_from_contract(config, contract)
    readiness = build_proposal_readiness(contract, config)
    config["readiness"] = readiness
    config["missing_fields"] = detect_missing_fields(config, contract=contract)
    if not readiness.get("ready_to_generate"):
        labels = ", ".join(m["label"] for m in readiness.get("critical") or [])
        raise ValueError(
            "Proposal data is incomplete — attachment extraction must finish before generating. "
            f"Missing: {labels}"
        )

    existing = (
        session.query(Proposal)
        .filter_by(contract_id=contract.id)
        .order_by(Proposal.date_updated.desc())
        .first()
    )
    if existing and existing.status in ("submitted", "won"):
        raise ValueError(
            "This contract already has a submitted bid on file. "
            "Open the existing proposal — do not create a duplicate application."
        )

    html, sections = generate_proposal_content(contract, config)
    missing = detect_missing_fields(config, contract=contract)
    html = highlight_missing_in_html(html, missing)

    sub = config.get("sub") or {}
    pricing = config.get("section_c") or {}
    sec_a = config.get("section_a") or {}
    opt = pricing.get("option_years") or {}

    proposal = Proposal(
        contract_id=contract.id,
        sub_id=sub.get("sub_id"),
        contract_sub_id=sub.get("contract_sub_id"),
        sub_name=sub.get("business_name"),
        sub_quote=Decimal(str(pricing.get("sub_quote"))) if pricing.get("sub_quote") else None,
        margin_percentage=Decimal(str(pricing.get("margin_percentage", 20))),
        base_year_bid=Decimal(str(pricing.get("base_year_bid"))) if pricing.get("base_year_bid") else None,
        option_year_1=Decimal(str(opt.get("option_year_1"))) if opt.get("option_year_1") else None,
        option_year_2=Decimal(str(opt.get("option_year_2"))) if opt.get("option_year_2") else None,
        option_year_3=Decimal(str(opt.get("option_year_3"))) if opt.get("option_year_3") else None,
        option_year_4=Decimal(str(opt.get("option_year_4"))) if opt.get("option_year_4") else None,
        total_all_years=Decimal(str(pricing.get("total_all_years"))) if pricing.get("total_all_years") else None,
        option_year_increase_pct=Decimal(str(pricing.get("option_year_increase_pct", 3))),
        proposal_html=html,
        sections_json=sections,
        config_json=config,
        status="draft",
        version_history=[],
        contracting_officer_name=sec_a.get("contracting_officer_name"),
        submission_method=sec_a.get("submission_method"),
        submission_deadline=sec_a.get("submission_deadline"),
        missing_fields=missing,
    )
    margin_val = pricing.get("margin_percentage")
    if margin_val is not None:
        contract.margin_percentage = Decimal(str(margin_val))

    session.add(proposal)
    session.commit()
    session.refresh(proposal)
    return proposal


def highlight_missing_in_html(html: str, missing: list[dict[str, str]]) -> str:
    return html


def parse_sections_from_html(html: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    patterns = [
        (r"SECTION\s*1[^<]*COMPLIANCE", "compliance_matrix"),
        (r"SECTION\s*2[^<]*COVER\s*LETTER", "cover_letter"),
        (r"SECTION\s*3[^<]*TECHNICAL", "technical_approach"),
        (r"SECTION\s*4[^<]*PRICE", "price_schedule"),
        (r"SECTION\s*5[^<]*PAST\s*PERFORMANCE", "past_performance"),
        (r"SECTION\s*6[^<]*CAPABILITY", "capability_statement"),
        (r"SECTION\s*7[^<]*CERTIFICATION", "certifications"),
    ]
    upper = html.upper()
    for i, (pattern, key) in enumerate(patterns):
        match = re.search(pattern, upper)
        if not match:
            continue
        start = match.start()
        end = len(html)
        for j in range(i + 1, len(patterns)):
            m2 = re.search(patterns[j][0], upper[start + 10 :])
            if m2:
                end = start + 10 + m2.start()
                break
        sections[key] = html[start:end].strip()
    if not sections:
        sections["full"] = html
    return sections


def save_proposal_draft(session: Session, proposal_id: int, payload: dict[str, Any]) -> Proposal:
    proposal = session.get(Proposal, proposal_id)
    if not proposal:
        raise ValueError("Proposal not found")

    history = list(proposal.version_history or [])
    if proposal.proposal_html:
        history.append(
            {
                "html": proposal.proposal_html,
                "sections": proposal.sections_json,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        history = history[-20:]

    if "proposal_html" in payload:
        proposal.proposal_html = payload["proposal_html"]
    if "sections_json" in payload:
        proposal.sections_json = payload["sections_json"]
    if "notes" in payload:
        proposal.notes = payload["notes"]
    if "status" in payload and payload["status"] in PROPOSAL_STATUSES:
        if payload["status"] == "submitted":
            contract = session.get(Contract, proposal.contract_id)
            config = proposal.config_json if isinstance(proposal.config_json, dict) else {}
            if contract:
                config = sync_config_from_contract(config, contract)
            missing = detect_missing_fields(config, contract=contract)
            if missing:
                labels = ", ".join(m["label"] for m in missing if isinstance(m, dict))
                raise ValueError(f"Cannot mark submitted until required fields are filled: {labels}")
        proposal.status = payload["status"]
        if payload["status"] == "submitted":
            proposal.date_submitted = datetime.now(timezone.utc)
    if "winning_bid_amount" in payload and payload["winning_bid_amount"] is not None:
        proposal.winning_bid_amount = Decimal(str(payload["winning_bid_amount"]))

    proposal.version_history = history
    proposal.date_updated = datetime.now(timezone.utc)
    session.commit()
    session.refresh(proposal)
    return proposal


def restore_proposal_version(session: Session, proposal_id: int, version_index: int) -> Proposal:
    proposal = session.get(Proposal, proposal_id)
    if not proposal:
        raise ValueError("Proposal not found")

    history = list(proposal.version_history or [])
    if version_index < 0 or version_index >= len(history):
        raise ValueError("Version not found")

    if proposal.proposal_html:
        history.append(
            {
                "html": proposal.proposal_html,
                "sections": proposal.sections_json,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "note": "Before restore",
            }
        )

    snap = history[version_index]
    proposal.proposal_html = snap.get("html") or ""
    proposal.sections_json = snap.get("sections") or parse_sections_from_html(proposal.proposal_html)
    proposal.version_history = history[-20:]
    proposal.date_updated = datetime.now(timezone.utc)
    session.commit()
    session.refresh(proposal)
    return proposal


def format_version_history(history: list | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, snap in enumerate(history or []):
        if not isinstance(snap, dict):
            continue
        html = snap.get("html") or ""
        text = re.sub(r"<[^>]+>", " ", html)
        preview = " ".join(text.split())[:100]
        rows.append(
            {
                "index": idx,
                "saved_at": snap.get("saved_at"),
                "preview": preview,
                "note": snap.get("note"),
            }
        )
    return rows


def proposal_to_dict(proposal: Proposal) -> dict[str, Any]:
    sections = proposal.sections_json or {}
    if not sections and proposal.proposal_html:
        sections = parse_sections_from_html(proposal.proposal_html)
    word_counts = {k: _word_count(v) for k, v in sections.items()}
    return {
        "id": proposal.id,
        "contract_id": proposal.contract_id,
        "notice_id": proposal.contract.notice_id if proposal.contract else None,
        "sub_id": proposal.sub_id,
        "sub_name": proposal.sub_name,
        "sub_quote": _dec(proposal.sub_quote),
        "margin_percentage": _dec(proposal.margin_percentage),
        "base_year_bid": _dec(proposal.base_year_bid),
        "option_year_1": _dec(proposal.option_year_1),
        "option_year_2": _dec(proposal.option_year_2),
        "option_year_3": _dec(proposal.option_year_3),
        "option_year_4": _dec(proposal.option_year_4),
        "total_all_years": _dec(proposal.total_all_years),
        "proposal_html": proposal.proposal_html,
        "sections": sections,
        "section_titles": SECTION_TITLES,
        "word_counts": word_counts,
        "total_word_count": sum(word_counts.values()),
        "status": proposal.status,
        "version_history": proposal.version_history or [],
        "versions": format_version_history(proposal.version_history),
        "missing_fields": proposal.missing_fields or [],
        "config": proposal.config_json,
        "notes": proposal.notes,
        "date_created": proposal.date_created.isoformat() if proposal.date_created else None,
        "date_updated": proposal.date_updated.isoformat() if proposal.date_updated else None,
    }


def _word_count(html: str) -> int:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return len(text.split())


def regenerate_section(session: Session, proposal_id: int, section_key: str) -> Proposal:
    from claude_client import regenerate_proposal_section

    proposal = session.get(Proposal, proposal_id)
    if not proposal or not proposal.config_json:
        raise ValueError("Proposal not found")
    if section_key not in PROPOSAL_SECTIONS:
        raise ValueError(f"Invalid section: {section_key}")

    contract = session.get(Contract, proposal.contract_id)
    if not contract:
        raise ValueError("Contract not found")

    ensure_proposal_context(session, contract)
    session.refresh(contract)

    new_html = regenerate_proposal_section(contract, proposal.config_json, section_key)
    sections = dict(proposal.sections_json or parse_sections_from_html(proposal.proposal_html or ""))
    sections[section_key] = new_html
    full = _rebuild_full_html(sections)
    proposal.sections_json = sections
    proposal.proposal_html = full
    session.commit()
    session.refresh(proposal)
    return proposal


def _rebuild_full_html(sections: dict[str, str]) -> str:
    parts = []
    for key in PROPOSAL_SECTIONS:
        if sections.get(key):
            parts.append(sections[key])
    return "\n\n".join(parts) if parts else sections.get("full", "")


def humanize_selection(session: Session, proposal_id: int, selected_html: str) -> str:
    from claude_client import humanize_proposal_text

    return humanize_proposal_text(selected_html)


def reduce_ai_score_pass(session: Session, proposal_id: int) -> Proposal:
    from claude_client import reduce_proposal_ai_score

    proposal = session.get(Proposal, proposal_id)
    if not proposal or not proposal.proposal_html:
        raise ValueError("Proposal not found")
    new_html = reduce_proposal_ai_score(proposal.proposal_html)
    proposal.proposal_html = new_html
    proposal.sections_json = parse_sections_from_html(new_html)
    session.commit()
    session.refresh(proposal)
    return proposal

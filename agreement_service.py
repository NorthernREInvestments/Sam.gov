"""Subcontract agreement generation — config assembly, Claude fill-in, persistence."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, joinedload

from models import Contract, ContractSub, Sub, SubcontractAgreement
from proposal_service import _extract_solicitation_meta, ensure_solicitation_meta
from settings_store import get_owner_settings
from sub_constants import AGREEMENT_SIGNATURE_STATUSES, DEFAULT_AGREEMENT_SIGNATURE_STATUS

PRIME_ADDRESS_DEFAULTS = {
    "address_line_1": "1527 22nd Street",
    "city": "Mitchell",
    "state": "Nebraska",
    "zip": "69357",
    "business_state": "Wyoming",
    "business_email": "markg@northernreinvestments.com",
}


def _dec(value: Decimal | float | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _format_money(value: float | Decimal | None) -> str:
    if value is None:
        return "To be provided"
    return f"${float(value):,.2f}"


def _option_monthly_amounts(base_monthly: float, years: int = 4, increase_pct: float = 3.0) -> dict[str, float]:
    amounts: dict[str, float] = {}
    prev = base_monthly
    mult = 1 + increase_pct / 100.0
    for i in range(1, max(1, years) + 1):
        prev = round(prev * mult, 2)
        amounts[f"option_year_{i}"] = prev
    return amounts


def _prime_contractor_block(owner: dict[str, Any]) -> dict[str, Any]:
    merged = {**PRIME_ADDRESS_DEFAULTS, **owner}
    addr1 = str(merged.get("address_line_1") or PRIME_ADDRESS_DEFAULTS["address_line_1"]).strip()
    addr2 = str(merged.get("address_line_2") or "").strip()
    city = str(merged.get("city") or PRIME_ADDRESS_DEFAULTS["city"]).strip()
    state = str(merged.get("state") or merged.get("business_state") or "WY").strip()
    zip_code = str(merged.get("zip") or PRIME_ADDRESS_DEFAULTS["zip"]).strip()
    return {
        "legal_business_name": str(merged.get("legal_business_name") or "Northern RE Investments LLC"),
        "entity_type": f"A {merged.get('business_state') or 'Wyoming'} Limited Liability Company",
        "address_line_1": addr1,
        "address_line_2": addr2,
        "city": city,
        "state": state,
        "zip": zip_code,
        "owner_name": str(merged.get("owner_name") or "Mark Graham II"),
        "owner_title": str(merged.get("owner_title") or "Owner"),
        "business_phone": str(merged.get("business_phone") or "To be provided"),
        "business_email": str(merged.get("business_email") or PRIME_ADDRESS_DEFAULTS["business_email"]),
    }


def append_agreement_status_log(
    link: ContractSub,
    status: str,
    *,
    note: str = "",
) -> None:
    log = list(link.agreement_status_log or [])
    log.append(
        {
            "status": status,
            "at": datetime.now(timezone.utc).isoformat(),
            "note": note or "",
        }
    )
    link.agreement_status_log = log[-50:]


def build_agreement_config(session: Session, link_id: int) -> dict[str, Any]:
    link = (
        session.query(ContractSub)
        .options(joinedload(ContractSub.sub), joinedload(ContractSub.contract))
        .filter_by(id=link_id)
        .first()
    )
    if not link:
        raise ValueError("Contract sub link not found")
    if link.status != "Selected":
        raise ValueError("Sub must be marked Selected before generating an agreement")
    if not link.quote_amount:
        raise ValueError("Selected sub must have a quote amount")

    contract = link.contract
    sub = link.sub
    if not contract or not sub:
        raise ValueError("Contract or sub record missing")

    ensure_solicitation_meta(session, contract)
    sol = _extract_solicitation_meta(contract)
    owner = get_owner_settings()
    prime = _prime_contractor_block(owner)

    option_years = int(sol.get("option_years") or 4)
    increase_pct = float(owner.get("default_option_year_increase_pct") or 3)
    base_monthly = float(link.quote_amount)
    option_amounts = _option_monthly_amounts(base_monthly, years=min(option_years, 4), increase_pct=increase_pct)

    sub_city_state_zip = ", ".join(
        p for p in [sub.city, sub.state, sub.zip] if p
    )
    wage_rate = contract.wage_determination_rate
    if wage_rate is None:
        analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
        pws = analysis.get("pws_extraction") if isinstance(analysis.get("pws_extraction"), dict) else {}
        raw_rate = pws.get("wage_determination_rate")
        if raw_rate is not None:
            try:
                wage_rate = Decimal(str(raw_rate))
            except (TypeError, ValueError):
                wage_rate = None

    config = {
        "agreement_date": date.today().isoformat(),
        "prime_contractor": prime,
        "contract": {
            "notice_id": contract.notice_id,
            "title": contract.title,
            "solicitation_number": sol.get("solicitation_number"),
            "agency_name": contract.agency,
            "place_of_performance": sol.get("place_of_performance") or contract.location,
            "base_year_start": sol.get("base_year_start"),
            "base_year_end": sol.get("base_year_end"),
            "option_years": option_years,
            "wage_determination_number": contract.wage_determination_number,
            "wage_determination_rate": _dec(wage_rate),
        },
        "subcontractor": {
            "sub_id": sub.id,
            "contract_sub_id": link.id,
            "legal_business_name": sub.business_name,
            "address": sub.address,
            "city_state_zip": sub_city_state_zip,
            "owner_name": sub.owner_name,
            "owner_title": sub.owner_title or "Owner",
            "phone": sub.phone,
            "license_number": sub.license_number,
            "insurance_carrier": sub.insurance_carrier,
            "email": sub.business_email,
            "quote_amount_monthly": base_monthly,
            "quote_amount_monthly_display": _format_money(base_monthly),
            "option_year_amounts": {k: _format_money(v) for k, v in option_amounts.items()},
            "option_year_1": _format_money(option_amounts.get("option_year_1")),
            "option_year_2": _format_money(option_amounts.get("option_year_2")),
            "option_year_3": _format_money(option_amounts.get("option_year_3")),
            "option_year_4": _format_money(option_amounts.get("option_year_4")),
        },
        "field_map": {
            "DATE": date.today().strftime("%B %d, %Y"),
            "PHONE FROM SETTINGS": prime["business_phone"],
            "SUB LEGAL BUSINESS NAME": sub.business_name,
            "SUB ADDRESS": sub.address or "To be provided",
            "SUB CITY STATE ZIP": sub_city_state_zip or "To be provided",
            "SUB OWNER NAME": sub.owner_name or "To be provided",
            "SUB PHONE": sub.phone or "To be provided",
            "SUB LICENSE NUMBER": sub.license_number or "To be provided",
            "SOLICITATION NUMBER": sol.get("solicitation_number"),
            "AGENCY NAME": contract.agency or "To be provided",
            "CONTRACT TITLE": contract.title,
            "PLACE OF PERFORMANCE": sol.get("place_of_performance") or contract.location or "To be provided",
            "START DATE": sol.get("base_year_start") or "To be provided",
            "END DATE": sol.get("base_year_end") or "To be provided",
            "SUB QUOTE AMOUNT": _format_money(base_monthly),
            "OY1 AMOUNT": _format_money(option_amounts.get("option_year_1")),
            "OY2 AMOUNT": _format_money(option_amounts.get("option_year_2")),
            "OY3 AMOUNT": _format_money(option_amounts.get("option_year_3")),
            "OY4 AMOUNT": _format_money(option_amounts.get("option_year_4")),
            "WAGE DETERMINATION NUMBER": contract.wage_determination_number or "To be provided",
            "WAGE DETERMINATION RATE": (
                f"${float(wage_rate):,.2f}" if wage_rate is not None else "To be provided"
            ),
            "SUB EMAIL": sub.business_email or "To be provided",
            "SUB OWNER TITLE": sub.owner_title or "Owner",
        },
    }
    config["missing_fields"] = detect_agreement_missing_fields(config)
    return config


def detect_agreement_missing_fields(config: dict[str, Any]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    sub = config.get("subcontractor") or {}
    contract = config.get("contract") or {}

    for key, label in [
        ("owner_name", "Sub owner / representative name"),
        ("license_number", "Sub license number"),
        ("insurance_carrier", "Sub insurance carrier"),
        ("email", "Sub email"),
        ("address", "Sub street address"),
    ]:
        if not str(sub.get(key) or "").strip():
            missing.append({"field": key, "label": label, "where": "sub_profile"})

    for key, label in [
        ("base_year_start", "Contract start date"),
        ("base_year_end", "Contract end date"),
        ("wage_determination_number", "Wage determination number"),
        ("wage_determination_rate", "Wage determination hourly rate"),
    ]:
        val = contract.get(key)
        if val is None or not str(val).strip():
            missing.append({"field": key, "label": label, "where": "contract"})

    return missing


def agreement_for_link(session: Session, link_id: int) -> SubcontractAgreement | None:
    return (
        session.query(SubcontractAgreement)
        .filter_by(contract_sub_id=link_id)
        .order_by(SubcontractAgreement.generated_at.desc())
        .first()
    )


def agreement_to_dict(row: SubcontractAgreement | None, link: ContractSub | None = None) -> dict[str, Any]:
    if not row:
        status = (
            link.agreement_signature_status
            if link and link.agreement_signature_status
            else DEFAULT_AGREEMENT_SIGNATURE_STATUS
        )
        return {
            "id": None,
            "has_agreement": False,
            "version": 0,
            "generated_at": None,
            "agreement_signature_status": status,
            "missing_fields": [],
        }
    config = row.config_json if isinstance(row.config_json, dict) else {}
    return {
        "id": row.id,
        "contract_id": row.contract_id,
        "sub_id": row.sub_id,
        "contract_sub_id": row.contract_sub_id,
        "has_agreement": True,
        "version": row.version,
        "generated_at": row.generated_at.isoformat() if row.generated_at else None,
        "agreement_signature_status": (
            link.agreement_signature_status if link else DEFAULT_AGREEMENT_SIGNATURE_STATUS
        ),
        "missing_fields": config.get("missing_fields") or [],
        "config": config,
    }


def generate_agreement(
    session: Session,
    link_id: int,
    *,
    resend: bool = False,
) -> dict[str, Any]:
    from agreement_export import build_agreement_pdf
    from api_budget import ScreenBudgetExceeded, can_screen, record_screen_usage
    from claude_client import generate_subcontract_agreement

    config = build_agreement_config(session, link_id)
    missing = config.get("missing_fields") or []
    if missing:
        labels = ", ".join(m["label"] for m in missing if isinstance(m, dict) and m.get("label"))
        raise ValueError(f"Cannot generate agreement until required fields are filled: {labels}")

    link = session.get(ContractSub, link_id)
    if not link:
        raise ValueError("Contract sub link not found")

    if not can_screen():
        raise ValueError("Claude screening budget reached — try again tomorrow.")

    contract = session.get(Contract, link.contract_id)
    if not contract:
        raise ValueError("Contract not found")

    html = generate_subcontract_agreement(contract, config)
    if not record_screen_usage():
        raise ScreenBudgetExceeded()

    pdf_bytes, engine = build_agreement_pdf(_AgreementStub(html, config))

    existing = agreement_for_link(session, link_id)
    version = (existing.version + 1) if existing else 1
    if existing:
        existing.agreement_html = html
        existing.config_json = config
        existing.pdf_bytes = pdf_bytes
        existing.version = version
        existing.date_updated = datetime.now(timezone.utc)
        row = existing
    else:
        row = SubcontractAgreement(
            contract_id=link.contract_id,
            sub_id=link.sub_id,
            contract_sub_id=link.id,
            agreement_html=html,
            config_json=config,
            pdf_bytes=pdf_bytes,
            version=version,
        )
        session.add(row)

    link.agreement_signature_status = "Agreement Sent"
    append_agreement_status_log(
        link,
        "Agreement Sent",
        note="Generated" if not resend else "Regenerated / resent",
    )
    session.commit()
    session.refresh(row)
    session.refresh(link)

    return {
        "agreement": agreement_to_dict(row, link),
        "pdf_engine": engine,
        "resend": resend,
    }


def update_agreement_signature_status(
    session: Session,
    link_id: int,
    status: str,
) -> ContractSub:
    if status not in AGREEMENT_SIGNATURE_STATUSES:
        raise ValueError(f"Invalid status. Choose one of: {', '.join(AGREEMENT_SIGNATURE_STATUSES)}")
    link = session.get(ContractSub, link_id)
    if not link:
        raise ValueError("Contract sub link not found")
    if link.agreement_signature_status != status:
        append_agreement_status_log(link, status, note="Manual status update")
    link.agreement_signature_status = status
    session.commit()
    session.refresh(link)
    return link


def update_sub_profile(session: Session, sub_id: int, payload: dict[str, Any]) -> Sub:
    row = session.get(Sub, sub_id)
    if not row:
        raise ValueError("Sub not found")
    allowed = {
        "owner_name",
        "owner_title",
        "license_number",
        "insurance_carrier",
        "business_email",
        "address",
        "city",
        "state",
        "zip",
        "phone",
        "notes",
    }
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key == "state" and value:
            value = str(value).strip().upper()[:8]
        setattr(row, key, value)
    row.date_last_updated = datetime.now(timezone.utc)
    session.commit()
    session.refresh(row)
    return row


class _AgreementStub:
    """Minimal object for agreement_export.build_agreement_pdf before DB row exists."""

    def __init__(self, html: str, config: dict[str, Any]):
        self.agreement_html = html
        self.config_json = config

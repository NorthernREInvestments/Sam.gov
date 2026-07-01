"""Post-award contract performance, invoicing, and sub payment tracking."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, joinedload

from models import Contract, ContractInvoice, SubContact, SubPayment
from performance_constants import (
    EXPECTED_PAYMENT_DAYS,
    INVOICE_OVERDUE_DAYS,
    INVOICE_STATUSES,
    INVOICING_SYSTEMS,
    PAYMENT_METHODS,
    SUB_PAYMENT_NET_DAYS,
    SUB_PAYMENT_STATUSES,
)
from settings_store import get_owner_settings

SIGNOFF_WARNING = (
    "Government sign-off not yet received. Do not release sub payment until work completion "
    "is confirmed by the COR."
)


def _today() -> date:
    return date.today()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dec(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _days_between(start: date | None, end: date | None) -> int | None:
    if not start or not end:
        return None
    return (end - start).days


def _contract_prefix(contract: Contract) -> str:
    num = (contract.government_contract_number or contract.notice_id or "CONTRACT").strip()
    return re.sub(r"[^\w\-]", "", num.upper())[:32]


def next_invoice_number(session: Session, contract: Contract) -> str:
    prefix = _contract_prefix(contract)
    count = session.query(ContractInvoice).filter_by(contract_id=contract.id).count()
    return f"{prefix}-INV-{count + 1:03d}"


def _refresh_invoice_status(row: ContractInvoice) -> None:
    today = _today()
    if row.status == "Paid":
        return
    if row.payment_received_date:
        row.status = "Paid"
        if row.invoice_submitted_date:
            row.days_to_payment = _days_between(row.invoice_submitted_date, row.payment_received_date)
        return
    if row.invoice_accepted_date and not row.payment_received_date:
        row.status = "Accepted"
    elif row.invoice_submitted_date:
        row.status = "Submitted"
        if (today - row.invoice_submitted_date).days >= INVOICE_OVERDUE_DAYS:
            row.status = "Overdue"
    else:
        row.status = "Not Started"


def _refresh_sub_payment(row: SubPayment) -> None:
    today = _today()
    if row.payment_released_date:
        row.status = "Paid"
        return
    if not row.government_signoff_received:
        row.status = "Pending Signoff"
    else:
        row.status = "Ready to Pay"
    if row.payment_due_date and today > row.payment_due_date and row.status != "Paid":
        row.status = "Overdue"


def invoice_to_dict(row: ContractInvoice) -> dict[str, Any]:
    expected_pay = None
    if row.invoice_submitted_date and not row.payment_received_date:
        expected_pay = (row.invoice_submitted_date + timedelta(days=EXPECTED_PAYMENT_DAYS)).isoformat()
    days_since = None
    if row.invoice_submitted_date and not row.payment_received_date:
        days_since = (_today() - row.invoice_submitted_date).days
    return {
        "id": row.id,
        "contract_id": row.contract_id,
        "invoice_number": row.invoice_number,
        "billing_period_start": row.billing_period_start.isoformat() if row.billing_period_start else None,
        "billing_period_end": row.billing_period_end.isoformat() if row.billing_period_end else None,
        "invoice_amount": _dec(row.invoice_amount),
        "invoice_submitted_date": row.invoice_submitted_date.isoformat() if row.invoice_submitted_date else None,
        "invoice_submission_method": row.invoice_submission_method,
        "invoice_accepted_date": row.invoice_accepted_date.isoformat() if row.invoice_accepted_date else None,
        "payment_received_date": row.payment_received_date.isoformat() if row.payment_received_date else None,
        "payment_amount": _dec(row.payment_amount),
        "days_to_payment": row.days_to_payment,
        "status": row.status,
        "notes": row.notes,
        "expected_payment_date": expected_pay,
        "days_since_submission": days_since,
        "is_overdue": row.status == "Overdue",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def sub_payment_to_dict(row: SubPayment) -> dict[str, Any]:
    warnings: list[str] = []
    if not row.government_signoff_received and row.status != "Paid":
        warnings.append(SIGNOFF_WARNING)
    if row.status == "Overdue":
        warnings.append("Sub payment due date has passed — action required.")
    sub_name = row.sub_contact.company_name if row.sub_contact else None
    return {
        "id": row.id,
        "contract_id": row.contract_id,
        "invoice_id": row.invoice_id,
        "sub_contact_id": row.sub_contact_id,
        "sub_company_name": sub_name,
        "sub_invoice_received_date": row.sub_invoice_received_date.isoformat()
        if row.sub_invoice_received_date
        else None,
        "sub_invoice_amount": _dec(row.sub_invoice_amount),
        "government_signoff_received": row.government_signoff_received,
        "government_signoff_date": row.government_signoff_date.isoformat()
        if row.government_signoff_date
        else None,
        "government_signoff_notes": row.government_signoff_notes,
        "payment_due_date": row.payment_due_date.isoformat() if row.payment_due_date else None,
        "payment_released_date": row.payment_released_date.isoformat() if row.payment_released_date else None,
        "payment_amount": _dec(row.payment_amount),
        "payment_method": row.payment_method,
        "status": row.status,
        "notes": row.notes,
        "warnings": warnings,
        "signoff_warning": SIGNOFF_WARNING if not row.government_signoff_received else None,
    }


def performance_fields_dict(contract: Contract) -> dict[str, Any]:
    pop_end = contract.period_of_performance_end
    days_to_pop_end = (pop_end - _today()).days if pop_end else None
    option_warnings: list[dict[str, str]] = []
    if pop_end and contract.option_years_remaining and contract.option_years_remaining > 0:
        if days_to_pop_end is not None and days_to_pop_end <= 30:
            option_warnings.append(
                {"level": "red", "message": "Option year decision imminent — contact CO immediately to confirm continuation."}
            )
        elif days_to_pop_end is not None and days_to_pop_end <= 90:
            option_warnings.append(
                {"level": "yellow", "message": "Option year decision approaching — follow up with CO."}
            )
    cpars_reminder = None
    if contract.status in ("completed", "option_year") or (pop_end and _today() > pop_end):
        cpars_reminder = (
            "CPARS evaluation period is open — log into cpars.gov to review and respond to your performance rating. "
            "You have 14 days to review and comment on your CPARS rating before it is finalized."
        )
    return {
        "status": contract.status,
        "award_date": contract.award_date.isoformat() if contract.award_date else None,
        "period_of_performance_start": contract.period_of_performance_start.isoformat()
        if contract.period_of_performance_start
        else None,
        "period_of_performance_end": contract.period_of_performance_end.isoformat()
        if contract.period_of_performance_end
        else None,
        "option_years_remaining": contract.option_years_remaining,
        "government_contract_number": contract.government_contract_number,
        "invoicing_system": contract.invoicing_system,
        "invoicing_system_confirmed": contract.invoicing_system_confirmed,
        "cor_name": contract.cor_name,
        "cor_email": contract.cor_email,
        "cor_phone": contract.cor_phone,
        "co_name": contract.co_name,
        "co_email": contract.co_email,
        "co_phone": contract.co_phone,
        "stop_work_issued": contract.stop_work_issued,
        "stop_work_issued_date": contract.stop_work_issued_date.isoformat()
        if contract.stop_work_issued_date
        else None,
        "cpars_rating": contract.cpars_rating,
        "cpars_comments": contract.cpars_comments,
        "cpars_expected_date": contract.cpars_expected_date.isoformat() if contract.cpars_expected_date else None,
        "cpars_reminder": cpars_reminder,
        "days_to_period_end": days_to_pop_end,
        "option_year_warnings": option_warnings,
        "amendment_alert_active": contract.amendment_alert_active,
        "amendment_alert_data": contract.amendment_alert_data or [],
        "amendments_reviewed_at": contract.amendments_reviewed_at.isoformat()
        if contract.amendments_reviewed_at
        else None,
        "amendments_last_checked_at": contract.amendments_last_checked_at.isoformat()
        if contract.amendments_last_checked_at
        else None,
    }


def overdue_invoice_alert(contract: Contract, invoices: list[ContractInvoice]) -> dict[str, Any] | None:
    for inv in invoices:
        if inv.invoice_submitted_date and not inv.payment_received_date:
            days = (_today() - inv.invoice_submitted_date).days
            if days >= INVOICE_OVERDUE_DAYS:
                return {
                    "invoice_id": inv.id,
                    "invoice_number": inv.invoice_number,
                    "days_overdue": days,
                    "submitted_date": inv.invoice_submitted_date.isoformat(),
                    "message": (
                        f"PAYMENT OVERDUE — Invoice submitted {days} days ago with no payment received. "
                        "Consider issuing a stop work notice. Contact the CO immediately."
                    ),
                }
    return None


def get_contract_performance(session: Session, notice_id: str) -> dict[str, Any]:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")

    invoices = (
        session.query(ContractInvoice)
        .filter_by(contract_id=contract.id)
        .order_by(ContractInvoice.billing_period_start.desc().nulls_last())
        .all()
    )
    for inv in invoices:
        _refresh_invoice_status(inv)
    sub_pays = (
        session.query(SubPayment)
        .options(joinedload(SubPayment.sub_contact))
        .filter_by(contract_id=contract.id)
        .order_by(SubPayment.created_at.desc())
        .all()
    )
    for sp in sub_pays:
        _refresh_sub_payment(sp)
    session.flush()

    current_month = _today().replace(day=1)
    current_inv = next(
        (
            inv
            for inv in invoices
            if inv.billing_period_start
            and inv.billing_period_start.year == current_month.year
            and inv.billing_period_start.month == current_month.month
        ),
        None,
    )
    current_sub = sub_pays[0] if sub_pays else None

    paid_days = [inv.days_to_payment for inv in invoices if inv.days_to_payment is not None]
    avg_days = round(sum(paid_days) / len(paid_days), 1) if paid_days else None

    selected_sub = (
        session.query(SubContact).filter_by(contract_id=contract.id, is_selected=True).first()
    )

    return {
        "notice_id": notice_id,
        "contract_title": contract.title,
        "performance": performance_fields_dict(contract),
        "current_invoice": invoice_to_dict(current_inv) if current_inv else None,
        "current_sub_payment": sub_payment_to_dict(current_sub) if current_sub else None,
        "selected_sub_contact_id": selected_sub.id if selected_sub else None,
        "invoice_history": [invoice_to_dict(i) for i in invoices],
        "sub_payments": [sub_payment_to_dict(s) for s in sub_pays],
        "invoice_summary": {
            "total_invoiced": _dec(sum((i.invoice_amount or 0) for i in invoices)),
            "total_received": _dec(sum((i.payment_amount or 0) for i in invoices if i.payment_received_date)),
            "avg_days_to_payment": avg_days,
            "count": len(invoices),
        },
        "payment_overdue_alert": overdue_invoice_alert(contract, invoices),
        "amendment_banner": _amendment_banner(contract),
    }


def _amendment_banner(contract: Contract) -> dict[str, Any] | None:
    if not contract.amendment_alert_active:
        return None
    items = contract.amendment_alert_data or []
    latest = items[0] if items else {}
    return {
        "message": f"AMENDMENT POSTED — New document added {latest.get('posted_date', '')}. Review before submitting.",
        "attachments": items,
    }


def update_contract_performance(session: Session, notice_id: str, payload: dict[str, Any]) -> Contract:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")

    date_fields = (
        "award_date",
        "period_of_performance_start",
        "period_of_performance_end",
        "stop_work_issued_date",
        "cpars_expected_date",
    )
    for key in date_fields:
        if key in payload:
            raw = payload[key]
            setattr(contract, key, date.fromisoformat(raw) if raw else None)

    str_fields = (
        "status",
        "government_contract_number",
        "invoicing_system",
        "cor_name",
        "cor_email",
        "cor_phone",
        "co_name",
        "co_email",
        "co_phone",
        "cpars_rating",
        "cpars_comments",
    )
    for key in str_fields:
        if key in payload:
            setattr(contract, key, payload[key] or None)

    if "option_years_remaining" in payload:
        val = payload["option_years_remaining"]
        contract.option_years_remaining = int(val) if val not in (None, "") else None
    if "invoicing_system_confirmed" in payload:
        contract.invoicing_system_confirmed = bool(payload["invoicing_system_confirmed"])
    if "stop_work_issued" in payload:
        contract.stop_work_issued = bool(payload["stop_work_issued"])
        if contract.stop_work_issued:
            contract.status = "stop_work"
            if not contract.stop_work_issued_date:
                contract.stop_work_issued_date = _today()
        elif contract.status == "stop_work":
            contract.status = "active"

    if payload.get("mark_awarded"):
        contract.status = "awarded"
        if not contract.award_date:
            contract.award_date = _today()

    return contract


def create_invoice(session: Session, notice_id: str, payload: dict[str, Any]) -> ContractInvoice:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")
    row = ContractInvoice(
        contract_id=contract.id,
        invoice_number=next_invoice_number(session, contract),
        billing_period_start=date.fromisoformat(payload["billing_period_start"])
        if payload.get("billing_period_start")
        else None,
        billing_period_end=date.fromisoformat(payload["billing_period_end"])
        if payload.get("billing_period_end")
        else None,
        invoice_amount=Decimal(str(payload["invoice_amount"])) if payload.get("invoice_amount") else None,
        invoice_submission_method=payload.get("invoice_submission_method"),
        notes=payload.get("notes"),
    )
    session.add(row)
    session.flush()
    return row


def update_invoice(session: Session, invoice_id: int, payload: dict[str, Any]) -> ContractInvoice:
    row = session.get(ContractInvoice, invoice_id)
    if not row:
        raise ValueError("Invoice not found")
    for key in ("billing_period_start", "billing_period_end", "invoice_submitted_date", "invoice_accepted_date", "payment_received_date"):
        if key in payload:
            raw = payload[key]
            setattr(row, key, date.fromisoformat(raw) if raw else None)
    if "invoice_amount" in payload:
        val = payload["invoice_amount"]
        row.invoice_amount = Decimal(str(val)) if val not in (None, "") else None
    if "payment_amount" in payload:
        val = payload["payment_amount"]
        row.payment_amount = Decimal(str(val)) if val not in (None, "") else None
    if "invoice_submission_method" in payload:
        row.invoice_submission_method = payload["invoice_submission_method"]
    if "status" in payload:
        row.status = payload["status"]
    if "notes" in payload:
        row.notes = payload["notes"]
    _refresh_invoice_status(row)
    return row


def create_sub_payment(session: Session, notice_id: str, payload: dict[str, Any]) -> SubPayment:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")
    sub_id = payload.get("sub_contact_id")
    if not sub_id:
        selected = session.query(SubContact).filter_by(contract_id=contract.id, is_selected=True).first()
        sub_id = selected.id if selected else None
    received = date.fromisoformat(payload["sub_invoice_received_date"]) if payload.get("sub_invoice_received_date") else None
    due = (received + timedelta(days=SUB_PAYMENT_NET_DAYS)) if received else None
    row = SubPayment(
        contract_id=contract.id,
        invoice_id=payload.get("invoice_id"),
        sub_contact_id=sub_id,
        sub_invoice_received_date=received,
        sub_invoice_amount=Decimal(str(payload["sub_invoice_amount"])) if payload.get("sub_invoice_amount") else None,
        payment_due_date=due,
    )
    session.add(row)
    session.flush()
    _refresh_sub_payment(row)
    return row


def update_sub_payment(session: Session, payment_id: int, payload: dict[str, Any]) -> SubPayment:
    row = (
        session.query(SubPayment)
        .options(joinedload(SubPayment.sub_contact))
        .filter_by(id=payment_id)
        .first()
    )
    if not row:
        raise ValueError("Sub payment not found")
    for key in ("sub_invoice_received_date", "government_signoff_date", "payment_released_date"):
        if key in payload:
            raw = payload[key]
            setattr(row, key, date.fromisoformat(raw) if raw else None)
    if "sub_invoice_amount" in payload:
        val = payload["sub_invoice_amount"]
        row.sub_invoice_amount = Decimal(str(val)) if val not in (None, "") else None
    if "payment_amount" in payload:
        val = payload["payment_amount"]
        row.payment_amount = Decimal(str(val)) if val not in (None, "") else None
    if "payment_method" in payload:
        row.payment_method = payload["payment_method"]
    if "government_signoff_notes" in payload:
        row.government_signoff_notes = payload["government_signoff_notes"]
    if "notes" in payload:
        row.notes = payload["notes"]
    if "government_signoff_received" in payload:
        row.government_signoff_received = bool(payload["government_signoff_received"])
        if row.government_signoff_received and not row.government_signoff_date:
            row.government_signoff_date = _today()
    if row.sub_invoice_received_date:
        row.payment_due_date = row.sub_invoice_received_date + timedelta(days=SUB_PAYMENT_NET_DAYS)
    _refresh_sub_payment(row)
    return row


def exercise_option_year(session: Session, notice_id: str) -> Contract:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")
    if not contract.option_years_remaining or contract.option_years_remaining <= 0:
        raise ValueError("No option years remaining")
    if contract.period_of_performance_end:
        contract.period_of_performance_start = contract.period_of_performance_end + timedelta(days=1)
        contract.period_of_performance_end = contract.period_of_performance_start.replace(
            year=contract.period_of_performance_start.year + 1
        ) - timedelta(days=1)
    contract.option_years_remaining -= 1
    contract.status = "option_year"
    if contract.period_of_performance_end:
        contract.cpars_expected_date = contract.period_of_performance_end + timedelta(days=45)
    return contract


def generate_stop_work_notice(session: Session, notice_id: str, invoice_id: int | None = None) -> dict[str, str]:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")
    inv = None
    if invoice_id:
        inv = session.get(ContractInvoice, invoice_id)
    if not inv:
        inv = (
            session.query(ContractInvoice)
            .filter_by(contract_id=contract.id)
            .filter(ContractInvoice.invoice_submitted_date.isnot(None))
            .filter(ContractInvoice.payment_received_date.is_(None))
            .order_by(ContractInvoice.invoice_submitted_date.asc())
            .first()
        )
    owner = get_owner_settings()
    co = contract.co_name or contract.cor_name or "[CO Name]"
    phone = owner.get("business_phone") or "[phone]"
    if not inv:
        return {"body": "No overdue invoice found.", "subject": ""}
    days = (_today() - inv.invoice_submitted_date).days if inv.invoice_submitted_date else 0
    period = f"{inv.billing_period_start} through {inv.billing_period_end}" if inv.billing_period_start else "[billing period]"
    body = f"""Dear {co},

Northern RE Investments LLC has not received payment for invoice {inv.invoice_number} submitted on {inv.invoice_submitted_date} for the period {period}. Payment is now {days} days overdue.

Pursuant to our contract obligations and cash flow requirements, we are providing notice that we may need to suspend performance if payment is not received within 5 business days.

Please advise on the status of payment and confirm the correct invoicing system and point of contact for payment inquiries.

Respectfully,
Mark Graham II
Northern RE Investments LLC
markg@northernreinvestments.com
{phone}"""
    return {"subject": f"Payment overdue — invoice {inv.invoice_number}", "body": body.strip()}


def generate_signoff_request(session: Session, notice_id: str, payment_id: int) -> dict[str, str]:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")
    pay = (
        session.query(SubPayment)
        .options(joinedload(SubPayment.sub_contact), joinedload(SubPayment.invoice))
        .filter_by(id=payment_id, contract_id=contract.id)
        .first()
    )
    if not pay:
        raise ValueError("Sub payment not found")
    owner = get_owner_settings()
    cor = contract.cor_name or "[COR Name]"
    sub_name = pay.sub_contact.company_name if pay.sub_contact else "[Sub Company Name]"
    phone = owner.get("business_phone") or "[phone]"
    start = end = "[dates]"
    if pay.invoice and pay.invoice.billing_period_start:
        start = pay.invoice.billing_period_start.isoformat()
        end = pay.invoice.billing_period_end.isoformat() if pay.invoice.billing_period_end else start
    elif pay.sub_invoice_received_date:
        start = end = pay.sub_invoice_received_date.isoformat()
    body = f"""Dear {cor},

This message confirms that {sub_name} has completed all required services for the period {start} through {end} per the Performance Work Statement requirements.

Please reply to confirm that services were completed satisfactorily so we may process payment to our service provider.

Thank you,
Mark Graham II
Northern RE Investments LLC
markg@northernreinvestments.com
{phone}"""
    return {"subject": f"Work completion confirmation — {sub_name}", "body": body.strip()}


def card_performance_alerts(session: Session, contract: Contract) -> dict[str, Any]:
    """Lightweight alerts for contract cards and list views."""
    alerts: list[dict[str, str]] = []
    payment_overdue = None
    if contract.amendment_alert_active:
        items = contract.amendment_alert_data or []
        latest = items[0] if items else {}
        alerts.append(
            {
                "type": "amendment",
                "level": "red",
                "message": (
                    f"AMENDMENT POSTED — New document added {latest.get('posted_date', '')}. "
                    "Review before submitting."
                ),
            }
        )
    active_statuses = {"active", "awarded", "option_year", "stop_work"}
    if contract.status in active_statuses:
        invs = (
            session.query(ContractInvoice)
            .filter_by(contract_id=contract.id)
            .filter(ContractInvoice.invoice_submitted_date.isnot(None))
            .filter(ContractInvoice.payment_received_date.is_(None))
            .all()
        )
        payment_overdue = overdue_invoice_alert(contract, invs)
        if payment_overdue:
            alerts.append(
                {"type": "payment_overdue", "level": "red", "message": payment_overdue["message"]}
            )
    return {"alerts": alerts, "payment_overdue_alert": payment_overdue}


def performance_dashboard(session: Session) -> dict[str, Any]:
    from performance_settings import wawf_password_status, ipp_reminder_active

    active_statuses = {"active", "awarded", "option_year", "stop_work"}
    contracts = session.query(Contract).filter(Contract.status.in_(active_statuses)).all()
    today = _today()
    month_start = today.replace(day=1)

    overdue_inv = 0
    overdue_sub = 0
    submitted_month = 0
    received_month = 0
    sub_due_month = 0
    monthly_revenue = 0.0
    amendment_alerts = 0
    contract_rows: list[dict[str, Any]] = []
    year_start = date(today.year, 1, 1)

    for c in contracts:
        if c.amendment_alert_active:
            amendment_alerts += 1
        invs = (
            session.query(ContractInvoice)
            .filter_by(contract_id=c.id)
            .order_by(ContractInvoice.billing_period_start.desc().nulls_last())
            .all()
        )
        for inv in invs:
            _refresh_invoice_status(inv)
            if inv.billing_period_start and inv.billing_period_start.year == today.year and inv.billing_period_start.month == today.month:
                if inv.invoice_amount:
                    monthly_revenue += float(inv.invoice_amount)
            if inv.invoice_submitted_date and inv.invoice_submitted_date >= month_start:
                submitted_month += 1
            if inv.payment_received_date and inv.payment_received_date >= month_start:
                received_month += 1
            if inv.status == "Overdue":
                overdue_inv += 1
        pays = session.query(SubPayment).filter_by(contract_id=c.id).all()
        for p in pays:
            _refresh_sub_payment(p)
            if p.status == "Overdue":
                overdue_sub += 1
            if p.payment_due_date and p.payment_due_date.year == today.year and p.payment_due_date.month == today.month:
                sub_due_month += 1
        alert = overdue_invoice_alert(c, invs)
        urgency = "green"
        if alert or c.stop_work_issued:
            urgency = "red"
        elif c.amendment_alert_active:
            urgency = "red"
        elif any(p.status == "Overdue" for p in pays):
            urgency = "red"
        elif any(i.status in ("Submitted", "Accepted", "Overdue") for i in invs):
            urgency = "yellow"
        current = invs[0] if invs else None
        contract_rows.append(
            {
                "notice_id": c.notice_id,
                "title": c.title,
                "status": c.status,
                "urgency": urgency,
                "current_invoice_status": current.status if current else "Not Started",
                "payment_overdue": alert is not None,
            }
        )

    session.flush()
    all_invoices = (
        session.query(ContractInvoice)
        .join(Contract)
        .filter(Contract.status.in_(active_statuses))
        .all()
    )
    all_payments = (
        session.query(SubPayment)
        .join(Contract)
        .filter(Contract.status.in_(active_statuses))
        .all()
    )
    gov_received = sum(
        float(i.payment_amount or 0)
        for i in all_invoices
        if i.payment_received_date and i.payment_received_date >= month_start
    )
    sub_released = sum(
        float(p.payment_amount or 0)
        for p in all_payments
        if p.payment_released_date and p.payment_released_date >= month_start
    )
    gov_ytd = sum(
        float(i.payment_amount or 0)
        for i in all_invoices
        if i.payment_received_date and i.payment_received_date >= year_start
    )
    sub_ytd = sum(
        float(p.payment_amount or 0)
        for p in all_payments
        if p.payment_released_date and p.payment_released_date >= year_start
    )

    wawf = wawf_password_status()
    return {
        "summary": {
            "active_contracts": len(contracts),
            "monthly_revenue_expected": round(monthly_revenue, 2),
            "invoices_submitted_this_month": submitted_month,
            "payments_received_this_month": received_month,
            "sub_payments_due_this_month": sub_due_month,
            "overdue_invoices": overdue_inv,
            "overdue_sub_payments": overdue_sub,
            "amendment_alerts": amendment_alerts,
            "wawf_warning": wawf,
            "ipp_reminder": ipp_reminder_active(),
        },
        "cash_flow": {
            "government_received_this_month": round(gov_received, 2),
            "sub_payments_released_this_month": round(sub_released, 2),
            "net_margin_this_month": round(gov_received - sub_released, 2),
            "government_received_ytd": round(gov_ytd, 2),
            "sub_payments_released_ytd": round(sub_ytd, 2),
            "net_margin_ytd": round(gov_ytd - sub_ytd, 2),
        },
        "contracts": sorted(
            contract_rows,
            key=lambda r: (0 if r["urgency"] == "red" else 1 if r["urgency"] == "yellow" else 2),
        ),
    }

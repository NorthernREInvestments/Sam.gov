"""Dashboard list and pursue actions for gt_csv_opportunities."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from csv_import_service import _is_protected_status
from models import Contract, CsvOpportunity


def _location_display(row: CsvOpportunity) -> str | None:
    parts = [row.location_city, row.location_state]
    text = ", ".join(p for p in parts if p)
    return text or None


def csv_opportunity_to_card_dict(row: CsvOpportunity, *, today: date | None = None) -> dict[str, Any]:
    """Compact-dashboard card payload for a CSV-imported opportunity."""
    from naics_labels import naics_label
    from display_format import format_service_type_display

    today = today or date.today()
    days_left = (row.due_date - today).days if row.due_date else None
    location = _location_display(row)
    service = format_service_type_display(row.naics_code, naics_label(row.naics_code))
    set_aside = (row.set_aside or "").strip() or "—"
    agency = (row.agency or "").strip() or "Federal agency"
    status = (row.status or "New").strip()
    pursuing = status.lower() == "pursuing" or bool(row.contract_id)

    if pursuing and row.contract_id:
        primary_action = {"label": "View", "action": "overview"}
        status_message = "Pursuing — pipeline running"
    elif _is_protected_status(status):
        primary_action = {"label": "View", "action": "overview"}
        status_message = f"{status} — open for details"
    else:
        primary_action = {"label": "Pursue", "action": "pursue_csv"}
        status_message = f"{agency} · {set_aside}"

    return {
        "notice_id": row.notice_id,
        "title": row.title,
        "agency": row.agency,
        "agency_display": agency,
        "location": location,
        "location_display": location or "Location pending",
        "naics_code": row.naics_code,
        "naics_label": naics_label(row.naics_code),
        "service_type_display": service,
        "set_aside": row.set_aside,
        "set_aside_display": set_aside,
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "days_until_due": days_left,
        "csv_opportunity": True,
        "csv_status": status,
        "csv_id": row.id,
        "contract_id": row.contract_id,
        "watchlist_match_confidence": row.watchlist_match_confidence,
        "pricing_history": {"kind": "set_aside", "line": f"Set-aside: {set_aside}"},
        "workflow_progress": {
            "primary_action": primary_action,
            "status_message": status_message,
        },
        "link": row.sam_url,
    }


def list_csv_opportunity_cards(session: Session) -> list[dict[str, Any]]:
    """All imported CSV opportunities, soonest deadline first."""
    today = date.today()
    rows = (
        session.query(CsvOpportunity)
        .order_by(CsvOpportunity.due_date.asc().nullslast(), CsvOpportunity.id.asc())
        .all()
    )
    return [csv_opportunity_to_card_dict(row, today=today) for row in rows]


def pursue_csv_opportunity(session: Session, notice_id: str) -> dict[str, Any]:
    """
    Mark a CSV row as Pursuing, bridge to gt_contracts, queue attachments,
    and start the full intake pipeline (attachments → analysis → subs → proposal path).
    """
    from csv_attachment_queue_service import enqueue_csv_attachments, process_attachment_queue
    from csv_watchlist_service import bridge_csv_to_contract
    from watchlist_sync import start_watchlist_priority_pipeline

    notice_id = str(notice_id or "").strip()
    if not notice_id:
        return {"ok": False, "error": "invalid_notice_id"}

    row = session.query(CsvOpportunity).filter_by(notice_id=notice_id).first()
    if not row:
        return {"ok": False, "error": "not_found"}

    if row.status == "Pursuing" or row.contract_id:
        contract = bridge_csv_to_contract(session, row)
        session.commit()
        pipeline = start_watchlist_priority_pipeline([notice_id])
        return {
            "ok": True,
            "notice_id": notice_id,
            "contract_id": contract.id,
            "csv_status": row.status,
            "already_pursuing": True,
            "pipeline_started": bool(pipeline.get("started")),
        }

    if _is_protected_status(row.status):
        contract = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not contract and row.contract_id:
            contract = session.get(Contract, row.contract_id)
        return {
            "ok": True,
            "notice_id": notice_id,
            "contract_id": contract.id if contract else row.contract_id,
            "csv_status": row.status,
            "already_pursuing": True,
            "pipeline_started": False,
        }

    row.status = "Pursuing"
    contract = bridge_csv_to_contract(session, row)
    if contract.status in (None, "new", "skipped"):
        contract.status = "reviewing"

    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
    analysis.setdefault("csv_import", {})
    analysis["csv_import"] = {
        **(analysis.get("csv_import") or {}),
        "pursued_manually": True,
        "source": "gt_csv_opportunities",
    }
    contract.analysis = analysis

    enqueue_csv_attachments(session, notice_ids=[notice_id], watchlist_notice_ids=set())
    session.flush()
    attachment_summary = process_attachment_queue(session)
    session.commit()

    pipeline = start_watchlist_priority_pipeline([notice_id])
    return {
        "ok": True,
        "notice_id": notice_id,
        "contract_id": contract.id,
        "csv_status": row.status,
        "attachments_queued": attachment_summary.get("attachments_queued", 0),
        "attachments_completed": attachment_summary.get("completed", 0),
        "pipeline_started": bool(pipeline.get("started")),
    }

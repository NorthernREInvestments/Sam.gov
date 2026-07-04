"""Dashboard list and pursue actions for gt_csv_opportunities."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from csv_import_service import _is_protected_status
from models import Contract, CsvOpportunity


def _location_display(row: CsvOpportunity) -> str | None:
    parts = [row.location_city, row.location_state]
    text = ", ".join(p for p in parts if p)
    return text or None


def _pursued_at_from_contract(contract: Contract | None) -> str | None:
    if not contract:
        return None
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    csv_meta = analysis.get("csv_import") if isinstance(analysis.get("csv_import"), dict) else {}
    raw = csv_meta.get("pursued_at")
    return str(raw) if raw else None


def csv_opportunity_to_card_dict(
    row: CsvOpportunity,
    *,
    today: date | None = None,
    contract: Contract | None = None,
) -> dict[str, Any]:
    """Dashboard card payload for a CSV-imported opportunity."""
    from naics_labels import naics_label
    from display_format import format_service_type_display

    today = today or date.today()
    days_left = (row.due_date - today).days if row.due_date else None
    location = _location_display(row)
    service = format_service_type_display(row.naics_code, naics_label(row.naics_code))
    set_aside = (row.set_aside or "").strip() or "—"
    agency = (row.agency or "").strip() or "Federal agency"
    status = (row.status or "New").strip()
    confidence = (row.watchlist_match_confidence or "").strip()
    pursuing = status.lower() == "pursuing" or bool(row.contract_id)
    pursued_at = _pursued_at_from_contract(contract)

    if pursuing:
        primary_action = {"label": "View pipeline", "action": "overview"}
    elif _is_protected_status(status) and status.lower() != "new":
        primary_action = {"label": "View", "action": "overview"}
    else:
        primary_action = {"label": "Pursue", "action": "pursue_csv"}

    sam_url = (row.sam_url or "").strip()
    if not sam_url and row.notice_id:
        sam_url = f"https://sam.gov/opp/{row.notice_id}/view"

    return {
        "notice_id": row.notice_id,
        "title": row.title,
        "agency": row.agency,
        "agency_display": agency,
        "location": location,
        "location_city": row.location_city,
        "location_state": row.location_state,
        "location_display": location or "Location pending",
        "naics_code": row.naics_code,
        "naics_label": naics_label(row.naics_code),
        "service_type_display": service,
        "set_aside": row.set_aside,
        "set_aside_display": set_aside,
        "co_name": row.co_name,
        "co_email": row.co_email,
        "co_phone": row.co_phone,
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "days_until_due": days_left,
        "csv_opportunity": True,
        "csv_status": status,
        "csv_id": row.id,
        "contract_id": row.contract_id,
        "watchlist_match_confidence": confidence or None,
        "watchlist_match_score": row.watchlist_match_score,
        "sam_url": sam_url,
        "link": sam_url,
        "pursued_at": pursued_at,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "workflow_progress": {"primary_action": primary_action},
    }


def _days_bucket(days: int | None) -> str:
    if days is None:
        return "unknown"
    if days < 7:
        return "under_7"
    if days <= 14:
        return "7_14"
    if days <= 30:
        return "14_30"
    return "30_plus"


def _matches_filters(
    card: dict[str, Any],
    *,
    state: str | None,
    days_bucket: str | None,
    naics_code: str | None,
    keyword: str | None,
) -> bool:
    if state and (card.get("location_state") or "").upper() != state.upper():
        return False
    if naics_code and card.get("naics_code") != naics_code:
        return False
    if days_bucket and days_bucket != "all":
        if _days_bucket(card.get("days_until_due")) != days_bucket:
            return False
    if keyword:
        hay = (card.get("title") or "").lower()
        if keyword.lower() not in hay:
            return False
    return True


def _sort_csv_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pursuing (most recently pursued first), then soonest due date."""

    def _ts(value: str | None) -> float:
        if not value:
            return 0.0
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    def sort_key(card: dict[str, Any]) -> tuple:
        status = (card.get("csv_status") or "New").lower()
        pursuing = status == "pursuing"
        due = card.get("due_date") or "9999-12-31"
        pursued_ts = _ts(card.get("pursued_at") or card.get("updated_at"))
        return (
            0 if pursuing else 1,
            -pursued_ts if pursuing else 0,
            due,
            card.get("notice_id") or "",
        )

    return sorted(cards, key=sort_key)


def csv_opportunity_filter_options(session: Session) -> dict[str, Any]:
    rows = session.query(CsvOpportunity).all()
    states = sorted({str(r.location_state).strip().upper() for r in rows if r.location_state})
    naics_codes = sorted({str(r.naics_code).strip() for r in rows if r.naics_code})
    return {"states": states, "naics_codes": naics_codes}


def list_csv_opportunity_cards(
    session: Session,
    *,
    state: str | None = None,
    days_bucket: str | None = None,
    naics_code: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """All imported CSV opportunities with optional filters."""
    today = date.today()
    rows = session.query(CsvOpportunity).order_by(CsvOpportunity.due_date.asc().nullslast()).all()
    contract_by_notice = {
        c.notice_id: c
        for c in session.query(Contract).filter(
            Contract.notice_id.in_([r.notice_id for r in rows])
        ).all()
    } if rows else {}

    cards = [
        csv_opportunity_to_card_dict(row, today=today, contract=contract_by_notice.get(row.notice_id))
        for row in rows
    ]
    keyword = (keyword or "").strip() or None
    state = (state or "").strip().upper() or None
    naics_code = (naics_code or "").strip() or None
    days_bucket = (days_bucket or "").strip().lower() or None
    if days_bucket == "all":
        days_bucket = None

    filtered = [
        card
        for card in cards
        if _matches_filters(
            card,
            state=state,
            days_bucket=days_bucket,
            naics_code=naics_code,
            keyword=keyword,
        )
    ]
    sorted_cards = _sort_csv_cards(filtered)
    return {
        "count": len(sorted_cards),
        "total_count": len(cards),
        "filter_options": csv_opportunity_filter_options(session),
        "opportunities": sorted_cards,
    }


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

    now_iso = datetime.now(timezone.utc).isoformat()

    if row.status == "Pursuing" or row.contract_id:
        contract = bridge_csv_to_contract(session, row)
        analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
        csv_meta = dict(analysis.get("csv_import") or {})
        csv_meta["pursued_at"] = csv_meta.get("pursued_at") or now_iso
        csv_meta["source"] = "gt_csv_opportunities"
        analysis["csv_import"] = csv_meta
        contract.analysis = analysis
        session.commit()
        pipeline = start_watchlist_priority_pipeline([notice_id])
        return {
            "ok": True,
            "notice_id": notice_id,
            "contract_id": contract.id,
            "csv_status": row.status,
            "pursued_at": csv_meta["pursued_at"],
            "already_pursuing": True,
            "pipeline_started": bool(pipeline.get("started")),
        }

    if _is_protected_status(row.status) and row.status.lower() not in ("new", "reviewing"):
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
    analysis["csv_import"] = {
        **(analysis.get("csv_import") or {}),
        "pursued_manually": True,
        "pursued_at": now_iso,
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
        "pursued_at": now_iso,
        "attachments_queued": attachment_summary.get("attachments_queued", 0),
        "attachments_completed": attachment_summary.get("completed", 0),
        "pipeline_started": bool(pipeline.get("started")),
    }

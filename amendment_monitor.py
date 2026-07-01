"""SAM.gov amendment monitoring for active contracts."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from models import Contract
from performance_constants import AMENDMENT_MONITOR_STATUSES

logger = logging.getLogger("govtracker.amendments")


def _known_attachment_keys(contract: Contract) -> set[str]:
    keys: set[str] = set()
    sam = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    for att in (sam.get("opportunityAttachments") or []) + (sam.get("pieeAttachments") or []):
        if not isinstance(att, dict):
            continue
        key = att.get("attachment_id") or att.get("resource_id") or att.get("description") or att.get("url")
        if key:
            keys.add(str(key))
    for item in contract.amendment_alert_data or []:
        if isinstance(item, dict) and item.get("key"):
            keys.add(str(item["key"]))
    return keys


def _attachment_key(att: dict[str, Any]) -> str:
    return str(att.get("attachment_id") or att.get("resource_id") or att.get("description") or att.get("url") or "")


def should_monitor(contract: Contract) -> bool:
    if not contract.amendment_monitoring_active:
        return False
    if contract.status not in AMENDMENT_MONITOR_STATUSES:
        return False
    if contract.due_date and contract.due_date < date.today() and contract.status in ("bidding", "submitted"):
        return False
    return True


def check_contract_amendments(session: Session, contract: Contract) -> bool:
    """Return True if new amendments detected."""
    if not should_monitor(contract):
        contract.amendment_monitoring_active = False
        return False

    from sam_enrich import fetch_opportunity_attachments, refresh_opportunity_attachments

    notice_id = contract.notice_id
    known = _known_attachment_keys(contract)
    fresh = fetch_opportunity_attachments(notice_id)
    if fresh is None:
        contract.amendments_last_checked_at = datetime.now(timezone.utc)
        return False

    new_items: list[dict[str, Any]] = []
    for att in fresh:
        key = _attachment_key(att)
        if not key or key in known:
            continue
        new_items.append(
            {
                "key": key,
                "description": att.get("description"),
                "posted_date": att.get("posted_date"),
                "type": att.get("type"),
                "url": att.get("url") or att.get("download_url"),
            }
        )

    contract.amendments_last_checked_at = datetime.now(timezone.utc)

    if new_items:
        existing = list(contract.amendment_alert_data or [])
        existing = new_items + existing
        contract.amendment_alert_data = existing[:20]
        contract.amendment_alert_active = True
        contract.amendments_reviewed_at = None
        raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
        if raw.get("noticeId"):
            try:
                contract.sam_raw = refresh_opportunity_attachments(raw)
            except Exception:
                logger.exception("Failed to refresh sam_raw for %s", notice_id)
        return True

    if contract.due_date and contract.due_date < date.today():
        contract.amendment_monitoring_active = False
    return False


def check_all_amendments(session: Session) -> dict[str, int]:
    rows = session.query(Contract).all()
    checked = 0
    found = 0
    for row in rows:
        if not should_monitor(row):
            continue
        last = row.amendments_last_checked_at
        if last:
            hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            if hours < 24:
                continue
        checked += 1
        if check_contract_amendments(session, row):
            found += 1
    session.commit()
    return {"checked": checked, "amendments_found": found}


def dismiss_amendment_alert(session: Session, notice_id: str) -> Contract:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")
    contract.amendment_alert_active = False
    contract.amendments_reviewed_at = datetime.now(timezone.utc)
    return contract

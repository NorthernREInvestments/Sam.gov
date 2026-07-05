"""Restrict SAM.gov attachment API usage to CSV-imported opportunities when enabled."""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from sqlalchemy.orm import Session


def sam_attachments_csv_only_from() -> date | None:
    raw = os.getenv("SAM_ATTACHMENTS_CSV_ONLY_FROM", "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def sam_attachments_csv_only_until() -> date | None:
    """Last calendar day (inclusive) for CSV-only attachment mode."""
    raw = os.getenv("SAM_ATTACHMENTS_CSV_ONLY_UNTIL", "2026-07-10").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def sam_attachments_csv_only() -> bool:
    """When True, SAM attachment API calls are limited to gt_csv_opportunities rows."""
    until = sam_attachments_csv_only_until()
    today = date.today()
    if until is not None and today > until:
        return False
    start = sam_attachments_csv_only_from()
    if start is not None and today < start:
        return False
    raw = os.getenv("SAM_ATTACHMENTS_CSV_ONLY", "true").strip().lower()
    return raw not in ("0", "false", "no")


def sam_attachments_csv_only_snapshot() -> dict[str, Any]:
    return {
        "sam_attachments_csv_only": sam_attachments_csv_only(),
        "sam_attachments_csv_only_from": (
            sam_attachments_csv_only_from().isoformat()
            if sam_attachments_csv_only_from()
            else None
        ),
        "sam_attachments_csv_only_until": (
            sam_attachments_csv_only_until().isoformat()
            if sam_attachments_csv_only_until()
            else None
        ),
        "csv_auto_sam_attachments_on_import": csv_auto_sam_attachments_on_import(),
    }


def csv_auto_sam_attachments_on_import() -> bool:
    """When False (default), CSV upload/pricing never calls SAM — only Pursue does."""
    raw = os.getenv("CSV_AUTO_SAM_ATTACHMENTS", "false").strip().lower()
    return raw in ("1", "true", "yes")


def notice_id_csv_attachment_eligible(session: Session, notice_id: str) -> bool:
    if not sam_attachments_csv_only():
        return True
    if not notice_id:
        return False
    from models import CsvOpportunity

    return (
        session.query(CsvOpportunity.id)
        .filter(CsvOpportunity.notice_id == notice_id)
        .first()
        is not None
    )


def csv_notice_id_set(session: Session) -> set[str]:
    from models import CsvOpportunity

    rows = session.query(CsvOpportunity.notice_id).all()
    return {row[0] for row in rows if row[0]}


def filter_contracts_csv_eligible(session: Session, contracts: list[Any]) -> list[Any]:
    if not sam_attachments_csv_only():
        return list(contracts)
    csv_ids = csv_notice_id_set(session)
    return [row for row in contracts if row.notice_id in csv_ids]

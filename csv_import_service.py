"""Import SAM.gov ContractOpportunitiesFullCSV into gt_csv_opportunities."""

from __future__ import annotations

import csv
import io
import logging
import os
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from csv_upload_constants import CSV_COLUMN_MAP, PROTECTED_CSV_STATUSES
from models import CsvOpportunity
from settings_store import get_naics_codes

logger = logging.getLogger("govtracker.csv_import")

SAM_CSV_RESERVE_CALLS = int(os.getenv("CSV_UPLOAD_SAM_RESERVE", "2"))
CSV_UPLOAD_MAX_MB_DEFAULT = 250


def csv_upload_max_bytes() -> int:
    """Max SAM full CSV upload size (default 250 MB)."""
    raw = os.getenv("CSV_UPLOAD_MAX_MB", str(CSV_UPLOAD_MAX_MB_DEFAULT)).strip()
    try:
        mb = max(1, min(500, int(raw)))
    except ValueError:
        mb = CSV_UPLOAD_MAX_MB_DEFAULT
    return mb * 1024 * 1024


def verify_csv_upload_password(password: str) -> bool:
    import hmac

    expected = os.getenv("CSV_UPLOAD_PASSWORD", "").strip() or os.getenv("APP_PASSWORD", "").strip()
    if not expected:
        return False
    return hmac.compare_digest(password, expected)


def _normalize_status(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "New"
    for protected in PROTECTED_CSV_STATUSES:
        if text.lower() == protected.lower():
            return protected
    return text[:32]


def _is_protected_status(status: str | None) -> bool:
    text = str(status or "").strip()
    return any(text.lower() == s.lower() for s in PROTECTED_CSV_STATUSES)


def _parse_due_date(value: str | None) -> date | None:
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    if "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _row_get(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _set_aside_text(row: dict[str, str]) -> str:
    return _row_get(row, "SetASideCode", "SetASide", "set_aside_code", "set_aside")


def _passes_set_aside_filter(row: dict[str, str]) -> bool:
    blob = _set_aside_text(row).lower()
    return "sba" in blob or "total small business" in blob


def _passes_import_filters(
    row: dict[str, str],
    *,
    today: date | None = None,
    naics_set: set[str] | None = None,
) -> bool:
    today = today or date.today()
    active = _row_get(row, "Active", "active").lower()
    if active not in ("yes", "y", "true", "1"):
        return False
    naics = _row_get(row, "NaicsCode", "naics_code")
    allowed = naics_set if naics_set is not None else set(get_naics_codes())
    if naics not in allowed:
        return False
    if not _passes_set_aside_filter(row):
        return False
    due = _parse_due_date(_row_get(row, "ResponseDeadLine", "due_date"))
    if due is None or due < today:
        return False
    notice_id = _row_get(row, "NoticeId", "notice_id")
    if not notice_id:
        return False
    return True


def _map_csv_row(row: dict[str, str]) -> dict[str, Any]:
    notice_id = _row_get(row, "NoticeId", "notice_id")
    city = _row_get(row, "PopCity", "location_city")
    state = _row_get(row, "PopState", "location_state")
    return {
        "notice_id": notice_id,
        "title": _row_get(row, "Title", "title") or notice_id,
        "solicitation_number": _row_get(row, "Sol#", "solicitation_number") or None,
        "agency": _row_get(row, "Department/Ind.Agency", "agency") or None,
        "contracting_office": _row_get(row, "Office", "contracting_office") or None,
        "due_date": _parse_due_date(_row_get(row, "ResponseDeadLine", "due_date")),
        "naics_code": _row_get(row, "NaicsCode", "naics_code") or None,
        "set_aside": _set_aside_text(row) or None,
        "location_city": city or None,
        "location_state": state or None,
        "co_name": _row_get(row, "PrimaryContactFullname", "co_name") or None,
        "co_email": _row_get(row, "PrimaryContactEmail", "co_email") or None,
        "co_phone": _row_get(row, "PrimaryContactPhone", "co_phone") or None,
        "sam_url": _row_get(row, "Link", "sam_url") or None,
        "description": _row_get(row, "Description", "description") or None,
        "status": "New",
    }


def parse_sam_csv(content: bytes | str) -> list[dict[str, str]]:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    return [dict(row) for row in reader]


def clear_non_protected_csv_rows(session: Session) -> int:
    """Delete gt_csv_opportunities rows whose status is not actively protected."""
    protected = {s.lower() for s in PROTECTED_CSV_STATUSES}
    deleted = 0
    for row in session.query(CsvOpportunity).yield_per(500):
        if (row.status or "").strip().lower() in protected:
            continue
        session.delete(row)
        deleted += 1
    if deleted:
        session.flush()
    return deleted


def import_csv_opportunities(session: Session, csv_rows: list[dict[str, str]]) -> dict[str, Any]:
    """
    Filter, dedupe, and upsert into gt_csv_opportunities.
    Returns counts for the import summary (does not run queue or watchlist).
    """
    today = date.today()
    summary: dict[str, Any] = {
        "records_imported": 0,
        "records_updated": 0,
        "records_skipped_filters": 0,
        "records_protected_skipped": 0,
        "records_deleted_before_import": 0,
        "new_notice_ids": [],
        "imported_notice_ids": [],
    }

    from csv_attachment_queue_service import clear_pending_queue_for_deleted_csv

    clear_pending_queue_for_deleted_csv(session)
    summary["records_deleted_before_import"] = clear_non_protected_csv_rows(session)

    existing_by_notice: dict[str, CsvOpportunity] = {
        row.notice_id: row for row in session.query(CsvOpportunity).all()
    }

    for raw in csv_rows:
        _import_one_csv_row(session, raw, today=today, summary=summary, existing_by_notice=existing_by_notice)

    session.flush()
    return summary


def _import_one_csv_row(
    session: Session,
    raw: dict[str, str],
    *,
    today: date,
    summary: dict[str, Any],
    existing_by_notice: dict[str, CsvOpportunity],
    naics_set: set[str] | None = None,
) -> None:
    if not _passes_import_filters(raw, today=today, naics_set=naics_set):
        summary["records_skipped_filters"] += 1
        return

    mapped = _map_csv_row(raw)
    notice_id = mapped["notice_id"]
    existing = existing_by_notice.get(notice_id)
    if existing and _is_protected_status(existing.status):
        summary["records_protected_skipped"] += 1
        return

    if existing:
        for key, val in mapped.items():
            if key == "status":
                continue
            setattr(existing, key, val)
        existing.status = existing.status if _is_protected_status(existing.status) else "New"
        summary["records_updated"] += 1
        summary["imported_notice_ids"].append(notice_id)
    else:
        row = CsvOpportunity(**mapped)
        session.add(row)
        existing_by_notice[notice_id] = row
        summary["records_imported"] += 1
        summary["new_notice_ids"].append(notice_id)
        summary["imported_notice_ids"].append(notice_id)


def import_csv_opportunities_from_content(
    session: Session,
    content: bytes | str,
    *,
    progress: Any | None = None,
) -> dict[str, Any]:
    """Stream-parse CSV content row-by-row (lower memory than loading all rows)."""
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    if not text.strip():
        return {"ok": False, "error": "empty_or_invalid_csv"}

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {"ok": False, "error": "empty_or_invalid_csv"}

    today = date.today()
    summary: dict[str, Any] = {
        "records_imported": 0,
        "records_updated": 0,
        "records_skipped_filters": 0,
        "records_protected_skipped": 0,
        "records_deleted_before_import": 0,
        "new_notice_ids": [],
        "imported_notice_ids": [],
    }

    from csv_attachment_queue_service import clear_pending_queue_for_deleted_csv

    if progress:
        progress("prepare", "Preparing import (clearing previous CSV rows)…", rows_scanned=0, rows_imported=0)

    clear_pending_queue_for_deleted_csv(session)
    summary["records_deleted_before_import"] = clear_non_protected_csv_rows(session)

    if progress:
        progress("prepare", "Loading existing opportunities for dedupe…", rows_scanned=0, rows_imported=0)

    existing_by_notice: dict[str, CsvOpportunity] = {}
    for row in session.query(CsvOpportunity).yield_per(500):
        existing_by_notice[row.notice_id] = row

    naics_set = set(get_naics_codes())

    if progress:
        progress("import", "Scanning CSV rows…", rows_scanned=0, rows_imported=0)

    rows_scanned = 0
    for raw in reader:
        rows_scanned += 1
        _import_one_csv_row(
            session,
            dict(raw),
            today=today,
            summary=summary,
            existing_by_notice=existing_by_notice,
            naics_set=naics_set,
        )
        if rows_scanned % 250 == 0:
            session.flush()
        if progress and rows_scanned % 2000 == 0:
            imported = summary["records_imported"] + summary["records_updated"]
            progress(
                "import",
                f"Scanning CSV… {rows_scanned:,} rows read, {imported:,} matched filters so far",
                rows_scanned=rows_scanned,
                rows_imported=imported,
            )

    session.flush()
    if progress:
        imported = summary["records_imported"] + summary["records_updated"]
        progress(
            "import",
            f"CSV scan complete — {rows_scanned:,} rows read, {imported:,} matched filters",
            rows_scanned=rows_scanned,
            rows_imported=imported,
        )
    summary["ok"] = True
    return summary


def csv_opportunity_to_posting_dict(row: CsvOpportunity) -> dict[str, Any]:
    location = ", ".join(filter(None, [row.location_city, row.location_state]))
    raw = dict(row.sam_raw) if isinstance(row.sam_raw, dict) else {}
    if row.notice_id and not raw.get("noticeId"):
        raw["noticeId"] = row.notice_id
    if row.contracting_office and not raw.get("officeAddress"):
        raw["officeAddress"] = {"city": row.location_city, "state": row.location_state}
    return {
        "notice_id": row.notice_id,
        "title": row.title,
        "agency": row.agency,
        "location": location or None,
        "naics_code": row.naics_code,
        "description": row.description,
        "sam_raw": raw,
    }

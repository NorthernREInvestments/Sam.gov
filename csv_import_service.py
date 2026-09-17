"""Import SAM.gov ContractOpportunitiesFullCSV into gt_csv_opportunities."""

from __future__ import annotations
from application_clock import now_utc, today_local

import csv
import io
import logging
import os
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import exists, func
from sqlalchemy.orm import Session

from csv_upload_constants import CSV_COLUMN_MAP, PROTECTED_CSV_STATUSES
from models import Contract, CsvOpportunity
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


def _parse_posted_datetime(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%m/%d/%Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    if "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    return None


def _parse_amendment_number(row: dict[str, str], title: str | None = None) -> int | None:
    for key in (
        "AmendmentNumber",
        "Amendment Number",
        "Amendment",
        "ModificationNumber",
        "Modification Number",
    ):
        raw = _row_get(row, key)
        if raw:
            match = re.search(r"(\d+)", raw)
            if match:
                return int(match.group(1))
    title_text = (title or _row_get(row, "Title", "title") or "").strip()
    if title_text:
        match = re.search(r"(?:amendment|amend|mod(?:ification)?)\s*[#:]?\s*(\d+)", title_text, re.I)
        if match:
            return int(match.group(1))
    return None


def _csv_sam_meta_from_row(row: dict[str, str], mapped: dict[str, Any]) -> dict[str, Any]:
    posted = _parse_posted_datetime(
        _row_get(row, "PostedDate", "Posted Date", "postedDate", "LastModifiedDate", "Last Modified Date")
    )
    notice_type = _row_get(row, "Type", "Notice Type", "Contract Opportunity Type", "notice_type") or None
    amendment_number = _parse_amendment_number(row, mapped.get("title"))
    meta: dict[str, Any] = {
        "posted_date": posted.isoformat() if posted else None,
        "notice_type": notice_type,
        "amendment_number": amendment_number,
        "merged_notice_ids": [mapped["notice_id"]],
        "merged_notice_count": 1,
    }
    return meta


def _csv_amendment_rank(
    *,
    posted_date: str | None = None,
    amendment_number: int | None = None,
    due_date: date | None = None,
    import_sequence: int = 0,
) -> tuple:
    posted_ts = 0.0
    if posted_date:
        try:
            posted_ts = datetime.fromisoformat(str(posted_date).replace("Z", "+00:00")).timestamp()
        except ValueError:
            posted_ts = 0.0
    due_ord = due_date.toordinal() if due_date else 0
    return (posted_ts, amendment_number or 0, due_ord, import_sequence)


def _csv_rank_from_mapped(mapped: dict[str, Any], import_sequence: int = 0) -> tuple:
    sam_raw = mapped.get("sam_raw") if isinstance(mapped.get("sam_raw"), dict) else {}
    return _csv_amendment_rank(
        posted_date=sam_raw.get("posted_date"),
        amendment_number=sam_raw.get("amendment_number"),
        due_date=mapped.get("due_date"),
        import_sequence=import_sequence,
    )


def _csv_rank_from_opportunity(row: CsvOpportunity) -> tuple:
    sam_raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
    return _csv_amendment_rank(
        posted_date=sam_raw.get("posted_date"),
        amendment_number=sam_raw.get("amendment_number"),
        due_date=row.due_date,
        import_sequence=int(sam_raw.get("import_sequence") or 0),
    )


def _merge_csv_sam_meta(existing_meta: dict[str, Any], incoming_meta: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing_meta or {})
    incoming = dict(incoming_meta or {})
    notice_ids = list(merged.get("merged_notice_ids") or [])
    for notice_id in incoming.get("merged_notice_ids") or []:
        if notice_id and notice_id not in notice_ids:
            notice_ids.append(notice_id)
    merged["merged_notice_ids"] = notice_ids
    merged["merged_notice_count"] = len(notice_ids)
    existing_rank = _csv_amendment_rank(
        posted_date=merged.get("posted_date"),
        amendment_number=merged.get("amendment_number"),
        due_date=None,
        import_sequence=int(merged.get("import_sequence") or 0),
    )
    incoming_rank = _csv_amendment_rank(
        posted_date=incoming.get("posted_date"),
        amendment_number=incoming.get("amendment_number"),
        due_date=None,
        import_sequence=int(incoming.get("import_sequence") or 0),
    )
    if incoming_rank >= existing_rank:
        for key in ("posted_date", "notice_type", "amendment_number", "import_sequence"):
            if incoming.get(key) is not None:
                merged[key] = incoming[key]
    return merged


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
    today = today or today_local()
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


def _map_csv_row(row: dict[str, str], *, import_sequence: int = 0) -> dict[str, Any]:
    notice_id = _row_get(row, "NoticeId", "notice_id")
    city = _row_get(row, "PopCity", "location_city")
    state = _row_get(row, "PopState", "location_state")
    mapped = {
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
    sam_meta = _csv_sam_meta_from_row(row, mapped)
    sam_meta["import_sequence"] = import_sequence
    mapped["sam_raw"] = sam_meta
    return mapped


def _csv_dict_reader(content: bytes | str) -> csv.DictReader:
    """Stream CSV rows without decoding the entire file into a second string."""
    if isinstance(content, bytes):
        stream = io.TextIOWrapper(io.BytesIO(content), encoding="utf-8-sig", errors="replace")
    else:
        stream = io.StringIO(content)
    return csv.DictReader(stream)


def parse_sam_csv(content: bytes | str) -> list[dict[str, str]]:
    reader = _csv_dict_reader(content)
    if not reader.fieldnames:
        return []
    return [dict(row) for row in reader]


def _normalize_solicitation_number(value: str | None) -> str | None:
    """Normalize Sol# for deduplication (SAM publishes multiple NoticeIds per solicitation)."""
    text = str(value or "").strip().upper()
    if not text:
        return None
    return "".join(text.split())


def _csv_row_keep_score(row: CsvOpportunity) -> tuple:
    """Higher = prefer keeping this row when deduping by solicitation number."""
    confidence = (row.watchlist_match_confidence or "").strip().lower()
    watchlist_rank = 2 if confidence == "high" else 1 if confidence == "possible" else 0
    amendment_rank = _csv_rank_from_opportunity(row)
    return (
        1 if _is_protected_status(row.status) else 0,
        1 if row.contract_id else 0,
        watchlist_rank,
        1 if row.pricing_intel else 0,
        amendment_rank,
        row.id or 0,
    )


def _can_delete_csv_duplicate(row: CsvOpportunity) -> bool:
    return not _is_protected_status(row.status) and row.contract_id is None


class _ExistingOpportunityCache:
    """Look up CSV rows by notice_id or solicitation number during import."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._by_notice: dict[str, CsvOpportunity | None] = {}
        self._by_sol: dict[str, CsvOpportunity] = {}
        self._sol_index_built = False

    def _ensure_sol_index(self) -> None:
        if self._sol_index_built:
            return
        for row in self._session.query(CsvOpportunity).all():
            self._index_row(row)
        self._sol_index_built = True

    def _index_row(self, row: CsvOpportunity) -> None:
        self._by_notice[row.notice_id] = row
        sol = _normalize_solicitation_number(row.solicitation_number)
        if not sol:
            return
        current = self._by_sol.get(sol)
        if current is None or _csv_row_keep_score(row) >= _csv_row_keep_score(current):
            self._by_sol[sol] = row

    def get(self, notice_id: str) -> CsvOpportunity | None:
        if notice_id in self._by_notice:
            return self._by_notice[notice_id]
        row = self._session.query(CsvOpportunity).filter_by(notice_id=notice_id).first()
        if row:
            self._index_row(row)
        else:
            self._by_notice[notice_id] = None
        return row

    def get_by_solicitation(self, solicitation_number: str | None) -> CsvOpportunity | None:
        sol = _normalize_solicitation_number(solicitation_number)
        if not sol:
            return None
        self._ensure_sol_index()
        return self._by_sol.get(sol)

    def remember(self, row: CsvOpportunity) -> None:
        self._index_row(row)

    def forget(self, notice_id: str) -> None:
        self._by_notice.pop(notice_id, None)


def _repoint_csv_notice_id(
    session: Session,
    row: CsvOpportunity,
    new_notice_id: str,
    cache: _ExistingOpportunityCache,
) -> None:
    """Point a CSV row at the latest SAM notice id for the same solicitation."""
    old_notice_id = row.notice_id
    if old_notice_id == new_notice_id:
        return
    collision = cache.get(new_notice_id)
    if collision is not None and collision.id != row.id:
        if _can_delete_csv_duplicate(collision):
            session.delete(collision)
            cache.forget(new_notice_id)
        else:
            return
    row.notice_id = new_notice_id
    cache.forget(old_notice_id)
    cache.remember(row)
    from models import AttachmentQueueItem

    session.query(AttachmentQueueItem).filter_by(csv_opportunity_id=row.id).update(
        {AttachmentQueueItem.notice_id: new_notice_id},
        synchronize_session=False,
    )


def clear_non_protected_csv_rows(session: Session) -> int:
    """Delete all non-protected CSV rows not linked to dashboard contracts (legacy full replace)."""
    protected_lower = [status.lower() for status in PROTECTED_CSV_STATUSES]
    linked_to_dashboard = exists().where(Contract.notice_id == CsvOpportunity.notice_id)
    deleted = (
        session.query(CsvOpportunity)
        .filter(func.lower(func.coalesce(CsvOpportunity.status, "")).notin_(protected_lower))
        .filter(CsvOpportunity.contract_id.is_(None))
        .filter(~linked_to_dashboard)
        .delete(synchronize_session=False)
    )
    if deleted:
        session.flush()
    return deleted


PRICING_AFFECTING_FIELDS = frozenset(
    {
        "title",
        "agency",
        "naics_code",
        "location_city",
        "location_state",
        "description",
        "due_date",
        "set_aside",
        "solicitation_number",
    }
)


def _normalize_field_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return value


def _csv_row_fields_changed(existing: CsvOpportunity, mapped: dict[str, Any]) -> bool:
    for key, new_val in mapped.items():
        if key == "status":
            continue
        old_val = getattr(existing, key, None)
        if _normalize_field_value(old_val) != _normalize_field_value(new_val):
            return True
    return False


def _csv_pricing_fields_changed(existing: CsvOpportunity, mapped: dict[str, Any]) -> bool:
    for key in PRICING_AFFECTING_FIELDS:
        if key not in mapped:
            continue
        old_val = getattr(existing, key, None)
        new_val = mapped[key]
        if _normalize_field_value(old_val) != _normalize_field_value(new_val):
            return True
    return False


def _deletable_csv_rows_query(session: Session):
    """CSV rows safe to drop on stale/expired purge (not protected or actively pursued via CSV)."""
    protected_lower = [status.lower() for status in PROTECTED_CSV_STATUSES]
    return (
        session.query(CsvOpportunity)
        .filter(func.lower(func.coalesce(CsvOpportunity.status, "")).notin_(protected_lower))
        .filter(CsvOpportunity.contract_id.is_(None))
    )


def remove_duplicate_csv_rows(session: Session) -> int:
    """Collapse multiple NoticeIds that share the same solicitation number (SAM amendments)."""
    rows = session.query(CsvOpportunity).all()
    groups: dict[str, list[CsvOpportunity]] = {}
    for row in rows:
        sol = _normalize_solicitation_number(row.solicitation_number)
        if not sol:
            continue
        groups.setdefault(sol, []).append(row)

    deleted = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        ranked = sorted(group, key=_csv_row_keep_score, reverse=True)
        for dup in ranked[1:]:
            if not _can_delete_csv_duplicate(dup):
                continue
            session.delete(dup)
            deleted += 1

    if deleted:
        session.flush()
    return deleted


def remove_stale_csv_rows(session: Session, present_notice_ids: set[str]) -> int:
    """Remove CSV rows absent from the latest upload (keeps protected + dashboard-linked rows)."""
    if not present_notice_ids:
        return 0
    deleted = (
        _deletable_csv_rows_query(session)
        .filter(~CsvOpportunity.notice_id.in_(present_notice_ids))
        .delete(synchronize_session=False)
    )
    if deleted:
        session.flush()
    return deleted


def remove_expired_csv_rows(session: Session, *, today: date | None = None) -> int:
    """Remove CSV rows past the response deadline (keeps Pursuing / Submitted / Won / Lost)."""
    today = today or today_local()
    protected_lower = [status.lower() for status in PROTECTED_CSV_STATUSES]
    deleted = (
        session.query(CsvOpportunity)
        .filter(func.lower(func.coalesce(CsvOpportunity.status, "")).notin_(protected_lower))
        .filter(CsvOpportunity.due_date.isnot(None))
        .filter(CsvOpportunity.due_date < today)
        .delete(synchronize_session=False)
    )
    if deleted:
        session.flush()
    return deleted

def _new_import_summary() -> dict[str, Any]:
    return {
        "records_imported": 0,
        "records_updated": 0,
        "records_unchanged": 0,
        "records_skipped_filters": 0,
        "records_protected_skipped": 0,
        "records_removed_stale": 0,
        "records_removed_expired": 0,
        "records_removed_duplicate": 0,
        "records_skipped_older_amendment": 0,
        "new_notice_ids": [],
        "changed_notice_ids": [],
        "repricing_notice_ids": [],
        "imported_notice_ids": [],
    }


def _finalize_csv_import(session: Session, summary: dict[str, Any], present_notice_ids: set[str]) -> None:
    from csv_attachment_queue_service import clear_pending_queue_for_deleted_csv

    clear_pending_queue_for_deleted_csv(session)
    summary["records_removed_stale"] = remove_stale_csv_rows(session, present_notice_ids)
    summary["records_removed_expired"] = remove_expired_csv_rows(session)
    summary["records_removed_duplicate"] = remove_duplicate_csv_rows(session)
    if summary["records_removed_expired"] or summary["records_removed_duplicate"]:
        clear_pending_queue_for_deleted_csv(session)


def import_csv_opportunities(session: Session, csv_rows: list[dict[str, str]]) -> dict[str, Any]:
    """
    Filter, dedupe, and upsert into gt_csv_opportunities (incremental merge).
    Returns counts for the import summary (does not run queue or watchlist).
    """
    today = today_local()
    summary = _new_import_summary()
    present_notice_ids: set[str] = set()

    existing_cache = _ExistingOpportunityCache(session)
    naics_set = set(get_naics_codes())

    for raw in csv_rows:
        _import_one_csv_row(
            session,
            raw,
            today=today,
            summary=summary,
            existing_cache=existing_cache,
            naics_set=naics_set,
            present_notice_ids=present_notice_ids,
            import_sequence=0,
        )

    _finalize_csv_import(session, summary, present_notice_ids)
    session.flush()
    return summary


def _import_one_csv_row(
    session: Session,
    raw: dict[str, str],
    *,
    today: date,
    summary: dict[str, Any],
    existing_cache: _ExistingOpportunityCache,
    naics_set: set[str] | None = None,
    present_notice_ids: set[str],
    import_sequence: int = 0,
) -> None:
    if not _passes_import_filters(raw, today=today, naics_set=naics_set):
        summary["records_skipped_filters"] += 1
        return

    mapped = _map_csv_row(raw, import_sequence=import_sequence)
    notice_id = mapped["notice_id"]
    present_notice_ids.add(notice_id)
    existing = existing_cache.get(notice_id)
    if existing is None:
        existing = existing_cache.get_by_solicitation(mapped.get("solicitation_number"))
    if existing and _is_protected_status(existing.status):
        summary["records_protected_skipped"] += 1
        return

    incoming_meta = mapped.get("sam_raw") if isinstance(mapped.get("sam_raw"), dict) else {}
    incoming_rank = _csv_rank_from_mapped(mapped, import_sequence=import_sequence)

    if existing:
        existing_meta = existing.sam_raw if isinstance(existing.sam_raw, dict) else {}
        merged_meta = _merge_csv_sam_meta(existing_meta, incoming_meta)
        existing.sam_raw = merged_meta
        if incoming_rank < _csv_rank_from_opportunity(existing):
            summary["records_skipped_older_amendment"] += 1
            summary["imported_notice_ids"].append(notice_id)
            existing_cache.remember(existing)
            return
        if existing.notice_id != notice_id:
            _repoint_csv_notice_id(session, existing, notice_id, existing_cache)
        if not _csv_row_fields_changed(existing, mapped):
            summary["records_unchanged"] += 1
            summary["imported_notice_ids"].append(notice_id)
            existing_cache.remember(existing)
            return

        if _csv_pricing_fields_changed(existing, mapped):
            summary["repricing_notice_ids"].append(notice_id)
            existing.pricing_intel = None

        for key, val in mapped.items():
            if key in ("status", "sam_raw"):
                continue
            setattr(existing, key, val)
        existing.sam_raw = merged_meta
        existing.status = existing.status if _is_protected_status(existing.status) else "New"
        summary["records_updated"] += 1
        summary["changed_notice_ids"].append(notice_id)
        summary["imported_notice_ids"].append(notice_id)
        existing_cache.remember(existing)
    else:
        row = CsvOpportunity(**{k: v for k, v in mapped.items() if k != "sam_raw"})
        row.sam_raw = incoming_meta
        session.add(row)
        session.flush()
        existing_cache.remember(row)
        summary["records_imported"] += 1
        summary["new_notice_ids"].append(notice_id)
        summary["repricing_notice_ids"].append(notice_id)
        summary["imported_notice_ids"].append(notice_id)


def import_csv_opportunities_from_content(
    session: Session,
    content: bytes | str,
    *,
    progress: Any | None = None,
) -> dict[str, Any]:
    """Stream-parse CSV content row-by-row (lower memory than loading all rows)."""
    if isinstance(content, bytes) and not content.strip():
        return {"ok": False, "error": "empty_or_invalid_csv"}
    if isinstance(content, str) and not content.strip():
        return {"ok": False, "error": "empty_or_invalid_csv"}

    reader = _csv_dict_reader(content)
    if not reader.fieldnames:
        return {"ok": False, "error": "empty_or_invalid_csv"}

    today = today_local()
    summary = _new_import_summary()
    present_notice_ids: set[str] = set()

    if progress:
        progress("prepare", "Preparing incremental import…", rows_scanned=0, rows_imported=0)

    existing_cache = _ExistingOpportunityCache(session)
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
            existing_cache=existing_cache,
            naics_set=naics_set,
            present_notice_ids=present_notice_ids,
            import_sequence=rows_scanned,
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

    _finalize_csv_import(session, summary, present_notice_ids)
    session.flush()
    if progress:
        imported = summary["records_imported"] + summary["records_updated"]
        progress(
            "import",
            f"CSV scan complete — {rows_scanned:,} rows read, {imported:,} new/updated, {summary['records_unchanged']:,} unchanged",
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

"""SAM.gov attachment queue for CSV-imported opportunities."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from api_budget import can_spend_sam, get_usage_snapshot
from csv_attachment_session import (
    get_csv_attachment_session_calls,
    record_csv_attachment_session_calls,
    reset_csv_attachment_session,
)
from models import AttachmentQueueItem, CsvOpportunity

logger = logging.getLogger("govtracker.csv_queue")

QUEUE_STATUS_QUEUED = "queued"
QUEUE_STATUS_DOWNLOADING = "downloading"
QUEUE_STATUS_COMPLETE = "complete"
QUEUE_STATUS_FAILED = "failed"

# Legacy statuses from earlier builds
_LEGACY_QUEUED = ("pending", "queued")
_LEGACY_ACTIVE = ("processing", "downloading")

TIER_HIGH = 0
TIER_POSSIBLE = 1_000_000
TIER_OTHER = 2_000_000


def csv_attachment_budget_threshold() -> float:
    raw = os.getenv("CSV_ATTACHMENT_BUDGET_THRESHOLD", "0.8").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 0.8
    return min(1.0, max(0.1, value))


def csv_attachment_budget_allowed(*, extra_calls: int = 0, use_reserved_budget: bool = False) -> bool:
    """True while CSV attachment pulls are allowed.

    Default: pause at CSV_ATTACHMENT_BUDGET_THRESHOLD (80%) to reserve headroom for search sync.
    use_reserved_budget: allow spending the remaining daily SAM budget (manual Pull PDFs / post-import).
    """
    if use_reserved_budget:
        return can_spend_sam(max(1, extra_calls))
    snap = get_usage_snapshot()
    limit = int(snap.get("sam_daily_limit") or 0)
    if limit <= 0:
        return True
    used = int(snap.get("sam_used_today") or 0)
    cap = int(limit * csv_attachment_budget_threshold())
    return used + max(0, extra_calls) < cap


def csv_attachment_calls_remaining_before_cap(*, use_reserved_budget: bool = False) -> int:
    """SAM API calls still allowed for CSV attachment pulls."""
    snap = get_usage_snapshot()
    if use_reserved_budget:
        return max(0, int(snap.get("sam_remaining") or 0))
    limit = int(snap.get("sam_daily_limit") or 0)
    if limit <= 0:
        return 999_999
    used = int(snap.get("sam_used_today") or 0)
    cap = int(limit * csv_attachment_budget_threshold())
    return max(0, cap - used)


def compute_queue_priority(row: CsvOpportunity) -> tuple[int, str | None]:
    """
    Priority order: High watchlist → Possible watchlist → others by soonest due date.
    Lower priority value = processed first.
    """
    today = date.today()
    days_until = (row.due_date - today).days if row.due_date else 99_999
    days_until = max(0, days_until)
    confidence = (row.watchlist_match_confidence or "").strip()
    if confidence == "High":
        return TIER_HIGH + days_until, "High"
    if confidence == "Possible":
        return TIER_POSSIBLE + days_until, "Possible"
    return TIER_OTHER + days_until, None


def clear_pending_queue_for_deleted_csv(session: Session) -> int:
    """Remove orphan queued rows (csv row was cleared on re-import)."""
    orphan = ~session.query(CsvOpportunity.id).filter(
        CsvOpportunity.id == AttachmentQueueItem.csv_opportunity_id
    ).correlate(AttachmentQueueItem).exists()
    return _queued_items_query(session).filter(orphan).delete(synchronize_session=False)


def _queued_items_query(session: Session):
    return session.query(AttachmentQueueItem).filter(
        AttachmentQueueItem.status.in_(_LEGACY_QUEUED)
    )


def _active_items_query(session: Session):
    return session.query(AttachmentQueueItem).filter(
        AttachmentQueueItem.status.in_(_LEGACY_ACTIVE)
    )


def prepare_queue_for_processing(session: Session) -> None:
    """Retry failed items from prior days; reset stuck downloading rows."""
    today = date.today()
    for item in session.query(AttachmentQueueItem).filter_by(status=QUEUE_STATUS_FAILED).all():
        if item.processed_at and item.processed_at.date() < today:
            item.status = QUEUE_STATUS_QUEUED
            item.error_message = None
            item.processed_at = None
            item.sam_api_calls_used = 0
    for item in _active_items_query(session).all():
        item.status = QUEUE_STATUS_QUEUED
    session.flush()


def enqueue_csv_attachments(
    session: Session,
    *,
    notice_ids: list[str] | None = None,
    watchlist_notice_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Add gt_attachment_queue rows for imported CSV opportunities."""
    watchlist_notice_ids = watchlist_notice_ids or set()
    if not csv_attachment_budget_allowed():
        waiting = _queued_items_query(session).count()
        logger.info(
            "CSV attachment enqueue skipped — SAM budget at %.0f%% threshold (%s waiting)",
            csv_attachment_budget_threshold() * 100,
            waiting,
        )
        return {
            "attachments_queued": 0,
            "queue_ids": [],
            "enqueue_blocked_budget": True,
            "waiting_for_budget": waiting,
        }

    query = session.query(CsvOpportunity)
    if notice_ids is not None:
        if not notice_ids:
            return {"attachments_queued": 0, "queue_ids": [], "enqueue_blocked_budget": False}
        query = query.filter(CsvOpportunity.notice_id.in_(notice_ids))

    queued = 0
    queue_ids: list[int] = []
    for row in query.all():
        if row.notice_id in watchlist_notice_ids and not row.watchlist_match_confidence:
            row.watchlist_match_confidence = "Possible"

        existing = (
            session.query(AttachmentQueueItem)
            .filter(
                AttachmentQueueItem.csv_opportunity_id == row.id,
                AttachmentQueueItem.status.in_(_LEGACY_QUEUED + _LEGACY_ACTIVE),
            )
            .first()
        )
        if existing:
            priority, confidence = compute_queue_priority(row)
            existing.priority = priority
            existing.watchlist_match = confidence in ("High", "Possible")
            existing.watchlist_confidence = confidence
            continue

        complete = (
            session.query(AttachmentQueueItem)
            .filter_by(csv_opportunity_id=row.id, status=QUEUE_STATUS_COMPLETE)
            .first()
        )
        if complete:
            continue

        priority, confidence = compute_queue_priority(row)
        item = AttachmentQueueItem(
            csv_opportunity_id=row.id,
            notice_id=row.notice_id,
            priority=priority,
            watchlist_match=confidence in ("High", "Possible"),
            watchlist_confidence=confidence,
            status=QUEUE_STATUS_QUEUED,
        )
        session.add(item)
        session.flush()
        queued += 1
        queue_ids.append(item.id)

    waiting = _queued_items_query(session).count()
    return {
        "attachments_queued": queued,
        "queue_ids": queue_ids,
        "enqueue_blocked_budget": False,
        "waiting_for_budget": waiting,
    }


def estimate_queue_api_calls(session: Session) -> int:
    """Rough SAM API calls needed for queued items (fetch raw + attachment metadata)."""
    pending = _queued_items_query(session).count()
    return pending * 2


def get_attachment_queue_dashboard_stats(session: Session) -> dict[str, Any]:
    """Counts for dashboard display."""
    snap = get_usage_snapshot()
    limit = int(snap.get("sam_daily_limit") or 0)
    used = int(snap.get("sam_used_today") or 0)
    threshold = csv_attachment_budget_threshold()
    cap = int(limit * threshold) if limit > 0 else 0
    budget_blocked = limit > 0 and used >= cap

    queued = _queued_items_query(session).count()
    downloading = _active_items_query(session).count()
    complete = session.query(AttachmentQueueItem).filter_by(status=QUEUE_STATUS_COMPLETE).count()
    failed = session.query(AttachmentQueueItem).filter_by(status=QUEUE_STATUS_FAILED).count()

    return {
        "queued": queued,
        "downloading": downloading,
        "complete": complete,
        "failed": failed,
        "waiting_for_budget": queued if budget_blocked else 0,
        "budget_threshold_reached": budget_blocked,
        "budget_threshold_pct": int(threshold * 100),
        "sam_used_today": used,
        "sam_daily_limit": limit,
        "sam_remaining_today": max(0, limit - used) if limit > 0 else None,
        "sam_budget_cap_for_attachments": cap,
        "can_process_with_reserved_budget": queued > 0 and can_spend_sam(2),
        "session_api_calls_used": get_csv_attachment_session_calls(),
        "estimated_api_calls_remaining_queue": estimate_queue_api_calls(session),
    }


def _build_minimal_sam_raw(row: CsvOpportunity) -> dict[str, Any]:
    return {
        "noticeId": row.notice_id,
        "title": row.title,
        "departmentName": row.agency,
        "officeAddress": {
            "city": row.location_city,
            "state": row.location_state,
        },
        "naicsCode": row.naics_code,
        "typeOfSetAside": row.set_aside,
        "uiLink": row.sam_url,
        "description": row.description,
    }


def process_attachment_queue(
    session: Session,
    *,
    max_calls: int | None = None,
    use_reserved_budget: bool = False,
) -> dict[str, Any]:
    """Process gt_attachment_queue in priority order without exceeding daily budget cap."""
    from sam_enrich import fetch_opportunity_raw, is_sam_metadata_ready, scrape_attachment_metadata

    prepare_queue_for_processing(session)

    calls_allowed = csv_attachment_calls_remaining_before_cap(use_reserved_budget=use_reserved_budget)
    if max_calls is not None:
        calls_allowed = min(calls_allowed, max_calls)

    result: dict[str, Any] = {
        "processed": 0,
        "completed": 0,
        "failed": 0,
        "waiting_for_budget": 0,
        "budget_threshold_reached": not csv_attachment_budget_allowed(use_reserved_budget=use_reserved_budget),
        "use_reserved_budget": use_reserved_budget,
        "sam_api_calls_used": 0,
        "session_api_calls_used": 0,
    }

    if calls_allowed <= 0 or not csv_attachment_budget_allowed(use_reserved_budget=use_reserved_budget):
        result["waiting_for_budget"] = _queued_items_query(session).count()
        result["budget_threshold_reached"] = True
        logger.info(
            "CSV attachment queue paused — %s contract(s) waiting (SAM budget %.0f%% threshold)",
            result["waiting_for_budget"],
            csv_attachment_budget_threshold() * 100,
        )
        return result

    items = (
        _queued_items_query(session)
        .order_by(AttachmentQueueItem.priority.asc(), AttachmentQueueItem.id.asc())
        .all()
    )

    session_calls = 0
    for item in items:
        if session_calls + 2 > calls_allowed:
            result["waiting_for_budget"] += 1
            continue
        if not csv_attachment_budget_allowed(extra_calls=2, use_reserved_budget=use_reserved_budget):
            result["waiting_for_budget"] += len(items) - result["processed"] - result["waiting_for_budget"]
            result["budget_threshold_reached"] = True
            break

        row = session.get(CsvOpportunity, item.csv_opportunity_id)
        if not row:
            item.status = QUEUE_STATUS_FAILED
            item.error_message = "csv_row_missing"
            item.processed_at = datetime.now(timezone.utc)
            result["failed"] += 1
            result["processed"] += 1
            continue

        item.status = QUEUE_STATUS_DOWNLOADING
        session.flush()
        calls_for_item = 0

        try:
            raw = dict(row.sam_raw) if isinstance(row.sam_raw, dict) else _build_minimal_sam_raw(row)
            if not raw.get("noticeId"):
                raw["noticeId"] = row.notice_id

            if is_sam_metadata_ready(raw):
                row.sam_raw = raw
                item.status = QUEUE_STATUS_COMPLETE
                item.processed_at = datetime.now(timezone.utc)
                result["completed"] += 1
                result["processed"] += 1
                continue

            if not can_spend_sam(1) or not csv_attachment_budget_allowed(
                extra_calls=1, use_reserved_budget=use_reserved_budget
            ):
                item.status = QUEUE_STATUS_QUEUED
                result["waiting_for_budget"] += 1
                result["budget_threshold_reached"] = True
                break

            if not isinstance(row.sam_raw, dict) or not row.sam_raw.get("noticeId"):
                fetched = fetch_opportunity_raw(row.notice_id)
                calls_for_item += 1
                session_calls += 1
                record_csv_attachment_session_calls(1)
                if fetched:
                    raw = {**_build_minimal_sam_raw(row), **fetched}

            if not can_spend_sam(1) or not csv_attachment_budget_allowed(
                extra_calls=1, use_reserved_budget=use_reserved_budget
            ):
                item.status = QUEUE_STATUS_QUEUED
                result["waiting_for_budget"] += 1
                result["budget_threshold_reached"] = True
                break

            enriched, ok = scrape_attachment_metadata(raw)
            calls_for_item += 1
            session_calls += 1
            record_csv_attachment_session_calls(1)

            row.sam_raw = enriched
            item.sam_api_calls_used = calls_for_item
            item.processed_at = datetime.now(timezone.utc)
            if ok:
                item.status = QUEUE_STATUS_COMPLETE
                result["completed"] += 1
            else:
                item.status = QUEUE_STATUS_FAILED
                item.error_message = enriched.get("scrapeError") or "attachment_scrape_failed"
                result["failed"] += 1
            result["processed"] += 1
            result["sam_api_calls_used"] += calls_for_item
        except Exception as exc:
            logger.exception("Attachment queue failed for %s", row.notice_id)
            item.status = QUEUE_STATUS_FAILED
            item.error_message = str(exc)[:500]
            item.processed_at = datetime.now(timezone.utc)
            result["failed"] += 1
            result["processed"] += 1

    result["waiting_for_budget"] = _queued_items_query(session).count()
    result["session_api_calls_used"] = get_csv_attachment_session_calls()
    if result["waiting_for_budget"] > 0 and csv_attachment_budget_allowed(use_reserved_budget=use_reserved_budget):
        result["budget_threshold_reached"] = False
    elif result["waiting_for_budget"] > 0 and not use_reserved_budget and csv_attachment_budget_allowed(
        use_reserved_budget=True
    ):
        result["budget_threshold_reached"] = False
    elif result["waiting_for_budget"] > 0:
        result["budget_threshold_reached"] = True
        logger.info(
            "CSV attachment queue: %s contract(s) waiting for SAM API budget (resumes after daily reset)",
            result["waiting_for_budget"],
        )

    session.flush()
    return result


def run_scheduled_csv_attachment_queue() -> dict[str, Any]:
    """Process queued CSV attachments during daily sync (after budget reset)."""
    from csv_attachment_policy import csv_auto_sam_attachments_on_import
    from database import SessionLocal

    if not csv_auto_sam_attachments_on_import():
        return {"skipped": True, "reason": "csv_auto_sam_attachments_disabled"}

    session = SessionLocal()
    try:
        stats_before = get_attachment_queue_dashboard_stats(session)
        if stats_before["queued"] == 0 and stats_before["failed"] == 0:
            return {"skipped": True, "reason": "empty_queue"}
        result = process_attachment_queue(session)
        session.commit()
        result["dashboard"] = get_attachment_queue_dashboard_stats(session)
        return result
    except Exception:
        session.rollback()
        logger.exception("Scheduled CSV attachment queue failed")
        raise
    finally:
        session.close()

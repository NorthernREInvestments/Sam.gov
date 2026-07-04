"""Watchlist fingerprint matching for CSV-imported opportunities."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from csv_import_service import csv_opportunity_to_posting_dict
from models import Contract, CsvOpportunity
from watchlist_fingerprint import best_fingerprint_match, posting_fingerprint_from_opportunity

logger = logging.getLogger("govtracker.csv_watchlist")


def _location_display(row: CsvOpportunity) -> str | None:
    parts = [row.location_city, row.location_state]
    text = ", ".join(p for p in parts if p)
    return text or None


def bridge_csv_to_contract(session: Session, row: CsvOpportunity) -> Contract:
    """Upsert gt_contracts from CSV row for pipeline processing (gt_ table only)."""
    from naics_labels import naics_tier

    existing = session.query(Contract).filter_by(notice_id=row.notice_id).first()
    if existing:
        row.contract_id = existing.id
        return existing

    contract = Contract(
        notice_id=row.notice_id,
        title=row.title[:512],
        agency=row.agency,
        location=_location_display(row),
        naics_code=row.naics_code,
        tier=naics_tier(row.naics_code),
        set_aside=row.set_aside,
        due_date=row.due_date,
        link=row.sam_url,
        description=(row.description or "")[:8000] or None,
        co_name=row.co_name,
        co_email=row.co_email,
        co_phone=row.co_phone,
        status="new",
        sam_raw=row.sam_raw if isinstance(row.sam_raw, dict) else None,
    )
    session.add(contract)
    session.flush()
    row.contract_id = contract.id
    return contract


def run_csv_watchlist_matching(
    session: Session,
    *,
    trigger_pipeline: bool = True,
    notice_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    Fingerprint-match gt_csv_opportunities against gs_watchlist (read-only).
    High-confidence matches trigger pipeline + GovSpend notify via existing helpers.
    """
    from gs_watchlist_service import load_watching_targets
    from watchlist_sync import _apply_best_fingerprint, start_watchlist_priority_pipeline

    targets = load_watching_targets()
    if not targets:
        return {
            "watchlist_matches": 0,
            "high_confidence": 0,
            "possible_matches": 0,
            "pipeline_started": [],
            "watchlist_notice_ids": [],
        }

    summary: dict[str, Any] = {
        "watchlist_matches": 0,
        "high_confidence": 0,
        "possible_matches": 0,
        "pipeline_started": [],
        "watchlist_notice_ids": [],
    }
    pipeline_ids: list[str] = []

    rows_query = session.query(CsvOpportunity).order_by(CsvOpportunity.due_date.asc().nullslast())
    if notice_ids:
        rows_query = rows_query.filter(CsvOpportunity.notice_id.in_(notice_ids))
    rows = rows_query.all()
    for row in rows:
        opp = csv_opportunity_to_posting_dict(row)
        posting = posting_fingerprint_from_opportunity(opp)
        result = best_fingerprint_match(posting, targets)
        if not result:
            continue
        target = next((t for t in targets if t.id == result.watchlist_id), None)
        if not target:
            from gs_watchlist_service import target_by_id

            target = target_by_id(result.watchlist_id)
        if not target:
            continue

        row.watchlist_match_score = result.score
        row.watchlist_match_confidence = result.confidence
        row.watchlist_meta = {
            "watchlist_id": target.id,
            "match_score": result.score,
            "match_confidence": result.confidence,
            "match_signals": result.to_match_signals_json(),
            "contract_name": target.contract_name,
        }

        if result.confidence in ("High", "Possible", "Weak"):
            summary["watchlist_matches"] += 1
        if result.confidence in ("High", "Possible"):
            summary["watchlist_notice_ids"].append(row.notice_id)

        if result.confidence == "High":
            summary["high_confidence"] += 1
        elif result.confidence == "Possible":
            summary["possible_matches"] += 1

        if not trigger_pipeline:
            continue

        if result.confidence in ("High", "Possible", "Weak"):
            contract = bridge_csv_to_contract(session, row)
            _apply_best_fingerprint(contract, targets, result, pipeline_ids=pipeline_ids)
            row.contract_id = contract.id

    session.flush()

    if trigger_pipeline and pipeline_ids:
        unique_ids = list(dict.fromkeys(pipeline_ids))
        start_watchlist_priority_pipeline(unique_ids)
        summary["pipeline_started"] = unique_ids

    return summary


def run_full_csv_upload_pipeline(
    session: Session,
    csv_content: bytes | str,
) -> dict[str, Any]:
    """Steps 1–5: import, queue, attachments, watchlist match."""
    from csv_attachment_queue_service import (
        enqueue_csv_attachments,
        estimate_queue_api_calls,
        get_attachment_queue_dashboard_stats,
        process_attachment_queue,
    )
    from csv_import_service import import_csv_opportunities, parse_sam_csv
    from csv_attachment_session import reset_csv_attachment_session

    reset_csv_attachment_session()
    rows = parse_sam_csv(csv_content)
    if not rows:
        return {"ok": False, "error": "empty_or_invalid_csv"}

    import_summary = import_csv_opportunities(session, rows)
    session.commit()

    imported_notice_ids = import_summary.get("imported_notice_ids") or []
    pre_watchlist = run_csv_watchlist_matching(
        session,
        trigger_pipeline=False,
        notice_ids=imported_notice_ids or None,
    )
    session.commit()
    watchlist_ids = set(pre_watchlist.get("watchlist_notice_ids") or [])

    queue_summary = enqueue_csv_attachments(
        session,
        notice_ids=imported_notice_ids if imported_notice_ids else None,
        watchlist_notice_ids=watchlist_ids,
    )
    session.commit()

    attachment_summary = process_attachment_queue(session)
    session.commit()

    watchlist_summary = run_csv_watchlist_matching(
        session,
        trigger_pipeline=True,
        notice_ids=imported_notice_ids or None,
    )
    session.commit()

    protected_count = import_summary.get("records_protected_skipped", 0)

    estimated_remaining = estimate_queue_api_calls(session)

    return {
        "ok": True,
        "records_imported": import_summary["records_imported"],
        "records_updated": import_summary.get("records_updated", 0),
        "records_skipped_filters": import_summary["records_skipped_filters"],
        "records_protected": protected_count,
        "records_deleted_before_import": import_summary["records_deleted_before_import"],
        "watchlist_matches_found": watchlist_summary["watchlist_matches"],
        "high_confidence_matches": watchlist_summary["high_confidence"],
        "possible_matches": watchlist_summary["possible_matches"],
        "attachments_queued": queue_summary["attachments_queued"],
        "attachments_processed": attachment_summary.get("processed", 0),
        "attachments_completed": attachment_summary.get("completed", 0),
        "attachments_failed": attachment_summary.get("failed", 0),
        "attachments_skipped_budget": attachment_summary.get("waiting_for_budget", 0),
        "attachments_waiting_for_budget": attachment_summary.get("waiting_for_budget", 0),
        "budget_threshold_reached": attachment_summary.get("budget_threshold_reached", False),
        "sam_api_calls_used_this_session": attachment_summary.get("session_api_calls_used", 0),
        "estimated_api_calls_remaining_queue": estimated_remaining,
        "attachment_queue": get_attachment_queue_dashboard_stats(session),
        "pipeline_started": watchlist_summary.get("pipeline_started", []),
        "api_budget": __import__("api_budget", fromlist=["get_usage_snapshot"]).get_usage_snapshot(),
    }

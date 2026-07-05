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

        if result.confidence in ("High", "Possible", "Weak"):
            contract = bridge_csv_to_contract(session, row)
            _apply_best_fingerprint(
                contract,
                targets,
                result,
                pipeline_ids=pipeline_ids if trigger_pipeline else [],
            )
            row.contract_id = contract.id

    session.flush()

    if trigger_pipeline and pipeline_ids:
        from screening_pipeline import has_attachments_ready

        ready_ids = [
            notice_id
            for notice_id in dict.fromkeys(pipeline_ids)
            if (row := session.query(Contract).filter_by(notice_id=notice_id).first())
            and has_attachments_ready(row, session)
        ]
        if ready_ids:
            start_watchlist_priority_pipeline(ready_ids)
        summary["pipeline_started"] = ready_ids
    elif trigger_pipeline:
        summary["pipeline_started"] = []

    return summary


def run_full_csv_upload_pipeline(
    session: Session,
    csv_content: bytes | str,
    *,
    process_attachments: bool = True,
) -> dict[str, Any]:
    """Steps 1–5: import, queue, optional attachments, watchlist match."""
    from csv_attachment_queue_service import (
        enqueue_csv_attachments,
        estimate_queue_api_calls,
        get_attachment_queue_dashboard_stats,
        start_background_csv_attachment_queue,
    )
    from csv_import_service import import_csv_opportunities_from_content
    from csv_attachment_session import reset_csv_attachment_session
    from csv_upload_job import update_csv_upload_progress

    reset_csv_attachment_session()
    import_summary = import_csv_opportunities_from_content(
        session,
        csv_content,
        progress=update_csv_upload_progress,
    )
    if not import_summary.get("ok"):
        return {"ok": False, "error": import_summary.get("error", "empty_or_invalid_csv")}
    session.commit()

    imported_notice_ids = import_summary.get("imported_notice_ids") or []
    pipeline_notice_ids = list(
        dict.fromkeys(
            (import_summary.get("new_notice_ids") or [])
            + (import_summary.get("changed_notice_ids") or [])
        )
    )
    repricing_notice_ids = list(dict.fromkeys(import_summary.get("repricing_notice_ids") or []))

    update_csv_upload_progress("watchlist", "Matching GovSpend watchlist fingerprints (pass 1)…")
    pre_watchlist = run_csv_watchlist_matching(
        session,
        trigger_pipeline=False,
        notice_ids=pipeline_notice_ids or None,
    )
    session.commit()
    watchlist_ids = set(pre_watchlist.get("watchlist_notice_ids") or [])

    update_csv_upload_progress("queue", "Saving new/changed rows…")
    queue_summary = enqueue_csv_attachments(
        session,
        notice_ids=pipeline_notice_ids if pipeline_notice_ids else None,
        watchlist_notice_ids=watchlist_ids,
    )
    session.commit()

    # SAM attachment pulls run in background — import is already complete from here.
    attachment_summary = {
        "processed": 0,
        "completed": 0,
        "failed": 0,
        "waiting_for_budget": queue_summary.get("waiting_for_budget", 0),
        "deferred_background": True,
        "session_api_calls_used": 0,
    }
    if pipeline_notice_ids:
        start_background_csv_attachment_queue()

    update_csv_upload_progress(
        "finalize",
        "Promoting watchlist matches to dashboard…",
        rows_imported=import_summary.get("records_imported", 0) + import_summary.get("records_updated", 0),
    )
    watchlist_summary = run_csv_watchlist_matching(
        session,
        trigger_pipeline=True,
        notice_ids=pipeline_notice_ids or None,
    )
    session.commit()

    pricing_result: dict[str, Any] = {"ok": True, "skipped": True, "total": 0}
    if repricing_notice_ids:
        from csv_pricing_job import start_csv_pricing_job

        pricing_result = start_csv_pricing_job(notice_ids=repricing_notice_ids)

    protected_count = import_summary.get("records_protected_skipped", 0)

    estimated_remaining = estimate_queue_api_calls(session)

    return {
        "ok": True,
        "records_imported": import_summary["records_imported"],
        "records_updated": import_summary.get("records_updated", 0),
        "records_unchanged": import_summary.get("records_unchanged", 0),
        "records_skipped_filters": import_summary["records_skipped_filters"],
        "records_protected": protected_count,
        "records_removed_stale": import_summary.get("records_removed_stale", 0),
        "new_notice_ids": import_summary.get("new_notice_ids") or [],
        "changed_notice_ids": import_summary.get("changed_notice_ids") or [],
        "repricing_notice_ids": repricing_notice_ids,
        "pricing_started": bool(pricing_result.get("ok") and not pricing_result.get("skipped")),
        "pricing_total": pricing_result.get("total", 0),
        "pricing_message": pricing_result.get("message"),
        "watchlist_matches_found": watchlist_summary["watchlist_matches"],
        "high_confidence_matches": watchlist_summary["high_confidence"],
        "possible_matches": watchlist_summary["possible_matches"],
        "attachments_queued": queue_summary["attachments_queued"],
        "attachments_deferred_background": attachment_summary.get("deferred_background", False),
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

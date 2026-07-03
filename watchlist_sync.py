"""GovSpend watchlist → SAM.gov search → priority pipeline (read-only on gs_watchlist)."""

from __future__ import annotations

import logging
import threading
from typing import Any

from database import SessionLocal
from models import Contract

logger = logging.getLogger("govtracker.watchlist")

_pipeline_lock = threading.Lock()
_pipeline_running = False


def _record_watchlist_hit(
    row: Contract,
    target,
    *,
    match_fields: list[str],
    match_count: int,
) -> str | None:
    """Stamp local match, apply pricing, notify GovSpend API."""
    from gs_watchlist_service import stamp_govspend_watchlist_hit
    from govspend_client import notify_govspend_watchlist_hit
    from watchlist_pricing import apply_watchlist_pricing

    stamp_govspend_watchlist_hit(
        row,
        target,
        match_fields=match_fields,
        match_count=match_count,
    )
    apply_watchlist_pricing(row)
    notify_govspend_watchlist_hit(
        row,
        target,
        match_fields=match_fields,
        match_count=match_count,
    )
    return row.notice_id


def rematch_existing_contracts(session) -> list[str]:
    """Daily pass: mark in-DB contracts that match GovSpend watchlist targets."""
    from gs_watchlist_service import (
        is_govspend_watchlist_hit,
        is_watchlist_field_match,
        load_watching_targets,
        score_contract_against_target,
    )

    targets = load_watching_targets()
    if not targets:
        return []

    hits: list[str] = []
    rows = session.query(Contract).all()
    for row in rows:
        if is_govspend_watchlist_hit(row):
            continue
        best_target = None
        best_count = 0
        best_fields: list[str] = []
        for target in targets:
            count, fields = score_contract_against_target(row, target)
            if count > best_count:
                best_count = count
                best_target = target
                best_fields = fields
        if not best_target or not is_watchlist_field_match(best_count):
            continue
        notice_id = _record_watchlist_hit(
            row,
            best_target,
            match_fields=best_fields,
            match_count=best_count,
        )
        if notice_id:
            hits.append(notice_id)
    return hits


def _process_sam_batch_for_target(
    session,
    target,
    opportunities: list[dict[str, Any]],
) -> list[str]:
    from gs_watchlist_service import is_watchlist_field_match, score_opportunity_against_target
    from sync import filter_search_results, upsert_contracts

    batch, _ = filter_search_results(opportunities, session, min_days_until_due=0, min_score=1)
    if not batch:
        return []

    upsert_contracts(session, batch)
    session.flush()

    notice_ids: list[str] = []
    for opp in batch:
        count, fields = score_opportunity_against_target(opp, target)
        if not is_watchlist_field_match(count):
            continue
        notice_id = str(opp.get("notice_id") or "")
        if not notice_id:
            continue
        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            continue
        recorded = _record_watchlist_hit(
            row,
            target,
            match_fields=fields,
            match_count=count,
        )
        if recorded:
            notice_ids.append(recorded)
    return notice_ids


def run_govspend_watchlist_sync(*, trigger_pipeline: bool = True) -> dict[str, Any]:
    """
    1) Read gs_watchlist (High/Medium + Watching)
    2) SAM search per target (uses API budget first)
    3) Mark 3+ field matches, apply watchlist pricing, run priority pipeline
    """
    from api_budget import can_spend_sam, get_usage_snapshot
    from gs_watchlist_service import clear_watchlist_cache, load_watching_targets
    from sam_client import fetch_govspend_target_from_sam

    clear_watchlist_cache()
    targets = load_watching_targets()
    result: dict[str, Any] = {
        "targets_loaded": len(targets),
        "sam_searches": 0,
        "sam_hits": 0,
        "rematched_existing": 0,
        "govspend_notifications": {"attempted": 0, "sent": 0, "failed": 0, "skipped": 0},
        "priority_notice_ids": [],
        "errors": [],
    }

    session = SessionLocal()
    try:
        rematched = rematch_existing_contracts(session)
        result["rematched_existing"] = len(rematched)
        priority_ids = list(dict.fromkeys(rematched))
        from govspend_client import retry_pending_govspend_notifications

        result["govspend_notifications"] = retry_pending_govspend_notifications(session)
        session.commit()
    except Exception as exc:
        session.rollback()
        result["errors"].append(f"rematch: {exc}")
        priority_ids = []
    finally:
        session.close()

    for target in targets:
        if not can_spend_sam(1):
            result["errors"].append("SAM budget exhausted during watchlist search")
            break
        try:
            opportunities = fetch_govspend_target_from_sam(target)
            result["sam_searches"] += 1
        except Exception as exc:
            result["errors"].append(f"target {target.id}: {exc}")
            continue

        session = SessionLocal()
        try:
            hit_ids = _process_sam_batch_for_target(session, target, opportunities)
            from govspend_client import retry_pending_govspend_notifications

            notify_stats = retry_pending_govspend_notifications(session)
            for key in ("attempted", "sent", "failed", "skipped"):
                result["govspend_notifications"][key] += notify_stats.get(key, 0)
            session.commit()
            priority_ids.extend(hit_ids)
            result["sam_hits"] += len(hit_ids)
        except Exception as exc:
            session.rollback()
            result["errors"].append(f"target {target.id} upsert: {exc}")
        finally:
            session.close()

    priority_ids = list(dict.fromkeys(priority_ids))
    result["priority_notice_ids"] = priority_ids
    result["api_budget"] = get_usage_snapshot()

    if trigger_pipeline and priority_ids:
        start_watchlist_priority_pipeline(priority_ids)

    logger.info(
        "GovSpend watchlist sync: targets=%s searches=%s hits=%s pipeline=%s",
        len(targets),
        result["sam_searches"],
        result["sam_hits"],
        len(priority_ids),
    )
    return result


def start_watchlist_priority_pipeline(notice_ids: list[str]) -> dict[str, Any]:
    """Background: attachments → full intake (skip normal scoring queue)."""
    global _pipeline_running
    notice_ids = [n for n in notice_ids if n]
    if not notice_ids:
        return {"started": False, "reason": "empty"}

    with _pipeline_lock:
        if _pipeline_running:
            return {"started": False, "reason": "already_running"}
        _pipeline_running = True

    def _run() -> None:
        global _pipeline_running
        try:
            session = SessionLocal()
            try:
                from intake import enrich_matching_attachments, intake_matching_contracts

                for notice_id in notice_ids:
                    row = session.query(Contract).filter_by(notice_id=notice_id).first()
                    if not row:
                        continue
                    analysis = dict(row.analysis) if isinstance(row.analysis, dict) else {}
                    meta = dict(analysis.get("govspend_watchlist") or {})
                    meta["pipeline_status"] = "running"
                    analysis["govspend_watchlist"] = meta
                    row.analysis = analysis
                session.commit()

                enrich_matching_attachments(session, notice_ids, limit=None)
                session.commit()
                intake_matching_contracts(
                    session,
                    notice_ids,
                    limit=None,
                    force=True,
                    force_full=True,
                )
                session.commit()

                for notice_id in notice_ids:
                    row = session.query(Contract).filter_by(notice_id=notice_id).first()
                    if not row:
                        continue
                    analysis = dict(row.analysis) if isinstance(row.analysis, dict) else {}
                    meta = dict(analysis.get("govspend_watchlist") or {})
                    meta["pipeline_status"] = "complete"
                    analysis["govspend_watchlist"] = meta
                    row.analysis = analysis
                session.commit()
            finally:
                session.close()
        except Exception:
            logger.exception("Watchlist priority pipeline failed")
        finally:
            with _pipeline_lock:
                _pipeline_running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-watchlist-pipeline").start()
    return {"started": True, "notice_ids": notice_ids}


def list_watchlist_hit_contracts(session) -> list[Contract]:
    rows = session.query(Contract).order_by(Contract.due_date.asc().nullslast(), Contract.id.asc()).all()
    from gs_watchlist_service import is_govspend_watchlist_hit

    hits = [row for row in rows if is_govspend_watchlist_hit(row)]
    today = __import__("datetime").date.today()
    hits.sort(
        key=lambda r: (
            r.due_date is None,
            (r.due_date - today).days if r.due_date else 9999,
        )
    )
    return hits

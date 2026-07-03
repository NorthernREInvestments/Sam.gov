"""GovSpend watchlist → SAM.gov search → priority pipeline (read-only on gs_watchlist)."""

from __future__ import annotations

import logging
import threading
from typing import Any

from database import SessionLocal
from models import Contract
from watchlist_fingerprint import FingerprintMatchResult, log_weak_match, posting_fingerprint_from_contract

logger = logging.getLogger("govtracker.watchlist")

_pipeline_lock = threading.Lock()
_pipeline_running = False


def _resolve_target(targets, watchlist_id: int):
    for target in targets:
        if target.id == watchlist_id:
            return target
    from gs_watchlist_service import target_by_id

    return target_by_id(watchlist_id)


def _record_fingerprint_match(
    row: Contract,
    target,
    result: FingerprintMatchResult,
    *,
    notify: bool,
    apply_pricing: bool,
) -> str | None:
    from gs_watchlist_service import (
        should_notify_for_contract,
        should_run_pipeline_for_contract,
        stamp_fingerprint_match,
    )
    from govspend_client import notify_govspend_watchlist_hit
    from watchlist_pricing import apply_watchlist_pricing

    posting = posting_fingerprint_from_contract(row)
    stamp_fingerprint_match(row, target, result, posting=posting)

    if apply_pricing and should_run_pipeline_for_contract(row):
        apply_watchlist_pricing(row)

    if notify and should_notify_for_contract(row):
        notify_govspend_watchlist_hit(
            row,
            target,
            match_fields=result.matched_signals,
            match_count=len(result.matched_signals),
            match_score=result.score,
            match_confidence=result.confidence,
            match_signals=result.to_match_signals_json(),
        )
    return row.notice_id


def _apply_best_fingerprint(
    row: Contract,
    targets,
    result: FingerprintMatchResult,
    *,
    pipeline_ids: list[str],
) -> None:
    from gs_watchlist_service import is_watchlist_rejected, should_run_pipeline_for_contract

    if is_watchlist_rejected(row):
        return

    target = _resolve_target(targets, result.watchlist_id)
    if not target:
        return

    if result.confidence == "Weak":
        log_weak_match(row.notice_id, result)
        _record_fingerprint_match(
            row,
            target,
            result,
            notify=False,
            apply_pricing=False,
        )
        return

    if result.confidence == "Possible":
        notice_id = _record_fingerprint_match(
            row,
            target,
            result,
            notify=False,
            apply_pricing=False,
        )
        if notice_id:
            logger.info(
                "Possible watchlist match notice_id=%s watchlist_id=%s score=%s",
                notice_id,
                target.id,
                result.score,
            )
        return

    if result.confidence == "High":
        notice_id = _record_fingerprint_match(
            row,
            target,
            result,
            notify=True,
            apply_pricing=True,
        )
        if notice_id and should_run_pipeline_for_contract(row):
            pipeline_ids.append(notice_id)


def rematch_existing_contracts(session) -> list[str]:
    """Daily pass: fingerprint-match in-DB contracts against GovSpend watchlist targets."""
    from gs_watchlist_service import (
        best_contract_fingerprint,
        fingerprint_meta,
        is_govspend_watchlist_hit,
        is_possible_watchlist_match,
        is_watchlist_rejected,
        load_watching_targets,
    )

    targets = load_watching_targets()
    if not targets:
        return []

    pipeline_ids: list[str] = []
    rows = session.query(Contract).all()
    for row in rows:
        if is_watchlist_rejected(row):
            continue
        if is_govspend_watchlist_hit(row) or is_possible_watchlist_match(row):
            continue
        result = best_contract_fingerprint(row, targets)
        if not result:
            continue
        _apply_best_fingerprint(row, targets, result, pipeline_ids=pipeline_ids)
    return list(dict.fromkeys(pipeline_ids))


def _process_sam_batch_for_target(
    session,
    target,
    opportunities: list[dict[str, Any]],
    *,
    all_targets,
    pipeline_ids: list[str],
) -> list[str]:
    from gs_watchlist_service import (
        best_opportunity_fingerprint,
        is_govspend_watchlist_hit,
        is_possible_watchlist_match,
        is_watchlist_rejected,
    )
    from sync import filter_search_results, upsert_contracts

    batch, _ = filter_search_results(opportunities, session, min_days_until_due=0, min_score=1)
    if not batch:
        return []

    upsert_contracts(session, batch)
    session.flush()

    notice_ids: list[str] = []
    for opp in batch:
        notice_id = str(opp.get("notice_id") or "")
        if not notice_id:
            continue
        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            continue
        if is_watchlist_rejected(row) or is_govspend_watchlist_hit(row) or is_possible_watchlist_match(row):
            continue

        _, result = best_opportunity_fingerprint(opp, all_targets)
        if not result:
            continue
        before = len(pipeline_ids)
        _apply_best_fingerprint(row, all_targets, result, pipeline_ids=pipeline_ids)
        meta = fingerprint_meta(row)
        if meta and meta.get("match_confidence") in ("High", "Possible"):
            notice_ids.append(notice_id)
        if len(pipeline_ids) > before and notice_id not in pipeline_ids:
            pipeline_ids.append(notice_id)
    return notice_ids


def confirm_watchlist_match(session, notice_id: str) -> dict[str, Any]:
    from gs_watchlist_service import (
        confirm_fingerprint_match,
        fingerprint_meta,
        should_notify_for_contract,
        should_run_pipeline_for_contract,
        target_from_meta,
    )
    from govspend_client import notify_govspend_watchlist_hit
    from watchlist_pricing import apply_watchlist_pricing

    row = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not row:
        return {"ok": False, "error": "not_found"}
    prev = fingerprint_meta(row) or {}
    if prev.get("match_confidence") != "Possible":
        return {"ok": False, "error": "not_possible_match"}
    if prev.get("match_confirmed"):
        return {"ok": True, "notice_id": notice_id, "already_confirmed": True}

    meta = confirm_fingerprint_match(row)
    if not meta:
        return {"ok": False, "error": "confirm_failed"}

    target = target_from_meta(meta)
    apply_watchlist_pricing(row)
    if target and should_notify_for_contract(row):
        notify_govspend_watchlist_hit(
            row,
            target,
            match_fields=list(meta.get("match_fields") or []),
            match_count=int(meta.get("match_count") or 0),
            match_score=int(meta.get("match_score") or 0),
            match_confidence=str(meta.get("match_confidence") or "Possible"),
            match_signals=list(meta.get("match_signals") or []),
        )

    pipeline_started = False
    if should_run_pipeline_for_contract(row):
        start_watchlist_priority_pipeline([notice_id])
        pipeline_started = True

    session.commit()
    return {
        "ok": True,
        "notice_id": notice_id,
        "match_confirmed": True,
        "pipeline_started": pipeline_started,
    }


def reject_watchlist_match(session, notice_id: str) -> dict[str, Any]:
    from gs_watchlist_service import fingerprint_meta, reject_fingerprint_match

    row = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not row:
        return {"ok": False, "error": "not_found"}
    prev = fingerprint_meta(row) or {}
    if prev.get("match_confidence") not in ("Possible", "Weak"):
        return {"ok": False, "error": "not_reviewable"}
    if prev.get("match_rejected"):
        return {"ok": True, "notice_id": notice_id, "already_rejected": True}

    meta = reject_fingerprint_match(row)
    if not meta:
        return {"ok": False, "error": "reject_failed"}
    session.commit()
    return {"ok": True, "notice_id": notice_id, "match_rejected": True}


def run_govspend_watchlist_sync(*, trigger_pipeline: bool = True) -> dict[str, Any]:
    """
    1) Read gs_watchlist (High/Medium + Watching)
    2) SAM search per target (uses API budget first)
    3) Fingerprint match with tiered confidence, pricing, and priority pipeline
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
        "possible_matches": 0,
        "weak_logged": 0,
        "govspend_notifications": {"attempted": 0, "sent": 0, "failed": 0, "skipped": 0},
        "priority_notice_ids": [],
        "errors": [],
    }

    pipeline_ids: list[str] = []

    session = SessionLocal()
    try:
        rematched = rematch_existing_contracts(session)
        result["rematched_existing"] = len(rematched)
        pipeline_ids.extend(rematched)
        from govspend_client import retry_pending_govspend_notifications

        result["govspend_notifications"] = retry_pending_govspend_notifications(session)
        session.commit()
    except Exception as exc:
        session.rollback()
        result["errors"].append(f"rematch: {exc}")
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
            hit_ids = _process_sam_batch_for_target(
                session,
                target,
                opportunities,
                all_targets=targets,
                pipeline_ids=pipeline_ids,
            )
            from govspend_client import retry_pending_govspend_notifications

            notify_stats = retry_pending_govspend_notifications(session)
            for key in ("attempted", "sent", "failed", "skipped"):
                result["govspend_notifications"][key] += notify_stats.get(key, 0)
            session.commit()
            result["sam_hits"] += len(hit_ids)
        except Exception as exc:
            session.rollback()
            result["errors"].append(f"target {target.id} upsert: {exc}")
        finally:
            session.close()

    pipeline_ids = list(dict.fromkeys(pipeline_ids))
    result["priority_notice_ids"] = pipeline_ids

    session = SessionLocal()
    try:
        from gs_watchlist_service import fingerprint_meta, is_govspend_watchlist_hit, is_possible_watchlist_match

        rows = session.query(Contract).all()
        result["possible_matches"] = sum(1 for row in rows if is_possible_watchlist_match(row))
        result["weak_logged"] = sum(
            1
            for row in rows
            if (meta := fingerprint_meta(row))
            and meta.get("match_confidence") == "Weak"
            and not meta.get("match_rejected")
        )
        result["sam_hits"] = sum(1 for row in rows if is_govspend_watchlist_hit(row))
    finally:
        session.close()

    result["api_budget"] = get_usage_snapshot()

    if trigger_pipeline and pipeline_ids:
        start_watchlist_priority_pipeline(pipeline_ids)

    logger.info(
        "GovSpend watchlist sync: targets=%s searches=%s high_hits=%s possible=%s pipeline=%s",
        len(targets),
        result["sam_searches"],
        result["sam_hits"],
        result["possible_matches"],
        len(pipeline_ids),
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
    from gs_watchlist_service import is_govspend_watchlist_hit

    rows = session.query(Contract).order_by(Contract.due_date.asc().nullslast(), Contract.id.asc()).all()
    hits = [row for row in rows if is_govspend_watchlist_hit(row)]
    today = __import__("datetime").date.today()
    hits.sort(
        key=lambda r: (
            r.due_date is None,
            (r.due_date - today).days if r.due_date else 9999,
            -int((r.analysis or {}).get("govspend_watchlist", {}).get("match_score") or 0),
        )
    )
    return hits


def list_possible_watchlist_matches(session) -> list[Contract]:
    from gs_watchlist_service import is_possible_watchlist_match

    rows = session.query(Contract).order_by(Contract.due_date.asc().nullslast(), Contract.id.asc()).all()
    matches = [row for row in rows if is_possible_watchlist_match(row)]
    today = __import__("datetime").date.today()
    matches.sort(
        key=lambda r: (
            r.due_date is None,
            (r.due_date - today).days if r.due_date else 9999,
            -int((r.analysis or {}).get("govspend_watchlist", {}).get("match_score") or 0),
        )
    )
    return matches

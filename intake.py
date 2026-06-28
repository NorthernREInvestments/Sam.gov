"""Two-step contract intake: text screening → full PDF analysis when score >= threshold."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from api_budget import (
    ScreenBudgetExceeded,
    can_screen,
    can_spend_sam,
    intake_on_sync_enabled,
    intake_per_sync_limit,
    record_screen_usage,
)
from claude_client import screen_contract, screen_contract_text
from database import SessionLocal
from models import Contract
from screening_pipeline import (
    SKIP_LOW_SCORE_LABEL,
    finalize_full_analysis,
    full_analysis_min_score,
    is_full_analysis_complete,
    mark_low_text_score,
    mark_pending_full_analysis,
    needs_intake,
    needs_text_screening,
    qualifies_for_full_analysis,
    text_score_from_analysis,
)

logger = logging.getLogger("govtracker.intake")
_background_lock = threading.Lock()
_background_running = False
_attachment_lock = threading.Lock()
_attachment_running = False
_intake_ids: set[str] = set()
_intake_ids_lock = threading.Lock()


def _try_begin_intake(notice_id: str) -> bool:
    with _intake_ids_lock:
        if notice_id in _intake_ids:
            return False
        _intake_ids.add(notice_id)
        return True


def _end_intake(notice_id: str) -> None:
    with _intake_ids_lock:
        _intake_ids.discard(notice_id)


def ensure_description_for_text_screen(row: Contract) -> bool:
    """Fetch SAM posting description only (1 API call max) — no attachments or PIEE."""
    from sam_client import normalize_opportunity
    from sam_enrich import enrich_description_only

    raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
    if raw.get("descriptionText"):
        if not row.description:
            row.description = raw["descriptionText"][:8000]
        return False

    if not can_spend_sam(1):
        return False

    enriched = enrich_description_only(raw)
    if not enriched.get("descriptionText") and not row.description:
        return False

    row.sam_raw = enriched
    if enriched.get("descriptionText"):
        row.description = enriched["descriptionText"][:8000]
    refreshed = normalize_opportunity(enriched)
    if refreshed.get("location"):
        row.location = refreshed["location"]
    return True


def enrich_contract_attachments(row: Contract) -> bool:
    """Load full SAM.gov scrape (description + attachments + PIEE manifest)."""
    from sam_enrich import is_scrape_complete, scrape_opportunity_complete
    from sam_client import normalize_opportunity

    raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
    if is_scrape_complete(raw):
        return False

    enriched, ok = scrape_opportunity_complete(raw)
    if not ok:
        return False

    row.sam_raw = enriched
    if enriched.get("descriptionText"):
        row.description = enriched["descriptionText"][:8000]

    refreshed = normalize_opportunity(enriched)
    if refreshed.get("location"):
        row.location = refreshed["location"]
    return True


def enrich_contract_from_sam(row: Contract) -> bool:
    return enrich_contract_attachments(row)


def run_text_screen(row: Contract) -> dict[str, Any]:
    """Step 1 — Claude text-only score; no PDF download."""
    ensure_description_for_text_screen(row)
    if not can_screen():
        return {
            "notice_id": row.notice_id,
            "skipped": True,
            "reason": "screen_budget",
            "message": "Claude screening budget reached before text triage could run.",
        }

    analysis = screen_contract_text(row)
    if not record_screen_usage():
        raise ScreenBudgetExceeded()

    prior = row.analysis if isinstance(row.analysis, dict) else {}
    if prior.get("text_score") is not None and analysis.get("text_score") is None:
        analysis["text_score"] = prior["text_score"]

    score = text_score_from_analysis(analysis) or 0
    analysis["text_score"] = score
    analysis["score"] = score
    analysis["screening_stage"] = "text"

    if score < full_analysis_min_score():
        mark_low_text_score(row, analysis)
        return {
            "notice_id": row.notice_id,
            "skipped": False,
            "text_screened": True,
            "full_analysis": False,
            "text_score": score,
            "skip_reason": SKIP_LOW_SCORE_LABEL,
            "analysis": analysis,
        }

    mark_pending_full_analysis(row, analysis)
    return {
        "notice_id": row.notice_id,
        "skipped": False,
        "text_screened": True,
        "full_analysis": False,
        "text_score": score,
        "analysis": analysis,
    }


def run_full_analysis(row: Contract, *, prior: dict[str, Any] | None = None) -> dict[str, Any]:
    """Step 2 — PIEE/attachments + PDFs + full Claude analysis."""
    from pws_fields import apply_pws_extraction
    from sub_finder import maybe_auto_sub_search

    prior = prior or (row.analysis if isinstance(row.analysis, dict) else {})
    text_score = text_score_from_analysis(prior)

    enriched = enrich_contract_from_sam(row)
    if not enriched:
        from sam_enrich import is_scrape_complete

        raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
        if not is_scrape_complete(raw):
            return {
                "notice_id": row.notice_id,
                "skipped": True,
                "reason": "sam_budget",
                "message": "SAM.gov daily budget reached before attachments/PIEE could be loaded.",
                "text_score": text_score,
            }

    if not can_screen():
        return {
            "notice_id": row.notice_id,
            "skipped": True,
            "reason": "screen_budget",
            "enriched": True,
            "message": "Claude budget reached before full PDF analysis could run.",
            "text_score": text_score,
        }

    analysis = screen_contract(row)
    if not record_screen_usage():
        raise ScreenBudgetExceeded()

    if text_score is not None:
        analysis["text_score"] = text_score
    if prior.get("text_reason") and not analysis.get("text_reason"):
        analysis["text_reason"] = prior["text_reason"]
    if prior.get("text_screened_at"):
        analysis["text_screened_at"] = prior["text_screened_at"]

    finalize_full_analysis(row, analysis)
    apply_pws_extraction(row, analysis)

    if analysis.get("estimated_value") and not row.estimated_value:
        row.estimated_value = str(analysis["estimated_value"])[:128]

    maybe_auto_sub_search(row)

    return {
        "notice_id": row.notice_id,
        "skipped": False,
        "enriched": True,
        "screened": True,
        "full_analysis": True,
        "text_score": text_score,
        "pdfs_sent": analysis.get("pdfs_sent_to_claude", 0),
        "analysis": analysis,
    }


def full_intake_contract(row: Contract, *, force: bool = False, force_full: bool = False) -> dict[str, Any]:
    """
    Two-step intake:
    1. Text screening (no PDFs)
    2. Full PDF analysis only if text score >= FULL_ANALYSIS_MIN_SCORE (default 6) or force_full
    """
    if is_full_analysis_complete(row.analysis) and not force and not force_full:
        return {"notice_id": row.notice_id, "skipped": True, "reason": "already_analyzed"}

    if not _try_begin_intake(row.notice_id):
        return {"notice_id": row.notice_id, "in_progress": True}

    try:
        analysis = row.analysis if isinstance(row.analysis, dict) else {}

        if force_full or (is_full_analysis_complete(analysis) and force):
            return run_full_analysis(row, prior=analysis)

        if needs_text_screening(analysis) or force:
            text_result = run_text_screen(row)
            if text_result.get("skipped") and text_result.get("reason") == "screen_budget":
                return text_result
            analysis = row.analysis if isinstance(row.analysis, dict) else text_result.get("analysis", {})

        if force_full or qualifies_for_full_analysis(analysis, force=force_full):
            return run_full_analysis(row, prior=analysis)

        if text_score_from_analysis(analysis) is not None:
            return {
                "notice_id": row.notice_id,
                "skipped": False,
                "text_screened": True,
                "full_analysis": False,
                "text_score": text_score_from_analysis(analysis),
                "skip_reason": analysis.get("skip_reason"),
                "analysis": analysis,
            }

        return {"notice_id": row.notice_id, "skipped": True, "reason": "no_description"}
    finally:
        _end_intake(row.notice_id)


def force_full_analysis_contract(row: Contract) -> dict[str, Any]:
    """Manual override — run full PDF analysis regardless of text score."""
    if not _try_begin_intake(row.notice_id):
        return {"notice_id": row.notice_id, "in_progress": True}
    try:
        analysis = row.analysis if isinstance(row.analysis, dict) else {}
        if needs_text_screening(analysis):
            ensure_description_for_text_screen(row)
            if can_screen():
                text_analysis = screen_contract_text(row)
                record_screen_usage()
                analysis = {**analysis, **text_analysis}
                analysis["text_score"] = text_score_from_analysis(text_analysis)
                analysis["score"] = analysis["text_score"]
                row.analysis = analysis
        return run_full_analysis(row, prior=analysis)
    finally:
        _end_intake(row.notice_id)


def intake_matching_contracts(
    session,
    notice_ids: list[str],
    *,
    limit: int | None = None,
    force: bool = False,
    force_full: bool = False,
) -> dict[str, Any]:
    """Run two-step intake for synced contracts that need text or full analysis."""
    from sync import list_contracts

    if not intake_on_sync_enabled() and not force and not force_full:
        return {"processed": 0, "screened": 0, "text_screened": 0, "enriched_only": 0, "skipped": 0, "errors": []}

    cap = intake_per_sync_limit() if limit is None else max(0, limit)
    if cap == 0 and limit is None:
        cap = 999999

    matching = {r.notice_id: r for r in list_contracts(session)}

    processed = 0
    screened = 0
    text_screened = 0
    enriched_only = 0
    skipped = 0
    errors: list[str] = []

    for notice_id in notice_ids:
        if processed >= cap:
            break
        row = matching.get(notice_id)
        if not row:
            continue
        if not force and not force_full and not needs_intake(row):
            skipped += 1
            continue

        try:
            result = full_intake_contract(row, force=force, force_full=force_full)
            session.commit()
            processed += 1
            if result.get("screened") or result.get("full_analysis"):
                screened += 1
            if result.get("text_screened"):
                text_screened += 1
            elif result.get("enriched") or result.get("reason") == "screen_budget":
                enriched_only += 1
            elif result.get("skipped"):
                skipped += 1
                if result.get("message"):
                    errors.append(f"{notice_id}: {result['message']}")
            if result.get("reason") == "sam_budget":
                errors.append("SAM.gov daily budget reached — remaining contracts queued for later.")
                break
            if result.get("reason") == "screen_budget":
                errors.append("Claude budget reached — text/full analysis pending.")
                break
        except ScreenBudgetExceeded:
            session.rollback()
            errors.append("Claude budget reached — remaining analysis pending.")
            break
        except Exception as exc:
            session.rollback()
            errors.append(f"{notice_id}: {exc}")

    pending = sum(
        1
        for row in matching.values()
        if row.notice_id in notice_ids and needs_intake(row)
    )

    return {
        "processed": processed,
        "screened": screened,
        "text_screened": text_screened,
        "enriched_only": enriched_only,
        "skipped": skipped,
        "errors": errors,
        "pending_from_batch": pending,
    }


def intake_pending(*, limit: int = 3, matching_only: bool = True, force: bool = False, force_full: bool = False) -> dict[str, Any]:
    from sync import list_contracts

    session = SessionLocal()
    try:
        if matching_only:
            rows = list_contracts(session)
            if not force and not force_full:
                rows = [r for r in rows if needs_intake(r)]
            rows = rows[:limit]
        else:
            query = session.query(Contract).order_by(Contract.first_seen_at.desc())
            rows = query.limit(limit).all()
            if not force and not force_full:
                rows = [r for r in rows if needs_intake(r)]

        notice_ids = [r.notice_id for r in rows]
        return intake_matching_contracts(session, notice_ids, limit=limit, force=force, force_full=force_full)
    finally:
        session.close()


def start_background_intake(batch_size: int = 3) -> None:
    """Continue two-step intake for matching contracts after sync."""
    if not intake_on_sync_enabled():
        return

    global _background_running
    with _background_lock:
        if _background_running:
            return
        _background_running = True

    def _run() -> None:
        global _background_running
        try:
            total = 0
            while intake_on_sync_enabled() and can_screen() and can_spend_sam(2):
                result = intake_pending(limit=batch_size, matching_only=True)
                total += result.get("screened", 0) + result.get("text_screened", 0)
                if result.get("processed", 0) == 0:
                    break
                if any("SAM.gov daily budget" in e for e in result.get("errors", [])):
                    break
                if any("Claude budget" in e for e in result.get("errors", [])):
                    break
            if total:
                logger.info("Background intake finished: %s contract(s) processed", total)
        except Exception:
            logger.exception("Background intake failed")
        finally:
            with _background_lock:
                _background_running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-intake").start()


def enrich_matching_attachments(
    session,
    notice_ids: list[str] | None = None,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Full scrape only for contracts queued for full PDF analysis (text score >= threshold)."""
    from sam_enrich import is_scrape_complete
    from sync import list_contracts

    matching_rows = list_contracts(session)
    if notice_ids is not None:
        id_set = set(notice_ids)
        candidates = [r for r in matching_rows if r.notice_id in id_set]
    else:
        candidates = [
            r
            for r in matching_rows
            if qualifies_for_full_analysis(r.analysis if isinstance(r.analysis, dict) else None)
            and not is_scrape_complete(r.sam_raw if isinstance(r.sam_raw, dict) else {})
        ]

    enriched = 0
    errors: list[str] = []
    for row in candidates:
        if limit is not None and enriched >= limit:
            break
        if not qualifies_for_full_analysis(row.analysis if isinstance(row.analysis, dict) else None):
            continue
        raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
        if is_scrape_complete(raw):
            continue
        if not can_spend_sam(1):
            errors.append("SAM.gov daily budget reached — full scrape pending.")
            break
        try:
            if enrich_contract_attachments(row):
                session.commit()
                enriched += 1
        except Exception as exc:
            session.rollback()
            errors.append(f"{row.notice_id}: {exc}")

    pending = sum(
        1
        for row in candidates
        if qualifies_for_full_analysis(row.analysis if isinstance(row.analysis, dict) else None)
        and not is_scrape_complete(row.sam_raw if isinstance(row.sam_raw, dict) else {})
    )

    return {
        "attachments_enriched": enriched,
        "attachments_pending": pending,
        "errors": errors,
    }


def start_background_attachment_enrich(batch_size: int = 8) -> None:
    """Full scrape for high text-score contracts awaiting PDF analysis."""
    global _attachment_running
    with _attachment_lock:
        if _attachment_running:
            return
        _attachment_running = True

    def _run() -> None:
        global _attachment_running
        try:
            total = 0
            while can_spend_sam(1):
                session = SessionLocal()
                try:
                    result = enrich_matching_attachments(session, limit=batch_size)
                    total += result.get("attachments_enriched", 0)
                    if result.get("attachments_enriched", 0) == 0:
                        break
                    if any("SAM.gov daily budget" in e for e in result.get("errors", [])):
                        break
                finally:
                    session.close()
            if total:
                logger.info("Background full scrape finished: %s contract(s)", total)
        except Exception:
            logger.exception("Background attachment enrich failed")
        finally:
            with _attachment_lock:
                _attachment_running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-attachments").start()

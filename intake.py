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
    has_attachments_ready,
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


def ensure_contract_attachments_ready(row: Contract, session) -> bool:
    """
    DB-first attachment prep for every contract (new or existing).
    Uses stored PDF bytes / attachment_text when present; SAM.gov only for first-time download.
    """
    from attachment_pipeline import ensure_attachments_from_database

    if has_attachments_ready(row, session):
        return True
    if ensure_attachments_from_database(session, row):
        return True
    if not can_spend_sam(1):
        return False
    enrich_contract_attachments(row, session=session)
    return has_attachments_ready(row, session)


def run_post_attachment_intake(row: Contract, session) -> dict[str, Any] | None:
    """
    After PDFs are saved in PostgreSQL, run the full v2 pipeline from stored data only.
    Called immediately on new downloads so we never need a separate repair pass.
    """
    from api_budget import ScreenBudgetExceeded, can_screen, claude_intake_allowed, is_anthropic_api_blocked
    from screening_pipeline import has_attachments_ready, is_full_analysis_complete, workflow_is_current

    if not has_attachments_ready(row, session):
        return None
    if not claude_intake_allowed() or not can_screen():
        return None
    from workflow_backfill_service import contract_repair_reason

    reason = contract_repair_reason(row, session)
    if not reason:
        return None
    analysis = row.analysis if isinstance(row.analysis, dict) else {}
    if is_full_analysis_complete(row.analysis, row) and workflow_is_current(analysis):
        return None
    try:
        return full_intake_contract(row, session=session, force=True, db_only=True)
    except ScreenBudgetExceeded:
        return {"notice_id": row.notice_id, "skipped": True, "reason": "screen_budget"}
    except Exception as exc:
        if is_anthropic_api_blocked(exc):
            logger.warning("Claude eval failed for %s — continuing queue: %s", row.notice_id, str(exc)[:120])
            return {
                "notice_id": row.notice_id,
                "error": "claude_api",
                "detail": str(exc)[:200],
            }
        raise


def enrich_contract_attachments(row: Contract, session=None) -> bool:
    """Load SAM scrape, download attachment bytes into PostgreSQL, extract text."""
    from attachment_pipeline import ensure_attachments_from_database, is_attachment_extraction_ready, run_attachment_pipeline
    from csv_attachment_policy import notice_id_csv_attachment_eligible
    from database import SessionLocal
    from sam_enrich import is_sam_metadata_ready, is_scrape_complete, scrape_attachment_metadata
    from sam_client import normalize_opportunity

    own_session = session is None
    if own_session:
        session = SessionLocal()

    try:
        already_ready = is_attachment_extraction_ready(row, session)
        if ensure_attachments_from_database(session, row):
            return not already_ready

        if not notice_id_csv_attachment_eligible(session, row.notice_id):
            return False

        raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
        if is_scrape_complete(raw) and is_attachment_extraction_ready(row, session):
            return False

        if is_sam_metadata_ready(raw) and not is_attachment_extraction_ready(row, session):
            run_attachment_pipeline(row, session)
            run_post_attachment_intake(row, session)
            return True

        if is_sam_metadata_ready(raw) and is_scrape_complete(raw):
            return False

        enriched, ok = scrape_attachment_metadata(raw)
        if not ok:
            row.sam_raw = enriched
            return False

        row.sam_raw = enriched
        if enriched.get("descriptionText"):
            row.description = enriched["descriptionText"][:8000]

        refreshed = normalize_opportunity(enriched)
        if refreshed.get("location"):
            row.location = refreshed["location"]

        run_attachment_pipeline(row, session)
        run_post_attachment_intake(row, session)
        return True
    finally:
        if own_session:
            session.commit()
            session.close()


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


def run_full_analysis(
    row: Contract,
    *,
    prior: dict[str, Any] | None = None,
    session=None,
    db_only: bool = False,
) -> dict[str, Any]:
    """Step 2 — PIEE/attachments + PDFs + full Claude analysis."""
    from pws_fields import apply_pws_extraction, contract_pws_missing
    from screening_pipeline import pdfs_expected_on_contract, pdfs_read_in_analysis
    from sub_finder import ensure_sub_search_before_screening, subs_context_for_screening

    prior = prior or (row.analysis if isinstance(row.analysis, dict) else {})
    text_score = text_score_from_analysis(prior)

    if db_only:
        if session is not None:
            from attachment_pipeline import ensure_attachments_from_database

            attachments_ok = ensure_attachments_from_database(session, row) or has_attachments_ready(row, session)
        else:
            s = SessionLocal()
            try:
                from attachment_pipeline import ensure_attachments_from_database

                attachments_ok = ensure_attachments_from_database(s, row) or has_attachments_ready(row, s)
                s.commit()
            finally:
                s.close()
    elif session is not None:
        attachments_ok = ensure_contract_attachments_ready(row, session)
    else:
        s = SessionLocal()
        try:
            attachments_ok = ensure_contract_attachments_ready(row, s)
            s.commit()
        finally:
            s.close()

    if not attachments_ok:
        if not can_spend_sam(1):
            return {
                "notice_id": row.notice_id,
                "skipped": True,
                "reason": "sam_budget",
                "message": "SAM.gov daily budget reached before attachments could be loaded.",
                "text_score": text_score,
            }
        return {
            "notice_id": row.notice_id,
            "skipped": True,
            "reason": "pending_attachments",
            "message": "No stored PDFs in the database and SAM download did not complete.",
            "text_score": text_score,
        }

    # Attachments are in PostgreSQL — never re-fetch from SAM for Claude / scope / subs.
    db_only = True

    subs_context: dict[str, Any] | None = None
    if session is not None:
        try:
            ensure_sub_search_before_screening(session, row, force=False)
            subs_context = subs_context_for_screening(session, row)
        except Exception:
            logger.exception("Sub pipeline before screening failed for %s", row.notice_id)
    else:
        sub_session = SessionLocal()
        try:
            ensure_sub_search_before_screening(sub_session, row, force=False)
            subs_context = subs_context_for_screening(sub_session, row)
            sub_session.commit()
        except Exception:
            logger.exception("Sub pipeline before screening failed for %s", row.notice_id)
            sub_session.rollback()
        finally:
            sub_session.close()

    if not can_screen():
        return {
            "notice_id": row.notice_id,
            "skipped": True,
            "reason": "screen_budget",
            "enriched": True,
            "message": "Claude budget reached before full PDF analysis could run.",
            "text_score": text_score,
        }

    notice_id = row.notice_id
    contract_id = row.id
    closed_session_for_claude = False

    if db_only and session is not None:
        session.commit()
        session.close()
        session = None
        closed_session_for_claude = True
    elif session is not None:
        session.commit()

    if closed_session_for_claude:
        from attachment_storage import load_contract_for_repair
        from database import with_db_retry

        def _reload_row() -> Contract | None:
            s = SessionLocal()
            try:
                reloaded = load_contract_for_repair(s, contract_id) if contract_id else None
                if reloaded is None:
                    reloaded = s.query(Contract).filter_by(notice_id=notice_id).first()
                if reloaded is not None:
                    s.expunge(reloaded)
                return reloaded
            finally:
                s.close()

        row = with_db_retry(_reload_row)
        if row is None:
            raise ValueError(f"Contract not found: {notice_id}")

    analysis = screen_contract(row, subs_context=subs_context, db_only=db_only, session=None)
    if not record_screen_usage():
        raise ScreenBudgetExceeded()

    if closed_session_for_claude:
        session = SessionLocal()
        row = session.merge(row)

    if text_score is not None:
        analysis["text_score"] = text_score
    if prior.get("text_reason") and not analysis.get("text_reason"):
        analysis["text_reason"] = prior["text_reason"]
    if prior.get("text_screened_at"):
        analysis["text_screened_at"] = prior["text_screened_at"]

    row.analysis = analysis

    from claude_client import contract_attachment_text
    from pws_fields import supplement_pws_from_pdf_text

    apply_pws_extraction(row, analysis)

    def _persist_scope_from_pdfs() -> None:
        nonlocal analysis
        from attachment_pipeline import run_attachment_pipeline
        from database import SessionLocal

        stored_text = str(getattr(row, "attachment_text", None) or "").strip()
        if stored_text:
            full_text = stored_text
        else:
            if session is not None:
                run_attachment_pipeline(row, session, db_only=db_only)
            else:
                s = SessionLocal()
                try:
                    run_attachment_pipeline(row, s, db_only=db_only)
                    s.commit()
                finally:
                    s.close()
            full_text = contract_attachment_text(row, max_pdfs=12, session=session, db_only=db_only)

        supplement_pws_from_pdf_text(analysis, full_text)
        apply_pws_extraction(row, analysis)
        if row.square_footage is None and can_screen() and not db_only:
            from claude_client import try_extract_sqft_from_drawings

            if try_extract_sqft_from_drawings(row, analysis, db_only=db_only, session=session):
                apply_pws_extraction(row, analysis)
            record_screen_usage()
        row.analysis = analysis

    if contract_pws_missing(row):
        _persist_scope_from_pdfs()

    from prior_contract_extract import (
        ensure_prior_contract_from_pdfs,
        merge_prior_contract_hints,
        prior_contract_hints_complete,
        refresh_pricing_after_pdf_extract,
    )

    merge_prior_contract_hints(row)
    if not prior_contract_hints_complete(row.analysis if isinstance(row.analysis, dict) else {}):
        try:
            if not db_only:
                ensure_prior_contract_from_pdfs(row, session, force=False)
        except Exception:
            pass
    else:
        merge_prior_contract_hints(row)

    try:
        refresh_pricing_after_pdf_extract(row)
    except Exception:
        pass

    if session is not None and contract_pws_missing(row) and not db_only:
        from proposal_service import ensure_solicitation_meta

        ensure_solicitation_meta(session, row, force=True)
        analysis = row.analysis if isinstance(row.analysis, dict) else analysis
        _persist_scope_from_pdfs()
        merge_prior_contract_hints(row)
        try:
            refresh_pricing_after_pdf_extract(row)
        except Exception:
            pass

    if analysis.get("estimated_value") and not row.estimated_value:
        row.estimated_value = str(analysis["estimated_value"])[:128]

    if pdfs_expected_on_contract(row) and contract_pws_missing(row):
        analysis["screening_stage"] = "pdf_pending"
        analysis["pdf_pending_reason"] = "Attachments not fully processed — automatic retry queued."
        row.analysis = analysis
        row.status = "reviewing"
        full_done = False
    elif contract_pws_missing(row):
        analysis["screening_stage"] = "pdf_pending"
        analysis["pdf_pending_reason"] = "Scope extraction incomplete — automatic retry queued."
        row.analysis = analysis
        row.status = "reviewing"
        full_done = False
    else:
        finalize_full_analysis(row, analysis)
        full_done = True
        from submission_package import apply_submission_package

        apply_submission_package(row, session, analysis=analysis)

    if closed_session_for_claude and session is not None:
        session.commit()
        session.close()

    return {
        "notice_id": notice_id,
        "skipped": False,
        "enriched": True,
        "screened": full_done,
        "full_analysis": full_done,
        "pdf_pending": not full_done,
        "text_score": text_score,
        "pdfs_sent": analysis.get("pdfs_sent_to_claude", 0),
        "analysis": analysis,
    }


def run_scope_extraction(row: Contract, session) -> dict[str, Any]:
    """Backfill PWS scope + solicitation meta + prior contract fields from PDFs."""
    from pws_fields import contract_pws_missing
    from prior_contract_extract import merge_prior_contract_hints, prior_contract_hints_complete, refresh_pricing_after_pdf_extract
    from proposal_service import ensure_solicitation_meta

    if not has_attachments_ready(row):
        return {
            "notice_id": row.notice_id,
            "skipped": True,
            "reason": "pending_attachments",
        }

    merge_prior_contract_hints(row)
    needs_scope = contract_pws_missing(row)
    needs_prior = not prior_contract_hints_complete(row.analysis if isinstance(row.analysis, dict) else {})
    if not needs_scope and not needs_prior:
        return {"notice_id": row.notice_id, "skipped": True, "reason": "scope_complete"}

    if not can_screen():
        return {
            "notice_id": row.notice_id,
            "skipped": True,
            "reason": "screen_budget",
            "message": "Claude budget reached before PWS scope could be extracted.",
        }

    ensure_solicitation_meta(session, row, force=True)
    merge_prior_contract_hints(row)
    try:
        refresh_pricing_after_pdf_extract(row)
    except Exception:
        pass
    row.last_updated_at = datetime.now(timezone.utc)
    return {
        "notice_id": row.notice_id,
        "skipped": False,
        "scope_extracted": True,
        "square_footage": row.square_footage,
        "cleaning_frequency_per_week": float(row.cleaning_frequency_per_week)
        if row.cleaning_frequency_per_week is not None
        else None,
        "prior_contract_extracted": prior_contract_hints_complete(row.analysis if isinstance(row.analysis, dict) else {}),
    }


def full_intake_contract(
    row: Contract,
    *,
    session=None,
    force: bool = False,
    force_full: bool = False,
    db_only: bool = False,
) -> dict[str, Any]:
    """Run full Claude analysis once SAM attachments are complete (drives dashboard ranking)."""
    from pws_fields import contract_pws_missing

    from screening_pipeline import workflow_is_current

    analysis = row.analysis if isinstance(row.analysis, dict) else {}
    stale_workflow = not workflow_is_current(analysis)
    if is_full_analysis_complete(row.analysis, row) and not force and not force_full and not stale_workflow:
        if session is not None and contract_pws_missing(row):
            return run_scope_extraction(row, session)
        return {"notice_id": row.notice_id, "skipped": True, "reason": "already_analyzed"}

    if not _try_begin_intake(row.notice_id):
        return {"notice_id": row.notice_id, "in_progress": True}

    try:
        if not has_attachments_ready(row, session):
            return {
                "notice_id": row.notice_id,
                "skipped": True,
                "reason": "pending_attachments",
                "message": "Waiting for attachment PDFs in the database (or first-time SAM download).",
            }

        analysis = row.analysis if isinstance(row.analysis, dict) else {}
        return run_full_analysis(row, prior=analysis, session=session, db_only=db_only)
    finally:
        _end_intake(row.notice_id)


def force_full_analysis_contract(row: Contract, *, session=None) -> dict[str, Any]:
    """Manual override — run full PDF analysis regardless of text score."""
    if not _try_begin_intake(row.notice_id):
        return {"notice_id": row.notice_id, "in_progress": True}
    own_session = False
    if session is None:
        session = SessionLocal()
        own_session = True
    try:
        if own_session:
            bound = session.query(Contract).filter_by(notice_id=row.notice_id).first()
            if bound is not None:
                row = bound
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
        return run_full_analysis(row, prior=analysis, session=session)
    finally:
        _end_intake(row.notice_id)
        if own_session:
            try:
                if session.is_active:
                    session.commit()
            except Exception:
                pass
            session.close()


def intake_matching_contracts(
    session,
    notice_ids: list[str],
    *,
    limit: int | None = None,
    force: bool = False,
    force_full: bool = False,
) -> dict[str, Any]:
    """Run Claude full analysis for filter-matching contracts with attachments ready."""
    from api_budget import claude_intake_allowed
    from sync import list_contracts

    if not force and not force_full and not claude_intake_allowed():
        return {"processed": 0, "screened": 0, "text_screened": 0, "enriched_only": 0, "skipped": 0, "errors": []}

    if not intake_on_sync_enabled() and not force and not force_full:
        from pws_fields import contract_pws_missing

        scope_rows = [r for r in matching.values() if contract_pws_missing(r)]
        if not scope_rows:
            return {"processed": 0, "screened": 0, "text_screened": 0, "enriched_only": 0, "skipped": 0, "errors": []}
        matching = {r.notice_id: r for r in scope_rows}
        cap = min(cap, 5)

    cap = intake_per_sync_limit() if limit is None else max(0, limit)
    if cap is None:
        cap = 999999

    id_set = set(notice_ids) if notice_ids else None
    matching = {
        r.notice_id: r
        for r in list_contracts(session, require_dashboard_ready=False, require_scrape_complete=True)
        if id_set is None or r.notice_id in id_set
    }

    processed = 0
    screened = 0
    text_screened = 0
    enriched_only = 0
    skipped = 0
    errors: list[str] = []

    ids_to_process = notice_ids if notice_ids else list(matching.keys())
    for notice_id in ids_to_process:
        if processed >= cap:
            break
        row = matching.get(notice_id)
        if not row:
            continue
        if not force and not force_full and not needs_intake(row):
            skipped += 1
            continue

        try:
            result = full_intake_contract(row, session=session, force=force, force_full=force_full)
            session.commit()
            processed += 1
            if result.get("screened") or result.get("full_analysis"):
                screened += 1
            elif result.get("scope_extracted"):
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
                errors.append(f"{notice_id}: Claude budget reached — skipped, continuing queue.")
                continue
        except ScreenBudgetExceeded:
            session.rollback()
            errors.append(f"{notice_id}: Claude budget reached — skipped, continuing queue.")
            continue
        except Exception as exc:
            session.rollback()
            errors.append(f"{notice_id}: {exc}")

    pending = sum(1 for row in matching.values() if needs_intake(row))

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
            rows = list_contracts(session, require_dashboard_ready=False, require_scrape_complete=True)
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


def start_background_intake(batch_size: int = 8) -> None:
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
            from api_budget import can_spend_sam
            from workflow_backfill_service import run_workflow_repair_batch

            total = 0
            while intake_on_sync_enabled():
                repair = run_workflow_repair_batch(limit=batch_size)
                total += repair.get("repaired", 0)

                enrich_session = SessionLocal()
                try:
                    from intake import enrich_matching_attachments

                    enrich_matching_attachments(enrich_session, limit=batch_size)
                finally:
                    enrich_session.close()

                result = intake_pending(limit=batch_size, matching_only=True)
                total += result.get("screened", 0) + result.get("text_screened", 0)

                if repair.get("processed", 0) == 0 and result.get("processed", 0) == 0:
                    break
                if any("SAM.gov daily budget" in e for e in result.get("errors", [])):
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
    naics_code: str | None = None,
    skip_notice_ids: set[str] | frozenset[str] | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Backfill SAM attachments for filter-matching contracts not yet scrape-complete."""
    from api_budget import claude_intake_allowed
    from screening_pipeline import has_attachments_ready
    from sync import list_attachment_backlog

    skip = skip_notice_ids or set()
    candidates = [
        row
        for row in list_attachment_backlog(session, notice_ids=notice_ids)
        if row.notice_id not in skip and not has_attachments_ready(row)
    ]
    if naics_code:
        candidates.sort(key=lambda row: 0 if row.naics_code == naics_code else 1)

    enriched = 0
    attempts = 0
    last_attempted_notice_id: str | None = None
    errors: list[str] = []
    for row in candidates:
        if limit is not None and enriched >= limit:
            break
        if max_attempts is not None and attempts >= max_attempts:
            break
        if has_attachments_ready(row):
            continue
        attempts += 1
        last_attempted_notice_id = row.notice_id
        try:
            from attachment_pipeline import ensure_attachments_from_database

            if ensure_attachments_from_database(session, row):
                session.commit()
                enriched += 1
                if claude_intake_allowed():
                    intake_result = run_post_attachment_intake(row, session)
                    if intake_result and intake_result.get("error"):
                        session.rollback()
                        errors.append(f"{row.notice_id}: {intake_result.get('error')}")
                    else:
                        session.commit()
                continue
            if not can_spend_sam(1):
                errors.append("SAM.gov daily budget reached — full scrape pending.")
                break
            if enrich_contract_attachments(row, session=session):
                session.commit()
                enriched += 1
                if claude_intake_allowed():
                    intake_result = run_post_attachment_intake(row, session)
                    if intake_result and intake_result.get("error"):
                        session.rollback()
                        errors.append(f"{row.notice_id}: {intake_result.get('error')}")
                    else:
                        session.commit()
            else:
                session.commit()
        except Exception as exc:
            session.rollback()
            errors.append(f"{row.notice_id}: {exc}")

    pending = sum(1 for row in candidates if not has_attachments_ready(row))

    return {
        "attachments_enriched": enriched,
        "attachments_pending": pending,
        "attempts": attempts,
        "last_attempted_notice_id": last_attempted_notice_id,
        "errors": errors,
    }


def start_background_attachment_enrich(batch_size: int = 8) -> None:
    """Continue full scrape for matching contracts that still need attachments."""
    global _attachment_running
    with _attachment_lock:
        if _attachment_running:
            return
        _attachment_running = True

    def _run() -> None:
        global _attachment_running
        try:
            from csv_attachment_policy import sam_attachments_csv_only
            from settings_store import get_naics_codes
            from sync import burn_sam_budget_on_attachments

            pool = get_naics_codes()
            if not pool and not sam_attachments_csv_only():
                return
            try:
                result = burn_sam_budget_on_attachments(pool or [])
            except ValueError:
                return
            total = result.get("attachments_enriched", 0)
            if total:
                logger.info(
                    "Background attachment pull: %s contract(s) with PDFs, %s SAM call(s)",
                    total,
                    result.get("sam_calls_burned", 0),
                )
                start_background_intake()
        except Exception:
            logger.exception("Background attachment enrich failed")
        finally:
            with _attachment_lock:
                _attachment_running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-attachments").start()

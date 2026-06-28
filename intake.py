"""Full contract intake: SAM description + attachments → Claude reads PDFs → summary saved."""

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
from claude_client import screen_contract
from database import SessionLocal
from models import Contract

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
    """Fetch full posting description and attachment list from SAM.gov."""
    return enrich_contract_attachments(row)


def full_intake_contract(row: Contract, *, force: bool = False) -> dict[str, Any]:
    """
    Complete intake for one contract:
    1. Read full SAM.gov posting description
    2. Fetch attachment list and download PDFs (inside screen_contract)
    3. Claude writes plain-English summary + screening verdict
    """
    if row.analysis and not force:
        return {"notice_id": row.notice_id, "skipped": True, "reason": "already_analyzed"}

    if not _try_begin_intake(row.notice_id):
        return {"notice_id": row.notice_id, "in_progress": True}

    try:
        from sam_enrich import is_scrape_complete

        enriched = enrich_contract_from_sam(row)
        if not enriched and not is_scrape_complete(row.sam_raw if isinstance(row.sam_raw, dict) else {}):
            return {
                "notice_id": row.notice_id,
                "skipped": True,
                "reason": "sam_budget",
                "message": "SAM.gov daily budget reached before description/attachments could be loaded.",
            }

        if not can_screen():
            return {
                "notice_id": row.notice_id,
                "skipped": True,
                "reason": "screen_budget",
                "enriched": True,
                "message": "Description and attachments saved; Claude daily budget reached before summary could be written.",
            }

        analysis = screen_contract(row)
        if not record_screen_usage():
            raise ScreenBudgetExceeded()

        row.analysis = analysis
        row.last_updated_at = datetime.now(timezone.utc)

        from pws_fields import apply_pws_extraction

        apply_pws_extraction(row, analysis)

        if analysis.get("pursue") is False:
            row.status = "skipped"
        elif row.status == "new":
            row.status = "reviewing"
        if analysis.get("estimated_value") and not row.estimated_value:
            row.estimated_value = str(analysis["estimated_value"])[:128]

        from sub_finder import maybe_auto_sub_search

        maybe_auto_sub_search(row)

        return {
            "notice_id": row.notice_id,
            "skipped": False,
            "enriched": True,
            "screened": True,
            "pdfs_sent": analysis.get("pdfs_sent_to_claude", 0),
            "analysis": analysis,
        }
    finally:
        _end_intake(row.notice_id)


def intake_matching_contracts(
    session,
    notice_ids: list[str],
    *,
    limit: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """
    Run full intake for synced contracts that pass dashboard filters and lack a summary.
    Order: description → attachments → PDFs → Claude summary.
    """
    from sync import list_contracts

    if not intake_on_sync_enabled() and not force:
        return {"processed": 0, "screened": 0, "enriched_only": 0, "skipped": 0, "errors": []}

    cap = intake_per_sync_limit() if limit is None else max(0, limit)
    matching = {r.notice_id: r for r in list_contracts(session)}

    processed = 0
    screened = 0
    enriched_only = 0
    skipped = 0
    errors: list[str] = []

    for notice_id in notice_ids:
        if processed >= cap:
            break
        row = matching.get(notice_id)
        if not row:
            continue
        if row.analysis and not force:
            skipped += 1
            continue

        try:
            result = full_intake_contract(row, force=force)
            session.commit()
            processed += 1
            if result.get("screened"):
                screened += 1
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
                errors.append("Claude daily budget reached — descriptions saved; summaries pending.")
                break
        except ScreenBudgetExceeded:
            session.rollback()
            errors.append("Claude daily budget reached — remaining summaries pending.")
            break
        except Exception as exc:
            session.rollback()
            errors.append(f"{notice_id}: {exc}")

    pending = sum(
        1
        for row in matching.values()
        if not row.analysis and row.notice_id in notice_ids
    )

    return {
        "processed": processed,
        "screened": screened,
        "enriched_only": enriched_only,
        "skipped": skipped,
        "errors": errors,
        "pending_from_batch": pending,
    }


def intake_pending(*, limit: int = 3, matching_only: bool = True, force: bool = False) -> dict[str, Any]:
    """Intake unscreened contracts (matching dashboard filters by default)."""
    from sync import list_contracts

    session = SessionLocal()
    try:
        if matching_only:
            rows = list_contracts(session)
            if not force:
                rows = [r for r in rows if not r.analysis]
            rows = rows[:limit]
        else:
            query = session.query(Contract).order_by(Contract.first_seen_at.desc())
            if not force:
                query = query.filter(Contract.analysis.is_(None))
            rows = query.limit(limit).all()

        notice_ids = [r.notice_id for r in rows]
        return intake_matching_contracts(session, notice_ids, limit=limit, force=force)
    finally:
        session.close()


def start_background_intake(batch_size: int = 3) -> None:
    """Continue intake for matching unscreened contracts after sync (respects daily budgets)."""
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
                total += result.get("screened", 0)
                if result.get("processed", 0) == 0:
                    break
                if any("SAM.gov daily budget" in e for e in result.get("errors", [])):
                    break
                if any("Claude daily budget" in e for e in result.get("errors", [])):
                    break
            if total:
                logger.info("Background intake finished: %s contract(s) fully analyzed", total)
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
    """Backfill incomplete scrapes for matching contracts (no Claude)."""
    from sam_enrich import is_scrape_complete
    from sync import list_contracts

    matching_rows = list_contracts(session)
    if notice_ids is not None:
        id_set = set(notice_ids)
        candidates = [r for r in matching_rows if r.notice_id in id_set]
    else:
        candidates = matching_rows

    enriched = 0
    errors: list[str] = []
    for row in candidates:
        if limit is not None and enriched >= limit:
            break
        raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
        if is_scrape_complete(raw):
            continue
        if not can_spend_sam(1):
            errors.append("SAM.gov daily budget reached — incomplete contracts pending.")
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
        if not is_scrape_complete(row.sam_raw if isinstance(row.sam_raw, dict) else {})
    )

    return {
        "attachments_enriched": enriched,
        "attachments_pending": pending,
        "errors": errors,
    }


def start_background_attachment_enrich(batch_size: int = 8) -> None:
    """Load missing SAM.gov attachment lists for matching dashboard contracts."""
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
                    result = enrich_matching_attachments(session, limit=None)
                    total += result.get("attachments_enriched", 0)
                    if result.get("attachments_enriched", 0) == 0:
                        break
                    if any("SAM.gov daily budget" in e for e in result.get("errors", [])):
                        break
                finally:
                    session.close()
            if total:
                logger.info("Background attachment enrich finished: %s contract(s)", total)
        except Exception:
            logger.exception("Background attachment enrich failed")
        finally:
            with _attachment_lock:
                _attachment_running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-attachments").start()

"""Screen contracts with Claude and save analysis to PostgreSQL."""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from typing import Any

from api_budget import ScreenBudgetExceeded, can_screen, record_screen_usage
from claude_client import screen_contract
from database import SessionLocal
from models import Contract

logger = logging.getLogger("govtracker.screening")
_background_lock = threading.Lock()
_background_running = False
_screening_ids: set[str] = set()
_screening_ids_lock = threading.Lock()


def _try_begin_screening(notice_id: str) -> bool:
    with _screening_ids_lock:
        if notice_id in _screening_ids:
            return False
        _screening_ids.add(notice_id)
        return True


def _end_screening(notice_id: str) -> None:
    with _screening_ids_lock:
        _screening_ids.discard(notice_id)


def screen_pending(limit: int = 5, force: bool = False, matching_only: bool = False) -> dict[str, Any]:
    """
    Screen contracts without analysis.
    If matching_only=True, only screen contracts that pass current dashboard filters.
    """
    from sync import list_contracts

    session = SessionLocal()
    screened = 0
    skipped = 0
    errors: list[str] = []

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

        for row in rows:
            if row.analysis and not force:
                skipped += 1
                continue
            if not _try_begin_screening(row.notice_id):
                skipped += 1
                continue
            if not can_screen():
                errors.append("Daily Claude screening budget reached — try again tomorrow or raise ANTHROPIC_DAILY_SCREEN_BUDGET.")
                break
            try:
                analysis = screen_contract(row)
                if not record_screen_usage():
                    session.rollback()
                    errors.append("Daily Claude screening budget reached while saving usage.")
                    break
                row.analysis = analysis
                row.last_updated_at = datetime.now(timezone.utc)
                if analysis.get("pursue") is False:
                    row.status = "skipped"
                elif row.status == "new":
                    row.status = "reviewing"
                if analysis.get("estimated_value") and not row.estimated_value:
                    row.estimated_value = str(analysis["estimated_value"])[:128]
                session.commit()
                screened += 1
            except Exception as exc:
                session.rollback()
                errors.append(f"{row.notice_id}: {exc}")
            finally:
                _end_screening(row.notice_id)

        pending = session.query(Contract).filter(Contract.analysis.is_(None)).count()
    finally:
        session.close()

    return {
        "screened": screened,
        "skipped_existing": skipped,
        "errors": errors,
        "pending_remaining": pending,
    }


def screen_one(notice_id: str, force: bool = False) -> dict[str, Any]:
    session = SessionLocal()
    try:
        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise ValueError(f"Contract not found: {notice_id}")
        if row.analysis and not force:
            return {"notice_id": notice_id, "skipped": True, "analysis": row.analysis}
        if not _try_begin_screening(notice_id):
            return {"notice_id": notice_id, "in_progress": True}

        try:
            if not can_screen():
                raise ScreenBudgetExceeded()

            from sam_enrich import ensure_enriched_sam_raw
            from sam_client import normalize_opportunity

            ensure_enriched_sam_raw(row)
            if isinstance(row.sam_raw, dict):
                if row.sam_raw.get("descriptionText"):
                    row.description = row.sam_raw["descriptionText"][:8000]
                refreshed = normalize_opportunity(row.sam_raw)
                if refreshed.get("location"):
                    row.location = refreshed["location"]

            analysis = screen_contract(row)
            if not record_screen_usage():
                raise ScreenBudgetExceeded()
            row.analysis = analysis
            row.last_updated_at = datetime.now(timezone.utc)
            if analysis.get("pursue") is False:
                row.status = "skipped"
            elif row.status in ("new", "skipped"):
                row.status = "reviewing"
            if analysis.get("estimated_value") and not row.estimated_value:
                row.estimated_value = str(analysis["estimated_value"])[:128]
            session.commit()
            return {"notice_id": notice_id, "skipped": False, "analysis": analysis}
        finally:
            _end_screening(notice_id)
    finally:
        session.close()


def start_background_screening(batch_size: int = 5) -> None:
    """Screen unscreened contracts in a background thread (opt-in via AUTO_SCREEN_ON_STARTUP)."""
    from api_budget import auto_screen_on_startup

    if not auto_screen_on_startup():
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
            while True:
                if not can_screen():
                    logger.info("Background screening stopped: daily Claude budget reached")
                    break
                result = screen_pending(limit=batch_size, matching_only=False)
                total += result.get("screened", 0)
                if result.get("screened", 0) == 0:
                    break
                if result.get("pending_remaining", 0) == 0:
                    break
            if total:
                logger.info("Background screening finished: %s contract(s) analyzed", total)
        except Exception:
            logger.exception("Background screening failed")
        finally:
            with _background_lock:
                _background_running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-screen").start()


def main() -> None:
    limit = 5
    force = "--force" in sys.argv
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        limit = int(sys.argv[idx + 1])

    args = [a for a in sys.argv[1:] if not a.startswith("-") and a != str(limit)]
    if args:
        result = screen_one(args[0], force=force)
        print(json.dumps(result, indent=2))
        return

    matching_only = "--all" not in sys.argv
    result = screen_pending(limit=limit, force=force, matching_only=matching_only)
    print(f"Screened {result['screened']} contract(s).")
    print(f"{result['pending_remaining']} still waiting for screening.")
    if result["errors"]:
        print("Errors:")
        for err in result["errors"]:
            print(f"  - {err}")


if __name__ == "__main__":
    main()

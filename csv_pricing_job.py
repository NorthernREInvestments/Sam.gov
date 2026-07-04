"""Background USAspending pricing refresh for gt_csv_opportunities."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from database import SessionLocal
from models import AppSetting

logger = logging.getLogger("govtracker.csv_pricing_job")

CSV_PRICING_JOB_KEY = "csv_pricing_job_state"
_STALE_SECONDS = 3 * 60 * 60

_lock = threading.Lock()


def _default_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "error": None,
        "result": None,
        "started_at": None,
        "updated_at": None,
        "finished_at": None,
        "processed": 0,
        "total": 0,
        "message": None,
        "filters": None,
    }


def _serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    out = dict(state)
    for key in ("started_at", "updated_at", "finished_at"):
        val = out.get(key)
        if isinstance(val, datetime):
            out[key] = val.isoformat()
    return out


def _parse_state(raw: dict[str, Any]) -> dict[str, Any]:
    state = _default_state()
    state.update({k: v for k, v in raw.items() if k in state})
    return state


def _load_state_from_db() -> dict[str, Any]:
    session = SessionLocal()
    try:
        row = session.get(AppSetting, CSV_PRICING_JOB_KEY)
        if not row or not row.value:
            return _default_state()
        data = json.loads(row.value)
        if not isinstance(data, dict):
            return _default_state()
        return _parse_state(data)
    except Exception:
        logger.exception("Failed to load CSV pricing job state")
        return _default_state()
    finally:
        session.close()


def _save_state_to_db(state: dict[str, Any]) -> None:
    session = SessionLocal()
    try:
        payload = json.dumps(_serialize_state(state))
        row = session.get(AppSetting, CSV_PRICING_JOB_KEY)
        if row:
            row.value = payload
        else:
            session.add(AppSetting(key=CSV_PRICING_JOB_KEY, value=payload))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Failed to save CSV pricing job state")
    finally:
        session.close()


def _processing_is_stale(state: dict[str, Any]) -> bool:
    if state.get("status") != "processing":
        return False
    updated_raw = state.get("updated_at") or state.get("started_at")
    if not updated_raw:
        return True
    try:
        updated = datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - updated).total_seconds() > _STALE_SECONDS


def _get_state() -> dict[str, Any]:
    state = _load_state_from_db()
    if _processing_is_stale(state):
        state.update(
            status="failed",
            error="CSV pricing job interrupted — try Refresh CSV Pricing again.",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        _save_state_to_db(state)
    return state


def csv_pricing_job_status() -> dict[str, Any]:
    with _lock:
        return _serialize_state(_get_state())


def _update_progress(*, processed: int, total: int, notice_id: str | None = None) -> None:
    with _lock:
        state = _load_state_from_db()
        if state.get("status") != "processing":
            return
        state["processed"] = processed
        state["total"] = total
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        state["message"] = f"Pricing {processed} of {total}…"
        if notice_id:
            state["last_notice_id"] = notice_id
        _save_state_to_db(state)


def start_csv_pricing_job(
    *,
    state: str | None = None,
    days_bucket: str | None = None,
    naics_code: str | None = None,
    keyword: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run USAspending pricing for filtered CSV rows in a background thread."""
    filters = {
        "state": state,
        "days": days_bucket,
        "naics": naics_code,
        "q": keyword,
    }

    with _lock:
        current = _get_state()
        if current.get("status") == "processing":
            return {"ok": False, "error": "CSV pricing refresh already running."}

        session = SessionLocal()
        try:
            from csv_pricing_service import csv_opportunity_ids_for_filters

            row_ids = csv_opportunity_ids_for_filters(
                session,
                state=state,
                days_bucket=days_bucket,
                naics_code=naics_code,
                keyword=keyword,
                force=force,
            )
        finally:
            session.close()

        if not row_ids:
            if force:
                detail = "No CSV opportunities match the current filters."
            else:
                detail = "All matching CSV opportunities already have pricing — use force to re-run."
            return {"ok": False, "error": detail}

        matched = len(row_ids)
        resume_note = ""
        if not force:
            resume_note = " (resuming — skipping rows already priced)"

        now = datetime.now(timezone.utc).isoformat()
        _save_state_to_db(
            {
                **_default_state(),
                "status": "processing",
                "started_at": now,
                "updated_at": now,
                "processed": 0,
                "total": matched,
                "message": f"Pricing 0 of {matched}…{resume_note}",
                "filters": filters,
            }
        )

    def _run() -> None:
        session = SessionLocal()
        try:
            from csv_pricing_service import run_csv_pricing_batch

            result = run_csv_pricing_batch(
                session,
                row_ids,
                progress_callback=lambda **kwargs: _update_progress(**kwargs),
            )
            _save_state_to_db(
                {
                    **_load_state_from_db(),
                    "status": "complete",
                    "error": None,
                    "result": result,
                    "processed": result.get("processed", 0),
                    "total": result.get("total", 0),
                    "message": (
                        f"Pricing complete — {result.get('found_prior', 0)} prior matches, "
                        f"{result.get('found_regional', 0)} regional averages"
                    ),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            logger.info(
                "CSV pricing batch done: processed=%s prior=%s regional=%s errors=%s",
                result.get("processed"),
                result.get("found_prior"),
                result.get("found_regional"),
                result.get("errors"),
            )
        except Exception as exc:
            logger.exception("CSV pricing batch failed")
            _save_state_to_db(
                {
                    **_load_state_from_db(),
                    "status": "failed",
                    "error": str(exc),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        finally:
            session.close()

    threading.Thread(target=_run, daemon=True, name="govtracker-csv-pricing").start()
    return {
        "ok": True,
        "status": "processing",
        "total": matched,
        "message": f"Pricing 0 of {matched}…{resume_note}",
    }

"""Background SAM CSV import — avoids HTTP timeout on large uploads."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from database import SessionLocal
from models import AppSetting

logger = logging.getLogger("govtracker.csv_upload")

CSV_UPLOAD_JOB_KEY = "csv_upload_job_state"
_STALE_PROCESSING_HOURS = 3

_lock = threading.Lock()


def _default_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "error": None,
        "result": None,
        "started_at": None,
        "finished_at": None,
    }


def _serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    out = dict(state)
    for key in ("started_at", "finished_at"):
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
        row = session.get(AppSetting, CSV_UPLOAD_JOB_KEY)
        if not row or not row.value:
            return _default_state()
        data = json.loads(row.value)
        if not isinstance(data, dict):
            return _default_state()
        return _parse_state(data)
    except Exception:
        logger.exception("Failed to load CSV upload job state")
        return _default_state()
    finally:
        session.close()


def _save_state_to_db(state: dict[str, Any]) -> None:
    session = SessionLocal()
    try:
        payload = json.dumps(_serialize_state(state))
        row = session.get(AppSetting, CSV_UPLOAD_JOB_KEY)
        if row:
            row.value = payload
        else:
            session.add(AppSetting(key=CSV_UPLOAD_JOB_KEY, value=payload))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Failed to save CSV upload job state")
    finally:
        session.close()


def _processing_is_stale(state: dict[str, Any]) -> bool:
    if state.get("status") != "processing":
        return False
    started_raw = state.get("started_at")
    if not started_raw:
        return True
    try:
        started = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    age = datetime.now(timezone.utc) - started.astimezone(timezone.utc)
    return age.total_seconds() > _STALE_PROCESSING_HOURS * 3600


def _get_state() -> dict[str, Any]:
    state = _load_state_from_db()
    if _processing_is_stale(state):
        state.update(
            status="failed",
            error="Import interrupted by server restart — check watchlist sections or upload again.",
            result=None,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        _save_state_to_db(state)
    return state


def _set_state(**updates: Any) -> dict[str, Any]:
    state = _get_state()
    state.update(updates)
    _save_state_to_db(state)
    return state


def csv_upload_status() -> dict[str, Any]:
    with _lock:
        return _serialize_state(_get_state())


def start_csv_upload_job(content: bytes, *, process_attachments: bool = False) -> dict[str, Any]:
    """Parse and import CSV in a background thread; return immediately."""
    with _lock:
        current = _get_state()
        if current.get("status") == "processing" and not _processing_is_stale(current):
            return {"ok": False, "error": "CSV import already running — wait for it to finish."}
        _set_state(
            status="processing",
            error=None,
            result=None,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
        )

    def _run() -> None:
        session = SessionLocal()
        try:
            from csv_watchlist_service import run_full_csv_upload_pipeline

            result = run_full_csv_upload_pipeline(
                session,
                content,
                process_attachments=process_attachments,
            )
            if result.get("ok"):
                _set_state(
                    status="complete",
                    result=result,
                    error=None,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                logger.info(
                    "CSV background import finished: imported=%s watchlist_high=%s",
                    result.get("records_imported"),
                    result.get("high_confidence_matches"),
                )
            else:
                _set_state(
                    status="failed",
                    result=None,
                    error=result.get("error") or "Import failed",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
        except Exception as exc:
            session.rollback()
            logger.exception("CSV background import failed")
            _set_state(
                status="failed",
                result=None,
                error=str(exc),
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            session.close()

    threading.Thread(target=_run, daemon=True, name="govtracker-csv-upload").start()
    return {
        "ok": True,
        "status": "processing",
        "message": "Import running in the background — this may take several minutes for large files.",
    }

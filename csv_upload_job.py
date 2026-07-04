"""Background SAM CSV import — avoids HTTP timeout on large uploads."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from database import SessionLocal

logger = logging.getLogger("govtracker.csv_upload")

_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "idle",
    "error": None,
    "result": None,
    "started_at": None,
    "finished_at": None,
}


def csv_upload_status() -> dict[str, Any]:
    with _lock:
        out = dict(_state)
    if out.get("started_at"):
        out["started_at"] = out["started_at"].isoformat()
    if out.get("finished_at"):
        out["finished_at"] = out["finished_at"].isoformat()
    return out


def start_csv_upload_job(content: bytes, *, process_attachments: bool = False) -> dict[str, Any]:
    """Parse and import CSV in a background thread; return immediately."""
    with _lock:
        if _state["status"] == "processing":
            return {"ok": False, "error": "CSV import already running — wait for it to finish."}
        _state.update(
            status="processing",
            error=None,
            result=None,
            started_at=datetime.now(timezone.utc),
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
            with _lock:
                if result.get("ok"):
                    _state["status"] = "complete"
                    _state["result"] = result
                    _state["error"] = None
                else:
                    _state["status"] = "failed"
                    _state["result"] = None
                    _state["error"] = result.get("error") or "Import failed"
                _state["finished_at"] = datetime.now(timezone.utc)
            logger.info(
                "CSV background import finished: status=%s imported=%s",
                _state["status"],
                (result or {}).get("records_imported"),
            )
        except Exception as exc:
            session.rollback()
            logger.exception("CSV background import failed")
            with _lock:
                _state["status"] = "failed"
                _state["result"] = None
                _state["error"] = str(exc)
                _state["finished_at"] = datetime.now(timezone.utc)
        finally:
            session.close()

    threading.Thread(target=_run, daemon=True, name="govtracker-csv-upload").start()
    return {
        "ok": True,
        "status": "processing",
        "message": "Import running in the background — this may take several minutes for large files.",
    }

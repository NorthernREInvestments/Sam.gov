"""Persisted job state under M3_DATA_ROOT/jobs/<family>/."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JobLeaseError(RuntimeError):
    pass


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: Any) -> None:
    """Persist JSON durably.

    Prefer same-directory temp + replace. Some Railway volume mounts intermittently
    fail Path.replace on ``*.json.tmp`` (FileNotFoundError on the temp source) —
    fall back to a direct write so heartbeats/checkpoints cannot kill the worker.
    """
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, default=str)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except OSError:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        # Direct overwrite — still durable enough for checkpoint/heartbeat continuity.
        path.write_text(raw, encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class JobStore:
    """Canonical persisted job + per-item checkpoints + lease/heartbeat."""

    def __init__(self, family: str, *, root: Path | None = None) -> None:
        from m3_data_root import data_path

        self.family = family
        self.root = root or data_path("jobs", family)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def job_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def items_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "items"

    def lease_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "lease.json"

    def active_pointer_path(self) -> Path:
        return self.root / "ACTIVE_JOB.json"

    def create_job(
        self,
        *,
        total: int,
        opportunity_ids: list[str],
        meta: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            jid = job_id or f"{self.family}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            now = _utc()
            job = {
                "JOB_ID": jid,
                "FAMILY": self.family,
                "STATUS": "CREATED",
                "CREATED_AT": now,
                "STARTED_AT": None,
                "UPDATED_AT": now,
                "FINISHED_AT": None,
                "TOTAL": total,
                "COMPLETED": 0,
                "SUCCESS": 0,
                "PARTIAL": 0,
                "FAILED": 0,
                "TIMEOUT": 0,
                "AUTH_FAILED": 0,
                "NO_PACKAGE": 0,
                "FETCH_FAILED": 0,
                "PARSE_FAILED": 0,
                "SKIPPED_ALREADY_COMPLETE": 0,
                "CURRENT_INDEX": None,
                "CURRENT_OPPORTUNITY_ID": None,
                "LAST_ERROR": None,
                "RESUME_CURSOR": 0,
                "LAST_CHECKPOINT_AT": None,
                "HEARTBEAT_AT": None,
                "WORKER_ID": None,
                "OPPORTUNITY_IDS": list(opportunity_ids),
                "META": meta or {},
            }
            self.items_dir(jid).mkdir(parents=True, exist_ok=True)
            _atomic_write(self.job_path(jid), job)
            return job

    def load_job(self, job_id: str) -> dict[str, Any] | None:
        return _read_json(self.job_path(job_id))

    def save_job(self, job: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            job = dict(job)
            job["UPDATED_AT"] = _utc()
            _atomic_write(self.job_path(str(job["JOB_ID"])), job)
            return job

    def item_path(self, job_id: str, opportunity_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in opportunity_id)[:120]
        return self.items_dir(job_id) / f"{safe}.json"

    def load_item(self, job_id: str, opportunity_id: str) -> dict[str, Any] | None:
        return _read_json(self.item_path(job_id, opportunity_id))

    def write_item_checkpoint(self, job_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            oid = str(checkpoint["OPPORTUNITY_ID"])
            cp = dict(checkpoint)
            cp["CHECKPOINT_AT"] = _utc()
            _atomic_write(self.item_path(job_id, oid), cp)
            job = self.load_job(job_id) or {}
            job["LAST_CHECKPOINT_AT"] = cp["CHECKPOINT_AT"]
            job["UPDATED_AT"] = cp["CHECKPOINT_AT"]
            job["HEARTBEAT_AT"] = cp["CHECKPOINT_AT"]
            _atomic_write(self.job_path(job_id), job)
            return cp

    def list_items(self, job_id: str) -> list[dict[str, Any]]:
        d = self.items_dir(job_id)
        if not d.exists():
            return []
        out: list[dict[str, Any]] = []
        for p in sorted(d.glob("*.json")):
            data = _read_json(p)
            if data:
                out.append(data)
        return out

    def recompute_counts(self, job_id: str) -> dict[str, Any]:
        job = self.load_job(job_id)
        if not job:
            raise KeyError(job_id)
        items = self.list_items(job_id)
        terminal = {
            "SUCCESS",
            "PARTIAL",
            "TIMEOUT",
            "AUTH_FAILED",
            "NO_PACKAGE",
            "FETCH_FAILED",
            "PARSE_FAILED",
            "SKIPPED_ALREADY_COMPLETE",
            "EXPIRED_EXIT",
            "INELIGIBLE_EXIT",
            "OWNER_ACTION_REQUIRED",
            "DEFERRED_ERROR",
            "DEFERRED_EXTERNAL",
        }
        completed = [i for i in items if i.get("RESULT") in terminal]
        job["COMPLETED"] = len(completed)
        for key in (
            "SUCCESS",
            "PARTIAL",
            "TIMEOUT",
            "AUTH_FAILED",
            "NO_PACKAGE",
            "FETCH_FAILED",
            "PARSE_FAILED",
            "SKIPPED_ALREADY_COMPLETE",
            "EXPIRED_EXIT",
            "INELIGIBLE_EXIT",
            "OWNER_ACTION_REQUIRED",
            "DEFERRED_ERROR",
            "DEFERRED_EXTERNAL",
        ):
            job[key] = sum(1 for i in completed if i.get("RESULT") == key)
        job["FAILED"] = (
            int(job.get("AUTH_FAILED") or 0)
            + int(job.get("NO_PACKAGE") or 0)
            + int(job.get("FETCH_FAILED") or 0)
            + int(job.get("PARSE_FAILED") or 0)
            + int(job.get("DEFERRED_ERROR") or 0)
        )
        return self.save_job(job)

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        current_index: int | None = None,
        current_opportunity_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            job = self.load_job(job_id)
            if not job:
                raise KeyError(job_id)
            now = _utc()
            job["HEARTBEAT_AT"] = now
            job["UPDATED_AT"] = now
            job["WORKER_ID"] = worker_id
            if current_index is not None:
                job["CURRENT_INDEX"] = current_index
            if current_opportunity_id is not None:
                job["CURRENT_OPPORTUNITY_ID"] = current_opportunity_id
            _atomic_write(self.job_path(job_id), job)
            lease = {
                "JOB_ID": job_id,
                "WORKER_ID": worker_id,
                "HEARTBEAT_AT": now,
                "CURRENT_ITEM": current_opportunity_id,
                "COMPLETED": job.get("COMPLETED"),
                "TOTAL": job.get("TOTAL"),
                "LAST_CHECKPOINT": job.get("LAST_CHECKPOINT_AT"),
            }
            _atomic_write(self.lease_path(job_id), lease)
            return lease

    def acquire_lease(
        self,
        job_id: str,
        worker_id: str,
        *,
        stale_seconds: float = 120.0,
        force: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            job = self.load_job(job_id)
            if not job:
                raise KeyError(job_id)
            lease = _read_json(self.lease_path(job_id)) or {}
            now = datetime.now(timezone.utc)
            if not force and job.get("STATUS") == "RUNNING" and lease.get("WORKER_ID") and lease.get("WORKER_ID") != worker_id:
                hb = lease.get("HEARTBEAT_AT") or job.get("HEARTBEAT_AT")
                stale = True
                if hb:
                    try:
                        ts = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
                        stale = (now - ts).total_seconds() > stale_seconds
                    except Exception:
                        stale = True
                if not stale:
                    raise JobLeaseError(f"job_leased_by:{lease.get('WORKER_ID')}")
                job["STATUS"] = "STALE_RELEASED"
                job["LAST_ERROR"] = "STALE_WORKER_LEASE_EXPIRED"
                self.save_job(job)
            job["STATUS"] = "RUNNING"
            if not job.get("STARTED_AT"):
                job["STARTED_AT"] = _utc()
            job["WORKER_ID"] = worker_id
            self.save_job(job)
            _atomic_write(self.active_pointer_path(), {"JOB_ID": job_id, "UPDATED_AT": _utc()})
            return self.heartbeat(job_id, worker_id=worker_id)

    def release_lease(self, job_id: str, worker_id: str, *, final_status: str) -> dict[str, Any]:
        with self._lock:
            job = self.load_job(job_id) or {}
            job["STATUS"] = final_status
            job["FINISHED_AT"] = _utc()
            job["CURRENT_INDEX"] = None
            job["CURRENT_OPPORTUNITY_ID"] = None
            job = self.save_job(job)
            lease = _read_json(self.lease_path(job_id)) or {}
            if lease.get("WORKER_ID") == worker_id or not lease:
                lease["RELEASED_AT"] = _utc()
                lease["STATUS"] = final_status
                _atomic_write(self.lease_path(job_id), lease)
            return job

    def force_reset_running_lock(self, job_id: str) -> dict[str, Any]:
        """Clear RUNNING lock/current item only — preserve item checkpoints."""
        with self._lock:
            job = self.load_job(job_id)
            if not job:
                raise KeyError(job_id)
            job["STATUS"] = "INTERRUPTED"
            job["CURRENT_INDEX"] = None
            job["CURRENT_OPPORTUNITY_ID"] = None
            job["LAST_ERROR"] = "FORCE_RESET_RUNNING_LOCK"
            job["WORKER_ID"] = None
            job = self.save_job(job)
            lease = {
                "JOB_ID": job_id,
                "WORKER_ID": None,
                "HEARTBEAT_AT": None,
                "RELEASED_AT": _utc(),
                "STATUS": "FORCE_RESET",
                "PRESERVED_ITEM_CHECKPOINTS": True,
            }
            _atomic_write(self.lease_path(job_id), lease)
            return job

    def cancel_job(self, job_id: str, *, reason: str = "CANCELLED") -> dict[str, Any]:
        with self._lock:
            job = self.load_job(job_id)
            if not job:
                raise KeyError(job_id)
            job["STATUS"] = "CANCELLED"
            job["FINISHED_AT"] = _utc()
            job["LAST_ERROR"] = reason
            job["CURRENT_INDEX"] = None
            job["CURRENT_OPPORTUNITY_ID"] = None
            job = self.save_job(job)
            lease = _read_json(self.lease_path(job_id)) or {}
            lease["STATUS"] = "CANCELLED"
            lease["RELEASED_AT"] = _utc()
            _atomic_write(self.lease_path(job_id), lease)
            return job

    def find_active_job(self, *, stale_seconds: float = 120.0) -> dict[str, Any] | None:
        ptr = _read_json(self.active_pointer_path())
        if not ptr or not ptr.get("JOB_ID"):
            # scan newest dirs
            candidates = sorted(self.root.glob("*/job.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in candidates[:5]:
                job = _read_json(p)
                if job and job.get("STATUS") in {
                    "RUNNING",
                    "CREATED",
                    "INTERRUPTED",
                    "STALE_RELEASED",
                    "STOPPED",
                    "FAILED",
                    "ERROR",
                    "BLOCKED",
                }:
                    return job
            return None
        job = self.load_job(str(ptr["JOB_ID"]))
        if not job:
            return None
        if job.get("STATUS") in {"DONE", "ERROR", "CANCELLED"}:
            return None
        if job.get("STATUS") == "RUNNING":
            hb = job.get("HEARTBEAT_AT") or job.get("UPDATED_AT")
            if hb:
                try:
                    ts = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - ts).total_seconds() > stale_seconds:
                        job["STATUS"] = "STALE_RELEASED"
                        job["LAST_ERROR"] = "STALE_NO_HEARTBEAT"
                        return self.save_job(job)
                except Exception:
                    pass
        return job

    def progress_payload(self, job_id: str) -> dict[str, Any]:
        job = self.recompute_counts(job_id)
        items = self.list_items(job_id)
        return {
            "JOB_ID": job_id,
            "STATUS": job.get("STATUS"),
            "TOTAL": job.get("TOTAL"),
            "COMPLETED": job.get("COMPLETED"),
            "SUCCESS": job.get("SUCCESS"),
            "PARTIAL": job.get("PARTIAL"),
            "TIMEOUT": job.get("TIMEOUT"),
            "FAILED": job.get("FAILED"),
            "AUTH_FAILED": job.get("AUTH_FAILED"),
            "NO_PACKAGE": job.get("NO_PACKAGE"),
            "FETCH_FAILED": job.get("FETCH_FAILED"),
            "PARSE_FAILED": job.get("PARSE_FAILED"),
            "SKIPPED_ALREADY_COMPLETE": job.get("SKIPPED_ALREADY_COMPLETE"),
            "CURRENT_INDEX": job.get("CURRENT_INDEX"),
            "CURRENT_OPPORTUNITY_ID": job.get("CURRENT_OPPORTUNITY_ID"),
            "LAST_CHECKPOINT_AT": job.get("LAST_CHECKPOINT_AT"),
            "HEARTBEAT_AT": job.get("HEARTBEAT_AT"),
            "STARTED_AT": job.get("STARTED_AT"),
            "UPDATED_AT": job.get("UPDATED_AT"),
            "FINISHED_AT": job.get("FINISHED_AT"),
            "LAST_ERROR": job.get("LAST_ERROR"),
            "ITEM_COUNT_ON_DISK": len(items),
            "WORKER_ID": job.get("WORKER_ID"),
        }

"""Group-level BidNet purchasing-group registration queue.

Runs OUTSIDE per-opportunity timeouts. Register once per group, then release
blocked opportunities back into the funnel.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

log = logging.getLogger("govtracker.bidnet_pg_queue")

BUILD = "20261014-m3-bidnet-pg-group-queue-v1"
QUEUE_REL = "bidnet_auth/purchasing_group_registration_queue_v1.json"
_lock = threading.RLock()

# Dedicated budget for one group registration (not the opportunity 45s budget)
GROUP_REGISTRATION_TIMEOUT_S = float(os.environ.get("M3_PG_GROUP_TIMEOUT_S") or "120")
MAX_GROUPS_PER_SLICE = int(os.environ.get("M3_PG_GROUPS_PER_SLICE") or "2")


def _utc() -> str:
    return now_utc().isoformat()


def queue_path() -> Path:
    return Path(data_path(QUEUE_REL))


def _empty() -> dict[str, Any]:
    return {
        "kind": "BidNetPurchasingGroupRegistrationQueue",
        "build": BUILD,
        "updated_at": _utc(),
        "pending": [],
        "completed": [],
        "failed": [],
        "metrics": {
            "enqueued": 0,
            "completed": 0,
            "failed": 0,
            "opportunities_released": 0,
        },
    }


def load_queue() -> dict[str, Any]:
    path = queue_path()
    if not path.exists():
        return _empty()
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else _empty()
    except Exception:
        return _empty()


def save_queue(state: dict[str, Any]) -> Path:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state = deepcopy(state)
    state["updated_at"] = _utc()
    state["build"] = BUILD
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _lock:
        tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
    return path


def enqueue_group_registration(
    purchasing_group_id: str,
    *,
    name: str | None = None,
    opportunity_id: str | None = None,
    source_url: str | None = None,
    form_url: str | None = None,
) -> dict[str, Any]:
    """Idempotent enqueue. Never duplicates a pending group."""
    pg_id = str(purchasing_group_id or "").strip()
    if not pg_id:
        return {"ok": False, "error": "missing_pg_id"}
    from bidnet_auth.purchasing_group_tracker import is_registered, note_blocked_opportunity

    if opportunity_id:
        note_blocked_opportunity(pg_id, opportunity_id)
    if is_registered(pg_id):
        return {"ok": True, "status": "ALREADY_REGISTERED", "purchasing_group_id": pg_id}

    with _lock:
        state = load_queue()
        pending = list(state.get("pending") or [])
        for item in pending:
            if str(item.get("purchasing_group_id")) == pg_id:
                oids = list(item.get("opportunity_ids") or [])
                if opportunity_id and opportunity_id not in oids:
                    oids.append(opportunity_id)
                    item["opportunity_ids"] = oids[-500:]
                    item["updated_at"] = _utc()
                    state["pending"] = pending
                    save_queue(state)
                return {"ok": True, "status": "ALREADY_QUEUED", "purchasing_group_id": pg_id}
        pending.append(
            {
                "purchasing_group_id": pg_id,
                "name": name,
                "form_url": form_url,
                "source_url": source_url,
                "opportunity_ids": [opportunity_id] if opportunity_id else [],
                "enqueued_at": _utc(),
                "updated_at": _utc(),
                "attempts": 0,
            }
        )
        state["pending"] = pending
        metrics = dict(state.get("metrics") or {})
        metrics["enqueued"] = int(metrics.get("enqueued") or 0) + 1
        state["metrics"] = metrics
        save_queue(state)
        return {"ok": True, "status": "ENQUEUED", "purchasing_group_id": pg_id}


def process_group_registration_slice(
    auth_client: Any,
    *,
    max_groups: int | None = None,
) -> dict[str, Any]:
    """Process up to N pending groups with dedicated timeouts. Releases blocked opps."""
    from bidnet_auth.purchasing_group_registration import (
        extract_solicitation_id_from_url,
        resolve_access_gate_registration,
        run_purchasing_group_registration,
    )
    from bidnet_auth.purchasing_group_tracker import (
        STATUS_REGISTERED,
        get_group,
        is_registered,
    )
    limit = max(1, int(max_groups if max_groups is not None else MAX_GROUPS_PER_SLICE))
    processed: list[dict[str, Any]] = []
    released_total = 0
    active_job_id: str | None = None
    try:
        from m3_jobs.store import JobStore

        active = JobStore("full_scale_production_funnel_v1").find_active_job(stale_seconds=3600)
        if active and active.get("JOB_ID"):
            active_job_id = str(active["JOB_ID"])
    except Exception:
        active_job_id = None

    with _lock:
        state = load_queue()
        pending = list(state.get("pending") or [])

    for item in pending[:limit]:
        pg_id = str(item.get("purchasing_group_id") or "")
        if not pg_id:
            continue
        if is_registered(pg_id):
            row = {
                "purchasing_group_id": pg_id,
                "status": STATUS_REGISTERED,
                "skipped": True,
                "reason": "already_registered",
            }
            processed.append(row)
            _complete_pending(pg_id, row)
            continue

        item["attempts"] = int(item.get("attempts") or 0) + 1
        try:
            form_url = item.get("form_url")
            html = ""
            page_url = str(item.get("source_url") or form_url or "")
            if form_url or page_url:
                reg = run_purchasing_group_registration(
                    auth_client,
                    purchasing_group_id=pg_id,
                    form_url=form_url,
                    name=item.get("name"),
                    solicitation_id=extract_solicitation_id_from_url(page_url),
                    allow_submit=True,
                    opportunity_id=(item.get("opportunity_ids") or [None])[0],
                )
            else:
                reg = resolve_access_gate_registration(
                    auth_client,
                    html=html,
                    page_url=page_url,
                    allow_submit=True,
                    source_url=page_url,
                )
            row = {
                "purchasing_group_id": pg_id,
                "status": reg.get("status"),
                "submitted": reg.get("submitted"),
                "already_registered": reg.get("already_registered"),
                "ok": reg.get("ok"),
                "attempts": item["attempts"],
            }
            processed.append(row)
            st = str(reg.get("status") or "")
            if st == STATUS_REGISTERED or reg.get("already_registered"):
                _complete_pending(pg_id, row)
                if active_job_id:
                    try:
                        # Lazy import avoids circular load with full_scale module
                        from buyer_intelligence import full_scale_production_funnel_v1 as fspf

                        rel = fspf.release_pg_blocked_for_registered_groups(active_job_id)
                        released_total += int(rel.get("released") or 0)
                        metrics = dict(load_queue().get("metrics") or {})
                        metrics["opportunities_released"] = int(
                            metrics.get("opportunities_released") or 0
                        ) + int(rel.get("released") or 0)
                        q = load_queue()
                        q["metrics"] = metrics
                        save_queue(q)
                    except Exception as exc:
                        row["release_error"] = type(exc).__name__
            elif st in {"OWNER_APPROVAL_REQUIRED", "PAID_REQUIRED", "PREPARED"}:
                _fail_pending(pg_id, row, terminal=True)
            elif int(item["attempts"]) >= 3:
                _fail_pending(pg_id, {**row, "error": "max_attempts"}, terminal=True)
            else:
                _bump_pending(pg_id, item)
        except Exception as exc:
            row = {
                "purchasing_group_id": pg_id,
                "status": "ERROR",
                "error": type(exc).__name__,
                "attempts": item["attempts"],
            }
            processed.append(row)
            if int(item["attempts"]) >= 3:
                _fail_pending(pg_id, row, terminal=True)
            else:
                _bump_pending(pg_id, item)

    q = load_queue()
    return {
        "build": BUILD,
        "processed": processed,
        "pending_remaining": len(q.get("pending") or []),
        "opportunities_released": released_total,
        "metrics": q.get("metrics"),
        "group_timeout_s": GROUP_REGISTRATION_TIMEOUT_S,
    }


def _complete_pending(pg_id: str, row: dict[str, Any]) -> None:
    with _lock:
        state = load_queue()
        pending = [p for p in (state.get("pending") or []) if str(p.get("purchasing_group_id")) != pg_id]
        done = list(state.get("completed") or [])
        done.append({**row, "completed_at": _utc()})
        state["pending"] = pending
        state["completed"] = done[-200:]
        metrics = dict(state.get("metrics") or {})
        metrics["completed"] = int(metrics.get("completed") or 0) + 1
        state["metrics"] = metrics
        save_queue(state)


def _fail_pending(pg_id: str, row: dict[str, Any], *, terminal: bool) -> None:
    with _lock:
        state = load_queue()
        if terminal:
            pending = [
                p for p in (state.get("pending") or []) if str(p.get("purchasing_group_id")) != pg_id
            ]
            failed = list(state.get("failed") or [])
            failed.append({**row, "failed_at": _utc()})
            state["pending"] = pending
            state["failed"] = failed[-200:]
            metrics = dict(state.get("metrics") or {})
            metrics["failed"] = int(metrics.get("failed") or 0) + 1
            state["metrics"] = metrics
        save_queue(state)


def _bump_pending(pg_id: str, item: dict[str, Any]) -> None:
    with _lock:
        state = load_queue()
        pending = list(state.get("pending") or [])
        for p in pending:
            if str(p.get("purchasing_group_id")) == pg_id:
                p["attempts"] = item.get("attempts")
                p["updated_at"] = _utc()
                p["last_error"] = item.get("last_error")
        state["pending"] = pending
        save_queue(state)


def ui_summary() -> dict[str, Any]:
    q = load_queue()
    return {
        "kind": "PurchasingGroupRegistrationQueueUI",
        "build": BUILD,
        "updated_at": q.get("updated_at"),
        "pending_count": len(q.get("pending") or []),
        "completed_count": len(q.get("completed") or []),
        "failed_count": len(q.get("failed") or []),
        "pending": (q.get("pending") or [])[:20],
        "metrics": q.get("metrics"),
    }

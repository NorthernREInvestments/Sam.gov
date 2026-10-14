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

BUILD = "20261014-m3-bidnet-pg-group-queue-v1b"
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


def _resolve_form_url(pg_id: str, form_url: str | None = None) -> str | None:
    """Prefer explicit form URL; else tracker gate path from prior interceptions."""
    cand = str(form_url or "").strip()
    if not cand:
        try:
            from bidnet_auth.purchasing_group_tracker import get_group

            tracked = get_group(str(pg_id)) or {}
            cand = str(
                tracked.get("complete_form_url")
                or tracked.get("complete_form_url_path")
                or tracked.get("form_url")
                or ""
            ).strip()
        except Exception:
            cand = ""
    if cand.startswith("/"):
        cand = "https://www.bidnetdirect.com" + cand
    return cand or None


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
    from bidnet_auth.purchasing_group_tracker import (
        is_registered,
        note_blocked_opportunity,
        upsert_group,
    )

    if opportunity_id:
        note_blocked_opportunity(pg_id, opportunity_id)
    try:
        upsert_group(pg_id, name=name, relevant=True, source_url=source_url)
    except Exception:
        pass
    if is_registered(pg_id):
        return {"ok": True, "status": "ALREADY_REGISTERED", "purchasing_group_id": pg_id}
    form_url = _resolve_form_url(pg_id, form_url)

    with _lock:
        state = load_queue()
        pending = list(state.get("pending") or [])
        for item in pending:
            if str(item.get("purchasing_group_id")) == pg_id:
                oids = list(item.get("opportunity_ids") or [])
                changed = False
                if opportunity_id and opportunity_id not in oids:
                    oids.append(opportunity_id)
                    item["opportunity_ids"] = oids[-500:]
                    changed = True
                if form_url and not item.get("form_url"):
                    item["form_url"] = form_url
                    changed = True
                if name and not item.get("name"):
                    item["name"] = name
                    changed = True
                if source_url and not item.get("source_url"):
                    item["source_url"] = source_url
                    changed = True
                if changed:
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


def rebuild_pending_from_job(job_id: str) -> dict[str, Any]:
    """Rebuild group registration queue from FSPF checkpoints awaiting PG access.

    Idempotent. Prioritizes groups with the most blocked live opportunities.
    Does not wipe completed memberships or invent registrations.
    """
    from bidnet_auth.purchasing_group_registration import extract_pg_id_from_url
    from bidnet_auth.purchasing_group_tracker import is_registered
    from m3_jobs.store import JobStore

    store = JobStore("full_scale_production_funnel_v1")
    job = store.load_job(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found", "job_id": job_id}

    enqueued = 0
    already = 0
    registered_skip = 0
    examined = 0
    by_pg: dict[str, dict[str, Any]] = {}
    for it in store.list_items(job_id):
        if not isinstance(it, dict):
            continue
        examined += 1
        blocker = str(it.get("terminal_blocker") or it.get("PREVIOUS_BLOCKER") or "")
        result = str(it.get("RESULT") or "")
        if result in {"SUCCESS", "EXPIRED_EXIT", "CANCELLED"}:
            continue
        if (
            "PURCHASING" not in blocker.upper()
            and "AWAITING_PURCHASING" not in blocker.upper()
            and result not in {"NO_PACKAGE", "PENDING_RETRY", "OWNER_ACTION_REQUIRED"}
        ):
            # Still accept NO_PACKAGE rows that carry a purchasing_group_id
            if not it.get("purchasing_group_id"):
                continue
        pg = str(it.get("purchasing_group_id") or "").strip() or extract_pg_id_from_url(
            str(it.get("authoritative_url") or "")
        )
        if not pg:
            continue
        if is_registered(pg):
            registered_skip += 1
            continue
        row = by_pg.setdefault(
            pg,
            {
                "purchasing_group_id": pg,
                "opportunity_ids": [],
                "source_url": None,
                "name": None,
            },
        )
        oid = str(it.get("OPPORTUNITY_ID") or it.get("opportunity_id") or "")
        if oid and oid not in row["opportunity_ids"]:
            row["opportunity_ids"].append(oid)
        url = str(it.get("authoritative_url") or "")
        if url and not row["source_url"]:
            row["source_url"] = url

    for pg, meta in sorted(
        by_pg.items(), key=lambda kv: -len(kv[1].get("opportunity_ids") or [])
    ):
        oids = list(meta.get("opportunity_ids") or [])
        # Attach all known oids via repeated enqueue (idempotent)
        first = True
        for oid in oids[:200]:
            res = enqueue_group_registration(
                pg,
                opportunity_id=oid,
                source_url=meta.get("source_url") if first else None,
                name=meta.get("name"),
            )
            first = False
            st = str(res.get("status") or "")
            if st == "ENQUEUED":
                enqueued += 1
            elif st == "ALREADY_QUEUED":
                already += 1
            elif st == "ALREADY_REGISTERED":
                registered_skip += 1

    q = load_queue()
    pending = sorted(
        list(q.get("pending") or []),
        key=lambda r: -len(r.get("opportunity_ids") or []),
    )
    q["pending"] = pending
    save_queue(q)
    return {
        "ok": True,
        "job_id": job_id,
        "examined": examined,
        "groups_seen": len(by_pg),
        "enqueued_new": enqueued,
        "already_queued": already,
        "registered_skip": registered_skip,
        "pending_remaining": len(pending),
        "top_pending": [
            {
                "purchasing_group_id": r.get("purchasing_group_id"),
                "opportunity_count": len(r.get("opportunity_ids") or []),
                "attempts": r.get("attempts"),
            }
            for r in pending[:12]
        ],
        "build": BUILD,
    }


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
        # Prefer groups that unlock the most blocked opportunities first.
        state["pending"] = sorted(
            list(state.get("pending") or []),
            key=lambda r: (
                -len(r.get("opportunity_ids") or []),
                int(r.get("attempts") or 0),
            ),
        )
        # Soft revive: Timeout/ERROR often used stale form URLs; INCOMPLETE may
        # succeed after certification-radio mapping improvements (never paid).
        failed = list(state.get("failed") or [])
        revived = []
        keep_failed = []
        for row in failed:
            err = str(row.get("error") or row.get("last_error") or "")
            st = str(row.get("status") or "")
            revive = (
                "Timeout" in err
                or st == "ERROR"
                or (st == "INCOMPLETE" and int(row.get("attempts") or 0) < 4)
            )
            if revive:
                revived.append(
                    {
                        "purchasing_group_id": row.get("purchasing_group_id"),
                        "name": row.get("name"),
                        "form_url": None if st == "INCOMPLETE" else row.get("form_url"),
                        "source_url": row.get("source_url"),
                        "opportunity_ids": list(row.get("opportunity_ids") or []),
                        "enqueued_at": row.get("enqueued_at") or _utc(),
                        "updated_at": _utc(),
                        "attempts": min(int(row.get("attempts") or 0), 1),
                        "revived_from_failed": True,
                        "revived_status": st or None,
                    }
                )
            else:
                keep_failed.append(row)
        if revived:
            pending_now = list(state.get("pending") or [])
            seen = {str(p.get("purchasing_group_id")) for p in pending_now}
            for r in revived:
                if str(r.get("purchasing_group_id")) not in seen:
                    pending_now.append(r)
            state["pending"] = pending_now
            state["failed"] = keep_failed[-200:]
            save_queue(state)
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
            if active_job_id:
                try:
                    from buyer_intelligence import full_scale_production_funnel_v1 as fspf

                    released_total += int(
                        (fspf.release_pg_blocked_for_registered_groups(active_job_id) or {}).get(
                            "released"
                        )
                        or 0
                    )
                except Exception:
                    pass
            continue

        tracked = get_group(pg_id) or {}
        tracked_st = str(tracked.get("status") or "")
        if tracked_st in {
            "OWNER_APPROVAL_REQUIRED",
            "PAID_REQUIRED",
            "PREPARED",
        }:
            row = {
                "purchasing_group_id": pg_id,
                "status": tracked_st,
                "skipped": True,
                "reason": "owner_or_paid_gate",
                "owner_blocker_reason": tracked.get("owner_blocker_reason"),
            }
            processed.append(row)
            _fail_pending(pg_id, row, terminal=True)
            continue

        item["attempts"] = int(item.get("attempts") or 0) + 1
        try:
            form_url = _resolve_form_url(pg_id, item.get("form_url"))
            if form_url and form_url != item.get("form_url"):
                item["form_url"] = form_url
                _bump_pending(pg_id, item)
            source_url = str(item.get("source_url") or "").strip()
            # Prefer live gate resolution from the solicitation URL. Stored form URLs
            # go stale (session-bound accessedElementId) and hang Playwright.
            if source_url:
                html = ""
                page_url = source_url
                try:
                    if hasattr(auth_client, "fetch_html"):
                        html = (
                            auth_client.fetch_html(
                                source_url, timeout_ms=45_000, prefer_http=False
                            )
                            or ""
                        )
                        page_url = str(
                            getattr(getattr(auth_client, "_page", None), "url", None)
                            or source_url
                        )
                except Exception as fetch_exc:
                    item["last_error"] = f"fetch:{type(fetch_exc).__name__}"
                reg = resolve_access_gate_registration(
                    auth_client,
                    html=html,
                    page_url=page_url,
                    allow_submit=True,
                    source_url=source_url,
                    opportunity_id=(item.get("opportunity_ids") or [None])[0],
                )
            elif form_url:
                reg = run_purchasing_group_registration(
                    auth_client,
                    purchasing_group_id=pg_id,
                    form_url=form_url,
                    name=item.get("name"),
                    solicitation_id=extract_solicitation_id_from_url(form_url),
                    allow_submit=True,
                    opportunity_id=(item.get("opportunity_ids") or [None])[0],
                )
            else:
                reg = {
                    "status": "ERROR",
                    "ok": False,
                    "reason": "missing_form_and_source_url",
                    "purchasing_group_id": pg_id,
                }
            # Persist freshly discovered form URL for the next attempt
            fresh_form = (reg.get("gate") or {}).get("complete_form_url") or reg.get(
                "complete_form_url"
            )
            if fresh_form:
                item["form_url"] = fresh_form
                _bump_pending(pg_id, item)
            row = {
                "purchasing_group_id": pg_id,
                "status": reg.get("status"),
                "submitted": reg.get("submitted"),
                "already_registered": reg.get("already_registered"),
                "ok": reg.get("ok"),
                "attempts": item["attempts"],
                "error": reg.get("error") or reg.get("reason"),
                "form_url_used": (form_url or item.get("form_url") or "")[:160] or None,
                "source_url": (source_url or "")[:160] or None,
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
            elif st in {"OWNER_APPROVAL_REQUIRED", "PAID_REQUIRED", "PREPARED", "INCOMPLETE"}:
                # INCOMPLETE after submit needs owner/field review — do not hot-loop
                _fail_pending(pg_id, row, terminal=True)
            elif int(item["attempts"]) >= 5 and str(row.get("error") or "") in {
                "TimeoutError",
                "FORM_NAV_REDIRECT",
                "FORM_URL_MISSING",
            }:
                _fail_pending(pg_id, {**row, "error": row.get("error") or "max_attempts"}, terminal=True)
            elif int(item["attempts"]) >= 3 and str(row.get("error") or "") not in {
                "TimeoutError",
                "FORM_NAV_REDIRECT",
            }:
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
            # Transient Playwright timeouts: keep pending longer; don't burn AUTH
            max_attempts = 5 if type(exc).__name__ == "TimeoutError" else 3
            if int(item["attempts"]) >= max_attempts:
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

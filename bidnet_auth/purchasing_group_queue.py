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

BUILD = "20261016-m3-bidnet-pg-group-queue-v1f"
QUEUE_REL = "bidnet_auth/purchasing_group_registration_queue_v1.json"
_lock = threading.RLock()

# Dedicated budget for one group registration (not the opportunity 45s budget)
GROUP_REGISTRATION_TIMEOUT_S = float(os.environ.get("M3_PG_GROUP_TIMEOUT_S") or "90")
MAX_GROUPS_PER_SLICE = int(os.environ.get("M3_PG_GROUPS_PER_SLICE") or "1")
GATE_FETCH_TIMEOUT_MS = int(os.environ.get("M3_PG_GATE_FETCH_MS") or "20000")


def _form_url_without_stale_id(url: str | None) -> str | None:
    from bidnet_auth.purchasing_group_registration import strip_stale_accessed_element

    return strip_stale_accessed_element(url)


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


def _resolve_form_url(
    pg_id: str,
    form_url: str | None = None,
    *,
    strip_stale: bool = False,
) -> str | None:
    """Prefer explicit form URL; else tracker path; else constructed URL.

    Do NOT strip accessedElementId by default — BidNet needs a session-scoped
    solicitation id for many group forms. Stripping on every resolve caused
    ~6s TimeoutError on path-only URLs.
    """
    cand = str(form_url or "").strip()
    if not cand:
        try:
            from bidnet_auth.purchasing_group_tracker import get_group

            tracked = get_group(str(pg_id)) or {}
            cand = str(
                tracked.get("complete_form_url")
                or tracked.get("complete_form_url_path")
                or tracked.get("form_url_path")
                or tracked.get("form_url")
                or ""
            ).strip()
        except Exception:
            cand = ""
    if cand.startswith("/"):
        cand = "https://www.bidnetdirect.com" + cand
    if strip_stale and cand and "accessedElementId=" in cand:
        cand = _form_url_without_stale_id(cand) or cand
    if not cand and pg_id:
        try:
            from bidnet_auth.purchasing_group_registration import build_group_form_url

            cand = build_group_form_url(purchasing_group_id=str(pg_id), solicitation_id=None) or ""
        except Exception:
            cand = ""
    return cand or None


def mint_fresh_form_url_from_source(
    auth_client: Any,
    *,
    purchasing_group_id: str,
    source_url: str,
) -> dict[str, Any]:
    """Open solicitation abstract and mint a session-fresh group-form URL.

    Uses interceptorProceedBtn / gate HTML when present. Never fabricates group IDs.
    """
    import time as _time

    from bidnet_auth.purchasing_group_registration import (
        build_group_form_url,
        extract_pg_gate_from_html,
        extract_pg_id_from_url,
        extract_solicitation_id_from_url,
    )

    t0 = _time.time()
    trace: list[dict[str, Any]] = []
    src = str(source_url or "").strip()
    pg_id = str(purchasing_group_id or "").strip()
    if not src or not pg_id:
        return {"ok": False, "error": "MISSING_SOURCE_OR_PG", "trace": trace}
    html = ""
    page_url = src
    try:
        if hasattr(auth_client, "fetch_html"):
            html = (
                auth_client.fetch_html(src, timeout_ms=GATE_FETCH_TIMEOUT_MS, prefer_http=True)
                or ""
            )
            page_url = str(
                getattr(getattr(auth_client, "_page", None), "url", None) or src
            )
            trace.append(
                {
                    "stage": "source_fetch",
                    "ok": bool(html),
                    "elapsed_s": round(_time.time() - t0, 3),
                    "html_len": len(html),
                }
            )
    except Exception as exc:
        trace.append(
            {
                "stage": "source_fetch",
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc)[:160],
                "elapsed_s": round(_time.time() - t0, 3),
            }
        )
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)[:160], "trace": trace}

    gate = extract_pg_gate_from_html(html, page_url=page_url) or {}
    proceed = str(gate.get("complete_form_url") or "").strip()
    # Confirm proceed URL targets the same purchasing group
    proceed_pg = extract_pg_id_from_url(proceed) if proceed else None
    if proceed and proceed_pg and str(proceed_pg) != pg_id:
        trace.append(
            {
                "stage": "gate_pg_mismatch",
                "expected": pg_id,
                "got": proceed_pg,
                "elapsed_s": round(_time.time() - t0, 3),
            }
        )
        proceed = ""
    sol_id = extract_solicitation_id_from_url(page_url) or extract_solicitation_id_from_url(src)
    if not proceed:
        proceed = build_group_form_url(purchasing_group_id=pg_id, solicitation_id=sol_id) or ""
        trace.append(
            {
                "stage": "built_form_url",
                "has_accessed_element": "accessedElementId=" in proceed,
                "sol_id": sol_id,
                "elapsed_s": round(_time.time() - t0, 3),
            }
        )
    else:
        trace.append(
            {
                "stage": "gate_proceed_url",
                "has_accessed_element": "accessedElementId=" in proceed,
                "elapsed_s": round(_time.time() - t0, 3),
            }
        )
    if not proceed:
        return {"ok": False, "error": "FORM_URL_MINT_FAILED", "trace": trace, "gate": gate}
    return {
        "ok": True,
        "form_url": proceed,
        "solicitation_id": sol_id,
        "gate": gate,
        "trace": trace,
        "elapsed_s": round(_time.time() - t0, 3),
    }


def enqueue_group_registration(
    purchasing_group_id: str,
    *,
    name: str | None = None,
    opportunity_id: str | None = None,
    source_url: str | None = None,
    form_url: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Idempotent enqueue. Never duplicates a pending group.

    force=True re-queues from failed after an auto-cleared owner blocker.
    """
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
        if force:
            # Pull out of failed so a fresh attempt can run.
            state["failed"] = [
                f
                for f in (state.get("failed") or [])
                if str(f.get("purchasing_group_id")) != pg_id
            ]
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


def _pending_priority(row: dict[str, Any]) -> tuple:
    """Higher unlock impact first; prefer groups that already reached a form."""
    oids = len(row.get("opportunity_ids") or [])
    tracked_st = ""
    last_err = str(row.get("last_error") or "")
    try:
        from bidnet_auth.purchasing_group_tracker import get_group

        tracked = get_group(str(row.get("purchasing_group_id") or "")) or {}
        oids = max(oids, len(tracked.get("blocked_opportunity_ids") or []))
        oids = max(oids, int(tracked.get("live_eligible_opportunity_count") or 0))
        tracked_st = str(tracked.get("status") or "")
        last_err = last_err or str(tracked.get("last_error") or tracked.get("owner_blocker_reason") or "")
    except Exception:
        pass
    # Prefer INCOMPLETE / Failed Check (form reachable) over pure nav TimeoutError.
    stage_rank = 0
    if tracked_st == "INCOMPLETE" or "Failed Check" in last_err:
        stage_rank = -2
    elif tracked_st in {"PREPARED", "REGISTERING"}:
        stage_rank = -1
    elif "Timeout" in last_err and int(row.get("attempts") or 0) >= 2:
        stage_rank = 3  # demote repeated nav failures
    return (stage_rank, -oids, int(row.get("attempts") or 0))


def process_group_registration_slice(
    auth_client: Any,
    *,
    max_groups: int | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Process up to N pending groups with dedicated timeouts. Releases blocked opps."""
    import time as _time

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
    active_job_id: str | None = str(job_id or "").strip() or None
    if not active_job_id:
        try:
            from m3_jobs.store import JobStore

            active = JobStore("full_scale_production_funnel_v1").find_active_job(stale_seconds=3600)
            if active and active.get("JOB_ID"):
                active_job_id = str(active["JOB_ID"])
        except Exception:
            active_job_id = None

    # Clear false owner blockers (conditional otherMBENames) before selecting work.
    owner_clear: dict[str, Any] = {}
    try:
        from bidnet_auth.purchasing_group_tracker import clear_auto_fixable_owner_blockers

        owner_clear = clear_auto_fixable_owner_blockers()
        for pg_id in owner_clear.get("cleared") or []:
            enqueue_group_registration(str(pg_id), force=True)
    except Exception as exc:
        owner_clear = {"error": type(exc).__name__}

    with _lock:
        state = load_queue()
        # Prefer groups that unlock the most blocked opportunities first.
        state["pending"] = sorted(
            list(state.get("pending") or []),
            key=_pending_priority,
        )
        save_queue(state)
        # Soft revive: Timeout/ERROR often used stale form URLs; INCOMPLETE may
        # succeed after certification-radio mapping improvements (never paid).
        # Auto-fixable OWNER_APPROVAL (conditional other-cert only) also revive.
        from bidnet_auth.purchasing_group_registration import missing_fields_are_auto_fixable
        from bidnet_auth.purchasing_group_tracker import get_group

        failed = list(state.get("failed") or [])
        revived = []
        keep_failed = []
        for row in failed:
            err = str(row.get("error") or row.get("last_error") or "")
            st = str(row.get("status") or "")
            tracked = get_group(str(row.get("purchasing_group_id") or "")) or {}
            auto_owner = st == "OWNER_APPROVAL_REQUIRED" and (
                missing_fields_are_auto_fixable(tracked.get("missing_fields"))
                or missing_fields_are_auto_fixable(row.get("missing_fields"))
                or str(tracked.get("status") or "") == "INCOMPLETE"
            )
            revive = (
                "Timeout" in err
                or st == "ERROR"
                or auto_owner
                or (st == "INCOMPLETE" and int(row.get("attempts") or 0) < 4)
            )
            if revive:
                prior_form = row.get("form_url")
                # Drop stale accessedElementId on revive after Timeout/ERROR
                if "Timeout" in err or st == "ERROR":
                    prior_form = _form_url_without_stale_id(prior_form)
                revived.append(
                    {
                        "purchasing_group_id": row.get("purchasing_group_id"),
                        "name": row.get("name"),
                        "form_url": None if st == "INCOMPLETE" else prior_form,
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
            from bidnet_auth.purchasing_group_registration import missing_fields_are_auto_fixable

            if tracked_st == "OWNER_APPROVAL_REQUIRED" and missing_fields_are_auto_fixable(
                tracked.get("missing_fields")
            ):
                # Reclassified — do not terminal-fail; fall through to registration.
                tracked_st = "INCOMPLETE"
            else:
                row = {
                    "purchasing_group_id": pg_id,
                    "status": tracked_st,
                    "skipped": True,
                    "reason": "owner_or_paid_gate",
                    "owner_blocker_reason": tracked.get("owner_blocker_reason"),
                    "missing_fields": (tracked.get("missing_fields") or [])[:6],
                }
                processed.append(row)
                _fail_pending(pg_id, row, terminal=True)
                continue

        item["attempts"] = int(item.get("attempts") or 0) + 1
        group_t0 = _time.time()
        stage_trace: list[dict[str, Any]] = []
        try:
            source_url = str(item.get("source_url") or "").strip()
            form_url = _resolve_form_url(pg_id, item.get("form_url"), strip_stale=False)
            prior_timeout = "Timeout" in str(item.get("last_error") or "")
            # After timeouts / 2nd+ attempt: mint a FRESH session-scoped form URL
            # from the solicitation abstract. Do not strip accessedElementId and
            # retry a path-only URL — that yields ~6s TimeoutError.
            mint_meta: dict[str, Any] | None = None
            if source_url and (
                prior_timeout
                or int(item.get("attempts") or 0) >= 2
                or not form_url
                or "accessedElementId=" not in str(form_url)
            ):
                mint_meta = mint_fresh_form_url_from_source(
                    auth_client,
                    purchasing_group_id=pg_id,
                    source_url=source_url,
                )
                stage_trace.extend(list(mint_meta.get("trace") or []))
                if mint_meta.get("ok") and mint_meta.get("form_url"):
                    form_url = str(mint_meta["form_url"])
                    stage_trace.append(
                        {
                            "stage": "form_url_minted",
                            "has_accessed_element": "accessedElementId=" in form_url,
                            "elapsed_s": round(_time.time() - group_t0, 3),
                        }
                    )
            if form_url and form_url != item.get("form_url"):
                item["form_url"] = form_url
                _bump_pending(pg_id, item)
            # Prefer direct form URL for the known PG id — never guess group IDs
            # from unrelated statewide URLs. Gate-resolve only when form_url missing.
            if form_url:
                if _time.time() - group_t0 >= GROUP_REGISTRATION_TIMEOUT_S:
                    raise TimeoutError("GROUP_REGISTRATION_TIMEOUT_S")
                stage_trace.append(
                    {
                        "stage": "registration_start",
                        "form_url_has_accessed_element": "accessedElementId="
                        in str(form_url),
                        "elapsed_s": round(_time.time() - group_t0, 3),
                    }
                )
                sol_for_reg = None
                if mint_meta and mint_meta.get("solicitation_id"):
                    sol_for_reg = mint_meta.get("solicitation_id")
                reg = run_purchasing_group_registration(
                    auth_client,
                    purchasing_group_id=pg_id,
                    form_url=form_url,
                    name=item.get("name"),
                    solicitation_id=sol_for_reg,
                    allow_submit=True,
                    opportunity_id=(item.get("opportunity_ids") or [None])[0],
                )
            elif source_url:
                html = ""
                page_url = source_url
                try:
                    if hasattr(auth_client, "fetch_html"):
                        html = (
                            auth_client.fetch_html(
                                source_url,
                                timeout_ms=GATE_FETCH_TIMEOUT_MS,
                                prefer_http=True,
                            )
                            or ""
                        )
                        page_url = str(
                            getattr(getattr(auth_client, "_page", None), "url", None)
                            or source_url
                        )
                except Exception as fetch_exc:
                    item["last_error"] = f"fetch:{type(fetch_exc).__name__}"
                if _time.time() - group_t0 >= GROUP_REGISTRATION_TIMEOUT_S:
                    raise TimeoutError("GROUP_REGISTRATION_TIMEOUT_S")
                reg = resolve_access_gate_registration(
                    auth_client,
                    html=html,
                    page_url=page_url,
                    allow_submit=True,
                    source_url=source_url,
                    opportunity_id=(item.get("opportunity_ids") or [None])[0],
                )
            else:
                # Last resort: construct clean form URL from verified PG id.
                from bidnet_auth.purchasing_group_registration import build_group_form_url

                built = build_group_form_url(purchasing_group_id=pg_id, solicitation_id=None)
                if built:
                    reg = run_purchasing_group_registration(
                        auth_client,
                        purchasing_group_id=pg_id,
                        form_url=built,
                        name=item.get("name"),
                        solicitation_id=None,
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
            if _time.time() - group_t0 >= GROUP_REGISTRATION_TIMEOUT_S and str(
                reg.get("status") or ""
            ) not in {STATUS_REGISTERED, "OWNER_APPROVAL_REQUIRED", "PAID_REQUIRED"}:
                reg = {
                    **(reg if isinstance(reg, dict) else {}),
                    "status": "ERROR",
                    "ok": False,
                    "error": "TimeoutError",
                    "reason": "GROUP_REGISTRATION_TIMEOUT_S",
                }
            # Persist freshly discovered form URL for the next attempt
            fresh_form = (reg.get("gate") or {}).get("complete_form_url") or reg.get(
                "complete_form_url"
            )
            if fresh_form:
                item["form_url"] = fresh_form
                _bump_pending(pg_id, item)
            if isinstance(reg, dict) and reg.get("stage_trace"):
                stage_trace.extend(list(reg.get("stage_trace") or []))
            stage_trace.append(
                {
                    "stage": "registration_result",
                    "status": reg.get("status") if isinstance(reg, dict) else None,
                    "error": (reg.get("error") if isinstance(reg, dict) else None),
                    "elapsed_s": round(_time.time() - group_t0, 3),
                }
            )
            row = {
                "purchasing_group_id": pg_id,
                "status": reg.get("status"),
                "submitted": reg.get("submitted"),
                "already_registered": reg.get("already_registered"),
                "ok": reg.get("ok"),
                "attempts": item["attempts"],
                "error": reg.get("error") or reg.get("reason"),
                "error_message": (
                    str(reg.get("message") or reg.get("error_message") or "")[:200] or None
                ),
                "failure_point": reg.get("failure_point"),
                "form_url_used": (form_url or item.get("form_url") or "")[:220] or None,
                "form_url_has_accessed_element": "accessedElementId="
                in str(form_url or item.get("form_url") or ""),
                "source_url": (source_url or "")[:160] or None,
                "elapsed_s": round(_time.time() - group_t0, 2),
                "validation_errors": (reg.get("validation_errors") or [])[:4] or None,
                "access_verify": reg.get("access_verify"),
                "stage_trace": stage_trace[:12],
            }
            if reg.get("error") or reg.get("message"):
                item["last_error"] = str(reg.get("error") or reg.get("message") or "")[:120]
                _bump_pending(pg_id, item)
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
            elif st == "INCOMPLETE":
                # Keep pending with backoff — Save≠COMPLETED; radio/field fixes may land.
                item["last_error"] = "INCOMPLETE_AFTER_SAVE"
                if int(item["attempts"]) >= 3:
                    _fail_pending(pg_id, row, terminal=True)
                else:
                    _bump_pending(pg_id, item)
            elif int(item["attempts"]) >= 5 and str(row.get("error") or "") in {
                "TimeoutError",
                "FORM_NAV_REDIRECT",
                "FORM_URL_MISSING",
                "GROUP_REGISTRATION_TIMEOUT_S",
            }:
                _fail_pending(pg_id, {**row, "error": row.get("error") or "max_attempts"}, terminal=True)
            elif int(item["attempts"]) >= 3 and str(row.get("error") or "") not in {
                "TimeoutError",
                "FORM_NAV_REDIRECT",
                "GROUP_REGISTRATION_TIMEOUT_S",
            }:
                _fail_pending(pg_id, {**row, "error": "max_attempts"}, terminal=True)
            else:
                _bump_pending(pg_id, item)
        except Exception as exc:
            stage_trace.append(
                {
                    "stage": "exception",
                    "error": type(exc).__name__,
                    "message": (str(exc) or repr(exc))[:200],
                    "elapsed_s": round(_time.time() - group_t0, 3),
                }
            )
            row = {
                "purchasing_group_id": pg_id,
                "status": "ERROR",
                "error": type(exc).__name__,
                "error_message": (str(exc) or repr(exc))[:200] or None,
                "failure_point": "exception",
                "attempts": item["attempts"],
                "elapsed_s": round(_time.time() - group_t0, 2),
                "stage_trace": stage_trace[:12],
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
        "owner_auto_cleared": owner_clear,
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

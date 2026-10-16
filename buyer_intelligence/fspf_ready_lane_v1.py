"""READY priority lane for FSPF — access-resolved opportunities process now.

Scheduling only: same job, same funnel, same checkpoints. Unlocked PG
opportunities are promoted atomically from deferred → READY and interleave
with first-pass work (~25% READY / ~75% first-pass when both have work).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

log = logging.getLogger("govtracker.fspf_ready_lane_v1")

BUILD = "20261016-m3-fspf-ready-lane-v2"
OUT = "_og_commercial_research_pipeline_out"
READY_QUEUE_FILE = f"{OUT}/m3_fspf_v1_ready_queue.json"
DEFERRED_QUEUE_FILE = f"{OUT}/m3_fspf_v1_deferred_queue.json"

# Fair capacity: 1 READY per 3 first-pass when both have work (≈25%/75%).
READY_RATIO_NUM = int(os.environ.get("M3_FSPF_READY_RATIO_NUM") or "1")
READY_RATIO_DEN = int(os.environ.get("M3_FSPF_READY_RATIO_DEN") or "4")

# Software defects repaired in this READY build — allow one more promote after
# ready_fail_count cap for opportunities that failed the prior broken path.
REPAIR_RETRY_BUILD = "v4e26-solicitation-resolution"
_REPAIRABLE_BLOCKERS = {
    "DETAIL_FETCH_EMPTY",
    "SOLICITATION_RESOLUTION_FAILED",
    "ABSTRACT_FETCH_FAILED_OR_WAF",
}

_COMMON = re.compile(
    r"\b(tool|tools|screw|fastener|mro|lubricant|filter|toner|paper|janitorial|"
    r"ppe|glove|hardware|electrical|lighting|lamp|led|office|furniture|automotive|"
    r"windshield|cable|battery|hose|valve|bearing|abrasive|safety|supply|supplies)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _load(rel: str) -> Any:
    import json
    from pathlib import Path

    path = Path(data_path(rel))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(rel: str, obj: Any) -> None:
    from m3_jobs.store import _atomic_write
    from pathlib import Path

    _atomic_write(Path(data_path(rel)), obj)


def load_ready_queue() -> dict[str, Any]:
    return _load(READY_QUEUE_FILE) or {
        "kind": "FullScaleReadyQueue",
        "build": BUILD,
        "items": [],
        "count": 0,
        "updated_at": _utc(),
    }


def _persist_ready(q: dict[str, Any]) -> dict[str, Any]:
    items = [x for x in (q.get("items") or []) if isinstance(x, dict) and x.get("opportunity_id")]
    # Dedupe by oid, keep highest priority / newest promotion
    by_oid: dict[str, dict[str, Any]] = {}
    for it in items:
        oid = str(it.get("opportunity_id") or "").strip()
        if not oid:
            continue
        prev = by_oid.get(oid)
        if prev is None or float(it.get("priority_score") or 0) >= float(
            prev.get("priority_score") or 0
        ):
            by_oid[oid] = it
    ordered = sorted(
        by_oid.values(),
        key=lambda r: (
            -float(r.get("priority_score") or 0),
            str(r.get("promoted_at") or ""),
        ),
    )
    out = {
        "kind": "FullScaleReadyQueue",
        "build": BUILD,
        "items": ordered,
        "count": len(ordered),
        "updated_at": _utc(),
        "metrics": dict(q.get("metrics") or {}),
    }
    _save(READY_QUEUE_FILE, out)
    return out


def ready_count() -> int:
    return int(load_ready_queue().get("count") or 0)


def _priority_score(
    *,
    entry: dict[str, Any],
    checkpoint: dict[str, Any] | None,
    queue_row: dict[str, Any] | None,
) -> float:
    score = 50.0
    cp = checkpoint or {}
    row = queue_row or {}
    title = str(row.get("title") or row.get("product") or cp.get("title") or "")
    if _COMMON.search(title):
        score += 20
    if cp.get("complete_spec") or entry.get("complete_spec"):
        score += 25
    if int(cp.get("documents_downloaded") or entry.get("documents_downloaded") or 0) > 0:
        score += 15
    if cp.get("line_items") or entry.get("line_items"):
        score += 15
    if entry.get("commercial_resume") or cp.get("commercial_resume"):
        score += 20
    nxt = str(entry.get("next_stage") or cp.get("next_stage") or "").upper()
    if nxt in {"SOURCE", "PRICE", "ECONOMICS", "FINANCE", "BID_PREP"}:
        score += 18
    # Live deadline runway if known
    days = row.get("days_left")
    if days is None:
        days = cp.get("days_left")
    try:
        dleft = float(days) if days is not None else None
    except Exception:
        dleft = None
    if dleft is not None:
        if dleft < 0:
            score -= 80  # expired — demote hard
        elif 2 <= dleft <= 21:
            score += 30
        elif 21 < dleft <= 45:
            score += 15
    return score


def promote_unlock_eligible_to_ready(
    *,
    job_id: str | None = None,
    max_promote: int | None = None,
) -> dict[str, Any]:
    """Atomically promote recovery_eligible deferred items into the READY lane.

    Order: write READY first, then mark/remove from deferred. An oid is never
    dropped from deferred until it is durably present in READY.
    """
    from m3_jobs.store import JobStore

    family = "full_scale_production_funnel_v1"
    store = JobStore(family)
    if not job_id:
        active = store.find_active_job(stale_seconds=86400, mutate_stale=False) or {}
        job_id = str(active.get("JOB_ID") or "") or None

    dq = _load(DEFERRED_QUEUE_FILE) or {"items": []}
    deferred_items = list(dq.get("items") or [])
    ready = load_ready_queue()
    ready_oids = {
        str(x.get("opportunity_id"))
        for x in (ready.get("items") or [])
        if isinstance(x, dict) and x.get("opportunity_id")
    }

    queue_rows: dict[str, dict[str, Any]] = {}
    try:
        from buyer_intelligence.full_scale_production_funnel_v1 import QUEUE_FILE, _load as fspf_load

        qman = fspf_load(QUEUE_FILE) or {}
        for r in qman.get("rows") or []:
            if isinstance(r, dict) and r.get("opportunity_id"):
                queue_rows[str(r["opportunity_id"])] = r
    except Exception:
        pass

    promoted: list[str] = []
    skipped_paid = 0
    skipped_done = 0
    skipped_already = 0
    candidates = [
        d
        for d in deferred_items
        if isinstance(d, dict)
        and d.get("recovery_eligible")
        and d.get("opportunity_id")
        and not d.get("ready_promoted")
    ]

    skipped_retry_cap = 0
    for d in candidates:
        if max_promote is not None and len(promoted) >= max_promote:
            break
        oid = str(d.get("opportunity_id")).strip()
        if not oid:
            continue
        if oid in ready_oids:
            skipped_already += 1
            # Still mark deferred so ownership is clear
            d["ready_promoted"] = True
            d["ready_promoted_at"] = d.get("ready_promoted_at") or _utc()
            continue

        # Do not endlessly re-promote the same READY failure during first-pass.
        fail_n = int(d.get("ready_fail_count") or 0)
        prior_blocker = str(d.get("terminal_blocker") or d.get("ready_demote_reason") or "")
        repair_eligible = (
            fail_n >= 2
            and prior_blocker.split(":")[0] in _REPAIRABLE_BLOCKERS
            and str(d.get("ready_repair_retry_build") or "") != REPAIR_RETRY_BUILD
        )
        if fail_n >= 2 and not repair_eligible:
            skipped_retry_cap += 1
            continue

        cp = store.load_item(job_id, oid) if job_id else None
        result = str((cp or {}).get("RESULT") or "")
        if result in {
            "SUCCESS",
            "PARTIAL",
            "SKIPPED_ALREADY_COMPLETE",
            "EXPIRED_EXIT",
            "INELIGIBLE_EXIT",
        }:
            skipped_done += 1
            continue

        blocker = str(
            (cp or {}).get("terminal_blocker") or d.get("terminal_blocker") or ""
        )
        # Paid walls are external entitlements — never re-promote on stale pg_unlocked.
        # REGISTERED PG membership does not grant paid subscription documents.
        if blocker.startswith("PAID_ENTITLEMENT:"):
            skipped_paid += 1
            continue
        if str(d.get("additional_access_dependency") or "").startswith("PAID_ENTITLEMENT"):
            skipped_paid += 1
            continue

        row = queue_rows.get(oid)
        score = _priority_score(entry=d, checkpoint=cp, queue_row=row)
        if score < 0:
            continue

        entry = {
            "opportunity_id": oid,
            "pg_unlocked": d.get("pg_unlocked")
            or (cp or {}).get("purchasing_group_id"),
            "shared_access_key": d.get("shared_access_key")
            or (cp or {}).get("shared_access_key"),
            "queue_index": d.get("queue_index")
            if d.get("queue_index") is not None
            else (cp or {}).get("INDEX"),
            "stage_reached": d.get("stage_reached")
            or (cp or {}).get("stage_reached")
            or "ACCESS_DOCS",
            "next_stage": d.get("next_stage") or (cp or {}).get("next_stage"),
            "last_completed_stage": d.get("last_completed_stage")
            or (cp or {}).get("last_completed_stage"),
            "commercial_resume": bool(
                d.get("commercial_resume") or (cp or {}).get("commercial_resume")
            ),
            "documents_downloaded": int(
                (cp or {}).get("documents_downloaded") or 0
            ),
            "complete_spec": bool((cp or {}).get("complete_spec")),
            "priority_score": score,
            "promoted_at": _utc(),
            "source": "pg_unlock_recovery",
            "job_id": job_id,
            "lane": "READY",
            "repair_retry": bool(repair_eligible),
            "repair_retry_build": REPAIR_RETRY_BUILD if repair_eligible else None,
        }
        if repair_eligible:
            d["ready_repair_retry_build"] = REPAIR_RETRY_BUILD
            d["ready_fail_count"] = 0  # one fresh attempt after shared repair
        # ATOMIC STEP 1: durable READY membership first
        ready_items = list(ready.get("items") or [])
        ready_items = [
            x
            for x in ready_items
            if not (isinstance(x, dict) and str(x.get("opportunity_id")) == oid)
        ]
        ready_items.append(entry)
        ready["items"] = ready_items
        ready = _persist_ready(ready)
        ready_oids.add(oid)

        # ATOMIC STEP 2: mark deferred promoted (keep until READY persist confirmed)
        d["ready_promoted"] = True
        d["ready_promoted_at"] = _utc()
        d["ready_lane"] = True
        promoted.append(oid)

    # Remove promoted items from active deferred scheduling (they live in READY).
    # Keep a slim tombstone trail for audit via ready_promoted flag in a side list.
    still_deferred = []
    promoted_set = set(promoted) | {
        str(x.get("opportunity_id"))
        for x in (ready.get("items") or [])
        if isinstance(x, dict) and x.get("opportunity_id")
    }
    for d in deferred_items:
        if not isinstance(d, dict):
            continue
        oid = str(d.get("opportunity_id") or "")
        if oid in promoted_set and d.get("ready_promoted"):
            continue  # owned by READY lane now
        still_deferred.append(d)

    dq["items"] = still_deferred
    dq["count"] = len(still_deferred)
    dq["pg_unlock_eligible_count"] = sum(
        1 for x in still_deferred if isinstance(x, dict) and x.get("recovery_eligible")
    )
    dq["ready_promoted_count"] = int(dq.get("ready_promoted_count") or 0) + len(promoted)
    dq["updated_at"] = _utc()
    _save(DEFERRED_QUEUE_FILE, dq)

    metrics = dict(ready.get("metrics") or {})
    metrics["total_promoted"] = int(metrics.get("total_promoted") or 0) + len(promoted)
    metrics["last_promote_at"] = _utc()
    ready["metrics"] = metrics
    ready = _persist_ready(ready)

    return {
        "ok": True,
        "build": BUILD,
        "promoted": len(promoted),
        "promoted_ids_sample": promoted[:40],
        "ready_count": ready.get("count"),
        "deferred_remaining": dq.get("count"),
        "skipped_already": skipped_already,
        "skipped_done": skipped_done,
        "skipped_paid": skipped_paid,
        "skipped_retry_cap": skipped_retry_cap,
        "job_id": job_id,
    }


def demote_ready_to_deferred(
    *,
    opportunity_id: str,
    reason: str,
    stage: str | None = None,
    blocker: str | None = None,
    defect_key: str | None = None,
    keep_recovery_eligible: bool = True,
    pg_unlocked: str | None = None,
    next_stage: str | None = None,
    last_completed_stage: str | None = None,
    commercial_resume: bool | None = None,
    queue_index: int | None = None,
) -> dict[str, Any]:
    """Return a failed READY opportunity to deferred (priority 3). Idempotent."""
    oid = str(opportunity_id or "").strip()
    ready = load_ready_queue()
    remaining = []
    removed = None
    for x in ready.get("items") or []:
        if isinstance(x, dict) and str(x.get("opportunity_id")) == oid:
            removed = x
            continue
        remaining.append(x)
    ready["items"] = remaining
    metrics = dict(ready.get("metrics") or {})
    metrics["demoted"] = int(metrics.get("demoted") or 0) + (1 if removed else 0)
    ready["metrics"] = metrics
    _persist_ready(ready)

    # Re-enter deferred via FSPF helper (preserves unlock flags when eligible)
    try:
        from buyer_intelligence.full_scale_production_funnel_v1 import _defer_opportunity

        blocker_s = str(blocker or reason or "")
        # Additional paid entitlement after PG unlock is an external dependency —
        # keep the registered PG status elsewhere, but stop READY promotion until
        # that entitlement changes.
        paid_extra = blocker_s.startswith("PAID_ENTITLEMENT")
        still_eligible = bool(keep_recovery_eligible) and not paid_extra
        _defer_opportunity(
            opportunity_id=oid,
            stage=stage or (removed or {}).get("stage_reached"),
            outcome="DEFERRED_ERROR" if defect_key else "DEFERRED_EXTERNAL",
            defect_key=defect_key,
            shared_access_key=(removed or {}).get("shared_access_key"),
            blocker=blocker_s[:160],
            index=queue_index
            if queue_index is not None
            else (removed or {}).get("queue_index"),
            recovery_eligible=still_eligible,
            pg_unlocked=pg_unlocked or (removed or {}).get("pg_unlocked"),
            next_stage=next_stage or (removed or {}).get("next_stage"),
            last_completed_stage=last_completed_stage
            or (removed or {}).get("last_completed_stage"),
            commercial_resume=commercial_resume
            if commercial_resume is not None
            else (removed or {}).get("commercial_resume"),
        )
        # Stamp fail count so promote() does not thrash the same oid.
        dq = _load(DEFERRED_QUEUE_FILE) or {"items": []}
        for d in dq.get("items") or []:
            if isinstance(d, dict) and str(d.get("opportunity_id")) == oid:
                d["ready_fail_count"] = int(d.get("ready_fail_count") or 0) + 1
                d["ready_promoted"] = False
                d["ready_demoted_at"] = _utc()
                d["ready_demote_reason"] = (reason or "")[:160]
                d["terminal_blocker"] = blocker_s[:160] or d.get("terminal_blocker")
                if paid_extra:
                    d["additional_access_dependency"] = blocker_s[:160]
                    d["recovery_eligible"] = False
                    d["ready_blocked_external"] = True
                break
        dq["updated_at"] = _utc()
        _save(DEFERRED_QUEUE_FILE, dq)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "opportunity_id": oid}

    return {"ok": True, "opportunity_id": oid, "reason": reason, "removed": bool(removed)}


def remove_from_ready(opportunity_id: str, *, reason: str = "completed") -> dict[str, Any]:
    oid = str(opportunity_id or "").strip()
    ready = load_ready_queue()
    before = int(ready.get("count") or 0)
    ready["items"] = [
        x
        for x in (ready.get("items") or [])
        if not (isinstance(x, dict) and str(x.get("opportunity_id")) == oid)
    ]
    metrics = dict(ready.get("metrics") or {})
    if reason == "completed":
        metrics["completed"] = int(metrics.get("completed") or 0) + 1
    ready["metrics"] = metrics
    ready = _persist_ready(ready)
    return {
        "ok": True,
        "opportunity_id": oid,
        "reason": reason,
        "before": before,
        "after": ready.get("count"),
    }


def pick_ready_batch(
    *,
    job_id: str,
    store: Any,
    rows: list[dict[str, Any]],
    limit: int,
) -> list[tuple[int, dict[str, Any]]]:
    """Build processable READY items (resume from checkpoint). Does not mutate cursor."""
    from buyer_intelligence.full_scale_production_funnel_v1 import _PRESERVE_RESULTS

    ready = load_ready_queue()
    out: list[tuple[int, dict[str, Any]]] = []
    for d in ready.get("items") or []:
        if len(out) >= limit:
            break
        if not isinstance(d, dict):
            continue
        oid = str(d.get("opportunity_id") or "").strip()
        if not oid:
            continue
        prev = store.load_item(job_id, oid)
        if prev and str(prev.get("RESULT") or "") in _PRESERVE_RESULTS | {
            "SUCCESS",
            "PARTIAL",
            "SKIPPED_ALREADY_COMPLETE",
        }:
            remove_from_ready(oid, reason="already_complete")
            continue
        idx = int(d.get("queue_index") or -1)
        item = rows[idx] if 0 <= idx < len(rows) else {"opportunity_id": oid}
        if str(item.get("opportunity_id") or "") != oid:
            item = {"opportunity_id": oid}
            for j, r in enumerate(rows):
                if str(r.get("opportunity_id")) == oid:
                    item = r
                    idx = j
                    break
        item = dict(item)
        item["opportunity_id"] = oid
        item["_ready_lane"] = True
        item["_prior_checkpoint"] = prev or {}
        item["recovery_eligible"] = True
        item["PG_UNLOCK_ELIGIBLE"] = True
        item["purchasing_group_id"] = (
            d.get("pg_unlocked")
            or (prev or {}).get("purchasing_group_id")
            or item.get("purchasing_group_id")
        )
        item["shared_access_key"] = (
            d.get("shared_access_key")
            or (prev or {}).get("shared_access_key")
            or (f"pg:{item['purchasing_group_id']}" if item.get("purchasing_group_id") else None)
        )
        item["commercial_resume"] = bool(
            d.get("commercial_resume") or (prev or {}).get("commercial_resume")
        )
        item["next_stage"] = (
            d.get("next_stage")
            or (prev or {}).get("next_stage")
            or d.get("stage_reached")
            or (prev or {}).get("stage_reached")
        )
        item["last_completed_stage"] = d.get("last_completed_stage") or (
            prev or {}
        ).get("last_completed_stage")
        item["stage_reached"] = (
            d.get("stage_reached") or (prev or {}).get("stage_reached") or "ACCESS_DOCS"
        )
        for key in (
            "line_items",
            "recovered_documents",
            "complete_spec",
            "documents_downloaded",
            "authoritative_url",
            "title",
            "buyer",
        ):
            if (prev or {}).get(key) and not item.get(key):
                item[key] = prev.get(key)
        if not item.get("authoritative_url"):
            item["authoritative_url"] = (prev or {}).get("AUTHORITATIVE_URL")
        out.append((idx, item))
    return out


def interleave_fair(
    first_pass: list[tuple[int, dict[str, Any]]],
    ready: list[tuple[int, dict[str, Any]]],
    *,
    slice_size: int,
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, Any]]:
    """≈75% first-pass / 25% READY when both have work."""
    if not ready:
        return first_pass[:slice_size], {
            "first_pass": min(len(first_pass), slice_size),
            "ready": 0,
            "ratio": "100/0",
        }
    if not first_pass:
        return ready[:slice_size], {
            "first_pass": 0,
            "ready": min(len(ready), slice_size),
            "ratio": "0/100",
        }
    den = max(1, READY_RATIO_DEN)
    num = max(0, min(READY_RATIO_NUM, den - 1))
    # e.g. num=1 den=4 → every 4th slot is READY
    out: list[tuple[int, dict[str, Any]]] = []
    fi = 0
    ri = 0
    slot = 0
    while len(out) < slice_size and (fi < len(first_pass) or ri < len(ready)):
        want_ready = num > 0 and (slot % den) >= (den - num)
        if want_ready and ri < len(ready):
            out.append(ready[ri])
            ri += 1
        elif fi < len(first_pass):
            out.append(first_pass[fi])
            fi += 1
        elif ri < len(ready):
            out.append(ready[ri])
            ri += 1
        else:
            break
        slot += 1
    return out, {
        "first_pass": fi,
        "ready": ri,
        "ratio": f"{den - num}/{num}" if num else "100/0",
        "slice_size": len(out),
    }


def audit_ready_attempts(*, job_id: str | None = None, limit: int = 200) -> dict[str, Any]:
    """Classify READY-lane attempts from demotions + READY-flagged checkpoints.

    Mutually exclusive primary outcome per opportunity (latest evidence wins).
    """
    from collections import Counter

    from m3_jobs.store import JobStore

    family = "full_scale_production_funnel_v1"
    store = JobStore(family)
    if not job_id:
        active = store.find_active_job(stale_seconds=86400, mutate_stale=False) or {}
        job_id = str(active.get("JOB_ID") or "") or None

    dq = _load(DEFERRED_QUEUE_FILE) or {"items": []}
    ready = load_ready_queue()
    by_oid: dict[str, dict[str, Any]] = {}

    def _cat(blocker: str, *, docs: int = 0, lines: int = 0, result: str = "") -> str:
        b = (blocker or "").upper()
        if result in {"SUCCESS", "PARTIAL"} or (docs > 0 and lines > 0 and "DEFERRED" not in result):
            return "SUCCESSFULLY_PROCESSED"
        if b.startswith("PAID_ENTITLEMENT"):
            return "ADDITIONAL_ENTITLEMENT_REQUIRED"
        if "AUTH" in b or "SESSION" in b or "LOGIN" in b:
            return "AUTH_SESSION_INVALID"
        if b in {"SOLICITATION_RESOLUTION_FAILED"} or "NO_INTERCEPTION" in b:
            return "SOLICITATION_ID_RESOLUTION_FAILED"
        if b in {"DETAIL_FETCH_EMPTY", "ABSTRACT_FETCH_FAILED_OR_WAF"}:
            return "DETAIL_NAVIGATION_FAILED"
        if "DOCUMENT_TAB" in b or "DOCS_TAB" in b:
            return "DOCUMENT_TAB_FAILED"
        if "DOWNLOAD" in b or "ATTACHMENT" in b:
            return "DOCUMENT_DOWNLOAD_FAILED"
        if "UNSUPPORTED" in b or "FORMAT" in b:
            return "DOCUMENT_FORMAT_UNSUPPORTED"
        if "EXTRACT" in b or "PARSER" in b:
            return "DOCUMENT_EXTRACTION_FAILED"
        if "INCOMPLETE" in b:
            return "INCOMPLETE_SPECIFICATIONS"
        if "TIMEOUT" in b or "BUDGET" in b:
            return "STAGE_TIMEOUT"
        if "UNAVAILABLE" in b or "WAF" in b or "SOURCE_UNAVAILABLE" in b:
            return "SOURCE_UNAVAILABLE"
        if "RESTRICT" in b:
            return "LEGITIMATE_SOLICITATION_RESTRICTION"
        if "AWAITING_PURCHASING" in b or "PURCHASING_GROUP" in b:
            return "ACCESS_SCOPE_INSUFFICIENT"
        if b:
            return f"OTHER:{b[:50]}"
        return "OTHER:UNKNOWN"

    for d in dq.get("items") or []:
        if not isinstance(d, dict):
            continue
        if not (
            d.get("ready_fail_count")
            or d.get("ready_demoted_at")
            or d.get("ready_demote_reason")
            or d.get("ready_promoted")
            or d.get("ready_lane")
        ):
            continue
        oid = str(d.get("opportunity_id") or "").strip()
        if not oid:
            continue
        blocker = str(d.get("terminal_blocker") or d.get("ready_demote_reason") or "")
        by_oid[oid] = {
            "opportunity_id": oid,
            "purchasing_group_id": d.get("pg_unlocked"),
            "stage_reached": d.get("stage_reached"),
            "terminal_blocker": blocker,
            "ready_fail_count": int(d.get("ready_fail_count") or 0),
            "ready_demoted_at": d.get("ready_demoted_at"),
            "additional_access_dependency": d.get("additional_access_dependency"),
            "recovery_eligible": d.get("recovery_eligible"),
            "source": "deferred_demotion",
            "outcome": _cat(blocker),
        }

    if job_id:
        try:
            items = store.list_items(job_id)
        except Exception:
            items = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if not (it.get("READY_LANE") or it.get("ready_processed_at")):
                continue
            oid = str(it.get("OPPORTUNITY_ID") or it.get("opportunity_id") or "").strip()
            if not oid:
                continue
            blocker = str(it.get("terminal_blocker") or "")
            docs = int(it.get("documents_downloaded") or 0)
            lines = int(it.get("fully_specified_product_lines") or it.get("line_count") or 0)
            result = str(it.get("RESULT") or "")
            row = {
                "opportunity_id": oid,
                "purchasing_group_id": it.get("purchasing_group_id"),
                "authoritative_url": (it.get("authoritative_url") or "")[:160],
                "bidnet_solicitation_id": it.get("bidnet_solicitation_id"),
                "registration_status": it.get("purchasing_group_registration_status"),
                "stage_reached": it.get("stage_reached"),
                "terminal_blocker": blocker,
                "documents_downloaded": docs,
                "line_count": lines,
                "early_search_status": it.get("early_search_status"),
                "search_to_detail_status": it.get("search_to_detail_status"),
                "bounded_blocker": it.get("bounded_blocker"),
                "RESULT": result,
                "source": "ready_checkpoint",
                "outcome": _cat(blocker, docs=docs, lines=lines, result=result),
            }
            # Prefer checkpoint over demotion tombstone when both exist
            prior = by_oid.get(oid)
            if prior is None or row["source"] == "ready_checkpoint":
                if prior and prior.get("ready_fail_count"):
                    row["ready_fail_count"] = prior["ready_fail_count"]
                by_oid[oid] = row

    for d in ready.get("items") or []:
        if not isinstance(d, dict):
            continue
        oid = str(d.get("opportunity_id") or "").strip()
        if not oid or oid in by_oid:
            continue
        by_oid[oid] = {
            "opportunity_id": oid,
            "purchasing_group_id": d.get("pg_unlocked"),
            "stage_reached": d.get("stage_reached"),
            "source": "ready_queue_pending",
            "outcome": "PENDING_READY",
        }

    cats = Counter(r["outcome"] for r in by_oid.values())
    examples: dict[str, list[dict[str, Any]]] = {}
    for r in by_oid.values():
        cat = r["outcome"]
        examples.setdefault(cat, [])
        if len(examples[cat]) < 8:
            examples[cat].append(r)

    return {
        "ok": True,
        "build": BUILD,
        "repair_retry_build": REPAIR_RETRY_BUILD,
        "job_id": job_id,
        "total_examined": len(by_oid),
        "by_outcome": dict(cats),
        "examples": examples,
        "ready_queue_count": ready.get("count"),
        "metrics": ready.get("metrics"),
        "limit_note": f"capped_examples_per_category=8 examined={len(by_oid)}",
    }


def ready_status() -> dict[str, Any]:
    q = load_ready_queue()
    return {
        "kind": "FullScaleReadyLaneStatus",
        "build": BUILD,
        "count": q.get("count"),
        "metrics": q.get("metrics"),
        "sample_ids": [
            str(x.get("opportunity_id"))
            for x in (q.get("items") or [])[:15]
            if isinstance(x, dict)
        ],
        "ratio_target": f"{READY_RATIO_DEN - READY_RATIO_NUM}:{READY_RATIO_NUM}",
        "updated_at": q.get("updated_at"),
    }

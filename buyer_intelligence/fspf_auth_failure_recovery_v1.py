"""Reclassify opportunities incorrectly concluded during BidNet browser outages.

Does not reset the universe or cursor. Marks browser/auth-defect rows as
READY-eligible again (once) when they have a registered PG, without promoting
paid-entitlement walls.
"""

from __future__ import annotations

import logging
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

log = logging.getLogger("govtracker.fspf_auth_failure_recovery_v1")

BUILD = "20261016-m3-fspf-auth-failure-recovery-v1b"
OUT = "_og_commercial_research_pipeline_out"
DEFERRED_QUEUE_FILE = f"{OUT}/m3_fspf_v1_deferred_queue.json"

_BROWSER_AUTH_MARKERS = {
    "AUTH_FAILED",
    "BROWSER_STARTUP_FAILED",
    "BROWSER_PROCESS_FAILED",
    "BROWSER_CONTEXT_INVALID",
    "BROWSER_SLOT_BUSY",
    "SESSION_RESTORE_FAILED",
    "SESSION_EXPIRED",
    "AUTH_PROBE_FAILED",
    "BIDNET_AUTH_TRANSIENT",
    "AUTH_REFRESH_IN_PROGRESS",
    "browser_auth_transient",
    "browser_unavailable_deferred",
    "browser_slot_busy",
}


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


def _is_browser_auth_defect(entry: dict[str, Any]) -> bool:
    blocker = str(entry.get("terminal_blocker") or "")
    defect = str(entry.get("software_defect_key") or "")
    fetch = str(entry.get("fetch_via") or "")
    for token in _BROWSER_AUTH_MARKERS:
        if token in blocker or token in defect or token in fetch:
            return True
    if blocker.startswith("browser_unavailable") or "BROWSER_" in blocker.upper():
        return True
    return False


def requeue_browser_auth_failures(
    *,
    job_id: str | None = None,
    max_requeue: int = 200,
) -> dict[str, Any]:
    """Clear false permanent conclusions caused by browser/auth outages.

    - Keeps paid entitlement rows blocked.
    - Restores recovery_eligible + resets ready_fail_count for browser defects
      that have a purchasing-group unlock key (READY lane can pick them up).
    - Does not touch first-pass cursor.
    """
    from bidnet_auth.purchasing_group_tracker import is_registered

    dq = _load(DEFERRED_QUEUE_FILE) or {"items": []}
    items = list(dq.get("items") or [])
    touched = 0
    skipped_paid = 0
    skipped_no_pg = 0
    samples: list[str] = []

    for d in items:
        if touched >= max_requeue:
            break
        if not isinstance(d, dict) or not d.get("opportunity_id"):
            continue
        if not _is_browser_auth_defect(d):
            continue
        blocker = str(d.get("terminal_blocker") or "")
        if blocker.startswith("PAID_ENTITLEMENT") or d.get("additional_access_dependency"):
            skipped_paid += 1
            continue
        # Pure browser/slot outages must retry even without a registered PG —
        # first-pass deferred them solely because Chromium was dead/busy.
        pure_browser = _is_browser_auth_defect(d) and (
            any(
                tok in blocker or tok in str(d.get("software_defect_key") or "")
                for tok in (
                    "BROWSER_SLOT_BUSY",
                    "BROWSER_STARTUP_FAILED",
                    "BROWSER_PROCESS_FAILED",
                    "BROWSER_CONTEXT_INVALID",
                    "browser_slot_busy",
                    "browser_auth_transient",
                    "browser_unavailable",
                    "AUTH_FAILED",
                    "BIDNET_AUTH_TRANSIENT",
                )
            )
            or str(d.get("fetch_via") or "") in _BROWSER_AUTH_MARKERS
        )
        pg = str(d.get("pg_unlocked") or "").strip()
        if not pure_browser:
            if not pg:
                # Still clear incorrect solicitation-resolution conclusions from auth outages
                if str(d.get("terminal_blocker") or "") in {
                    "SOLICITATION_RESOLUTION_FAILED",
                    "DETAIL_FETCH_EMPTY",
                    "NO_INTERCEPTION_RESULT",
                } and str(d.get("software_defect_key") or "") in _BROWSER_AUTH_MARKERS:
                    pass
                else:
                    skipped_no_pg += 1
                    continue
            if pg and not is_registered(pg):
                skipped_no_pg += 1
                continue
        elif pg and not is_registered(pg):
            # Browser defect with a known unpaid PG key — still requeue; PG
            # manager may have registered since the outage.
            pass

        d["recovery_eligible"] = True
        d["ready_promoted"] = False
        d["ready_fail_count"] = 0
        d["auth_repair_requeued_at"] = _utc()
        d["auth_repair_build"] = BUILD
        # Remove false solicitation conclusions caused while browser was down
        if str(d.get("terminal_blocker") or "") in {
            "SOLICITATION_RESOLUTION_FAILED",
            "DETAIL_FETCH_EMPTY",
            "NO_INTERCEPTION_RESULT",
            "AUTH_FAILED",
            "BROWSER_SLOT_BUSY",
        } or str(d.get("terminal_blocker") or "").startswith("BROWSER_"):
            d["previous_terminal_blocker"] = d.get("terminal_blocker")
            d["terminal_blocker"] = "AWAITING_AUTH_REPAIR_RETRY"
        touched += 1
        if len(samples) < 20:
            samples.append(str(d["opportunity_id"]))

    dq["items"] = items
    dq["count"] = len(items)
    dq["pg_unlock_eligible_count"] = sum(
        1 for x in items if isinstance(x, dict) and x.get("recovery_eligible")
    )
    dq["auth_repair_requeued"] = int(dq.get("auth_repair_requeued") or 0) + touched
    dq["updated_at"] = _utc()
    _save(DEFERRED_QUEUE_FILE, dq)

    return {
        "ok": True,
        "build": BUILD,
        "job_id": job_id,
        "requeued": touched,
        "skipped_paid": skipped_paid,
        "skipped_no_pg": skipped_no_pg,
        "sample_ids": samples,
        "deferred_count": dq.get("count"),
        "pg_unlock_eligible_count": dq.get("pg_unlock_eligible_count"),
    }

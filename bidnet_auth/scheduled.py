"""Integrate BidNet auth + recovery into the existing scheduled discovery path.

Does NOT create a second scheduler. Called from m3_discovery_service._execute_run.
"""

from __future__ import annotations

import logging
from typing import Any

from bidnet_auth.config import load_bidnet_auth_config
from bidnet_auth.states import AUTH_CHALLENGE, AUTH_FAILED, DISABLED
from bidnet_auth.telemetry import owner_connection_status, record_recovery_counters

log = logging.getLogger("govtracker.bidnet_auth.scheduled")


def run_scheduled_bidnet_auth_recovery(
    *,
    run_id: str | None = None,
    trigger_type: str | None = None,
    limit: int | None = None,
    backlog_limit: int | None = None,
) -> dict[str, Any]:
    """Validate/login BidNet session then run incremental authenticated recovery.

    Failures are returned as structured blockers — callers must not abort non-BidNet discovery.
    """
    cfg = load_bidnet_auth_config()
    report: dict[str, Any] = {
        "kind": "ScheduledBidNetAuthRecovery",
        "run_id": run_id,
        "trigger_type": trigger_type,
        "auth_enabled": cfg.auth_enabled,
        "credentials_configured": cfg.credentials_present,
        "auth": None,
        "recovery": None,
        "skipped": False,
        "blocker": None,
    }

    if not cfg.auth_enabled:
        report["skipped"] = True
        report["blocker"] = DISABLED
        report["connection"] = owner_connection_status()
        log.info("BidNet auth recovery skipped — DISABLED")
        return report

    if not cfg.credentials_present:
        report["skipped"] = True
        report["blocker"] = AUTH_FAILED
        report["auth"] = {
            "status": AUTH_FAILED,
            "authenticated": False,
            "message": "BIDNET_USERNAME/BIDNET_PASSWORD not configured",
        }
        report["connection"] = owner_connection_status()
        log.warning("BidNet auth recovery skipped — credentials missing")
        return report

    new_limit = int(limit if limit is not None else cfg.recovery_batch_size)
    backlog = int(backlog_limit if backlog_limit is not None else cfg.backlog_batch_size)
    harvest_n = int(cfg.harvest_batch_size or 100)
    # Incremental: list more, deep-recover fewer (new/priority via detail_limit)
    list_n = max(harvest_n, min(500, harvest_n * 2))
    detail_n = min(harvest_n, 100)

    # 1) Harvest CURRENT authenticated search results (not stale anonymous IDs)
    try:
        from bidnet_discovery import run_bidnet_authenticated_harvest

        harvest = run_bidnet_authenticated_harvest(
            max_results=list_n,
            max_pages=max(8, (list_n // 20) + 2),
            open_details=True,
            detail_limit=detail_n,
            persist=True,
            run_id=run_id,
            use_auth=True,
        )
        report["auth"] = harvest.get("auth")
        report["harvest"] = {
            "search_reachable": (harvest.get("harvest") or {}).get("search_reachable"),
            "reported_total": (harvest.get("harvest") or {}).get("reported_total"),
            "retrieved_total": (harvest.get("harvest") or {}).get("retrieved_total"),
            "pagination_method": (harvest.get("harvest") or {}).get("pagination_method"),
            "detail_stats": (harvest.get("harvest") or {}).get("detail_stats"),
            "canonical_merge": harvest.get("canonical_merge"),
            "blocker": harvest.get("blocker"),
        }
        if harvest.get("blocker") in {AUTH_CHALLENGE, AUTH_FAILED, DISABLED}:
            report["blocker"] = harvest.get("blocker")
            report["connection"] = owner_connection_status()
            log.warning("BidNet authenticated harvest blocked status=%s", harvest.get("blocker"))
            # Still attempt stale-ID recovery only if auth itself succeeded earlier — otherwise stop BidNet work
            if harvest.get("blocker") in {AUTH_CHALLENGE, AUTH_FAILED}:
                return report
    except Exception:
        report["harvest"] = {"error": "harvest_exception"}
        log.exception("BidNet authenticated harvest failed (non-fatal; continuing recovery)")

    # 2) Continue incremental recovery for existing BidNet backlog (uses auth client when available)
    try:
        from bidnet_recovery.batch import run_bidnet_recovery

        recovery = run_bidnet_recovery(
            limit=new_limit + backlog,
            batch_size=min(25, max(5, new_limit // 4 or 25)),
            resume=True,
            persist=True,
            force=False,
            use_auth=True,
            recovery_batch_size=new_limit,
            backlog_batch_size=backlog,
            run_id=run_id,
        )
        if not report.get("auth"):
            report["auth"] = recovery.get("auth")
        report["recovery"] = {
            "processed": recovery.get("processed"),
            "selected": recovery.get("selected"),
            "stats": recovery.get("stats"),
            "funnel": {
                "Detail_recovered": (recovery.get("funnel") or {}).get("Detail_recovered"),
                "Documents_recovered": (recovery.get("funnel") or {}).get("Documents_recovered"),
                "Economics_ready": (recovery.get("funnel") or {}).get("Economics_ready"),
            },
            "auth_stop": recovery.get("auth_stop"),
        }
        auth = recovery.get("auth") or report.get("auth") or {}
        status = auth.get("status")
        if status in {AUTH_CHALLENGE, AUTH_FAILED} or recovery.get("auth_stop"):
            report["blocker"] = status or recovery.get("auth_stop")
            log.warning(
                "BidNet authenticated recovery blocked status=%s reason=%s",
                status,
                auth.get("message") or recovery.get("auth_stop"),
            )
        else:
            st = recovery.get("stats") or {}
            record_recovery_counters(
                detail_recovered=int(st.get("detail_recovered") or 0),
                documents_recovered=int(st.get("documents_recovered") or 0),
                economics_ready=int(st.get("economics_ready") or 0),
                authenticated_run=bool(auth.get("authenticated")),
            )
            log.info(
                "BidNet scheduled auth recovery done auth=%s processed=%s docs=%s",
                status,
                recovery.get("processed"),
                st.get("documents_recovered"),
            )
    except Exception as exc:
        report["blocker"] = report.get("blocker") or AUTH_FAILED
        if not report.get("auth"):
            report["auth"] = {
                "status": AUTH_FAILED,
                "authenticated": False,
                "message": f"scheduled_recovery_error:{type(exc).__name__}",
            }
        log.exception("BidNet scheduled auth recovery failed (non-fatal to discovery)")

    report["connection"] = owner_connection_status()
    return report

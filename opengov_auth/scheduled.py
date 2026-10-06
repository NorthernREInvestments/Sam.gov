"""Integrate OpenGov auth discovery + recovery into existing scheduled discovery path."""

from __future__ import annotations

import logging
from typing import Any

from opengov_auth.config import load_opengov_auth_config
from opengov_auth.states import AUTH_CHALLENGE, AUTH_FAILED, DISABLED
from opengov_auth.telemetry import owner_connection_status

log = logging.getLogger("govtracker.opengov_auth.scheduled")


def run_scheduled_opengov_auth_pipeline(
    *,
    run_id: str | None = None,
    trigger_type: str | None = None,
    max_entities: int | None = None,
    recovery_limit: int | None = None,
) -> dict[str, Any]:
    """Run OpenGov authenticated discovery then recovery. Non-fatal to other sources."""
    cfg = load_opengov_auth_config()
    report: dict[str, Any] = {
        "kind": "ScheduledOpenGovAuthPipeline",
        "run_id": run_id,
        "trigger_type": trigger_type,
        "auth_enabled": cfg.auth_enabled,
        "credentials_configured": cfg.credentials_present,
        "discovery_scope": cfg.discovery_scope,
        "discovery": None,
        "recovery": None,
        "blocker": None,
        "skipped": False,
    }

    if not cfg.auth_enabled:
        report["skipped"] = True
        report["blocker"] = DISABLED
        report["connection"] = owner_connection_status()
        return report
    if not cfg.credentials_present:
        report["skipped"] = True
        report["blocker"] = AUTH_FAILED
        report["auth"] = {
            "status": AUTH_FAILED,
            "authenticated": False,
            "message": "OPENGOV_USERNAME/OPENGOV_PASSWORD not configured",
        }
        report["connection"] = owner_connection_status()
        log.warning("OpenGov scheduled pipeline skipped — credentials missing")
        return report

    try:
        # Cascade discovery: public → auth → agency; ANTI_BOT never terminal
        from opengov_discovery.cascade import run_opengov_cascade_discovery

        discovery = run_opengov_cascade_discovery(
            max_entities=max_entities if max_entities is not None else None,
            persist=True,
            run_id=run_id,
            use_auth=True,
            allow_browser=False,
            max_pages=6,
        )
        report["discovery"] = {
            "auth": discovery.get("auth"),
            "mode": "cascade",
            "entities_attempted": discovery.get("entities_attempted"),
            "entities_successful": discovery.get("entities_successful"),
            "raw_opportunities": discovery.get("raw_opportunities"),
            "unique_records": discovery.get("unique_records"),
            "net_new": discovery.get("net_new"),
            "portal_status_counts": discovery.get("portal_status_counts"),
            "route_counts": discovery.get("route_counts"),
            "anti_bot_primary_failures": discovery.get("anti_bot_primary_failures"),
            "anti_bot_recovered_via_fallback": discovery.get("anti_bot_recovered_via_fallback"),
            "recovery_blocked": discovery.get("recovery_blocked"),
            "vendor_global_search": discovery.get("vendor_global_search"),
            "canonical_merge": discovery.get("canonical_merge"),
            "resolver_telemetry": discovery.get("resolver_telemetry"),
            "blocker": discovery.get("blocker"),
        }
        report["auth"] = discovery.get("auth")
        if discovery.get("blocker") in {AUTH_CHALLENGE, AUTH_FAILED, DISABLED}:
            report["blocker"] = discovery.get("blocker")
            report["connection"] = owner_connection_status()
            log.warning("OpenGov discovery blocked status=%s", discovery.get("blocker"))
            return report
    except Exception as exc:
        report["blocker"] = AUTH_FAILED
        report["auth"] = {
            "status": AUTH_FAILED,
            "authenticated": False,
            "message": f"discovery_error:{type(exc).__name__}",
        }
        log.exception("OpenGov scheduled discovery failed (non-fatal)")
        report["connection"] = owner_connection_status()
        return report

    try:
        from opengov_recovery.batch import run_opengov_recovery

        recovery = run_opengov_recovery(
            limit=recovery_limit if recovery_limit is not None else cfg.recovery_batch_size,
            use_auth=True,
            persist=True,
            resume=True,
            run_id=run_id,
        )
        report["recovery"] = {
            "processed": recovery.get("processed"),
            "stats": recovery.get("stats"),
            "auth_stop": recovery.get("auth_stop"),
            "funnel": {
                "Documents_recovered": (recovery.get("funnel") or {}).get("Documents_recovered"),
                "Economics_ready": (recovery.get("funnel") or {}).get("Economics_ready"),
                "Detail_recovered": (recovery.get("funnel") or {}).get("Detail_recovered"),
            },
        }
        if recovery.get("auth_stop"):
            report["blocker"] = recovery.get("auth_stop")
    except Exception as exc:
        # Discovery already succeeded — recovery failure is non-fatal
        report["recovery"] = {"error": type(exc).__name__}
        log.exception("OpenGov scheduled recovery failed (non-fatal)")

    report["connection"] = owner_connection_status()
    return report

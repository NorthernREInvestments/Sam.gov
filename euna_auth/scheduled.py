"""Integrate Euna/Bonfire discovery into existing scheduled discovery path."""

from __future__ import annotations

import logging
from typing import Any

from euna_auth.config import load_euna_auth_config
from euna_auth.states import AUTH_FAILED, DISABLED, OPTIONAL_TARGETED_SOURCE, PAID_OPTIONAL
from euna_auth.telemetry import owner_connection_status

log = logging.getLogger("govtracker.euna_auth.scheduled")


def run_scheduled_euna_pipeline(
    *,
    run_id: str | None = None,
    trigger_type: str | None = None,
    max_entities: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run Euna/Bonfire discovery when allowed. Non-fatal to other sources.

    Default: SKIP nationwide discovery (PAID_OPTIONAL / targeted-only).
    Runs only when:
      - force=True (manual owner action), or
      - EUNA_NATIONAL_DISCOVERY_ENABLED=true, or
      - EUNA_ENABLED_STATES is non-empty (targeted states).
    """
    cfg = load_euna_auth_config()
    report: dict[str, Any] = {
        "kind": "ScheduledEunaPipeline",
        "run_id": run_id,
        "trigger_type": trigger_type,
        "auth_enabled": cfg.auth_enabled,
        "credentials_configured": cfg.credentials_present,
        "discovery_scope": cfg.discovery_scope,
        "source_role": cfg.source_role,
        "coverage_category": cfg.coverage_category,
        "national_discovery_enabled": cfg.national_discovery_enabled,
        "enabled_states": list(cfg.enabled_states),
        "discovery": None,
        "blocker": None,
        "skipped": False,
        "skip_reason": None,
    }

    if not force and not cfg.should_run_scheduled_discovery:
        report["skipped"] = True
        report["skip_reason"] = (
            "EUNA_NATIONAL_DISCOVERY_DISABLED"
            if not cfg.enabled_states
            else "EUNA_NO_ENABLED_STATES"
        )
        if not cfg.national_discovery_enabled and not cfg.enabled_states:
            report["skip_reason"] = "EUNA_NATIONAL_DISCOVERY_DISABLED"
        report["blocker"] = None  # not a failure — intentional paid-optional skip
        report["status"] = PAID_OPTIONAL
        report["source_role"] = OPTIONAL_TARGETED_SOURCE
        log.info(
            "Euna scheduled discovery skipped (paid optional; national=%s states=%s)",
            cfg.national_discovery_enabled,
            cfg.enabled_states or "NONE",
        )
        report["connection"] = owner_connection_status()
        return report

    # Targeted or explicitly enabled national path — preserve prior central discovery.
    try:
        from euna_discovery.central import run_euna_central_discovery

        discovery = run_euna_central_discovery(
            max_results=max(500, int(cfg.discovery_batch_size or 30) * 50),
            max_pages=40,
            persist=True,
            run_id=run_id,
            use_auth=cfg.auth_enabled and cfg.credentials_present,
            enrich_portals=False,
        )
        # Note: enabled_states is recorded for future portal/geo filter; central
        # Supplier Network is still the discovery root when entitlement exists.
        report["discovery"] = {
            "auth": discovery.get("auth"),
            "mode": "central_targeted" if cfg.enabled_states and not cfg.national_discovery_enabled else "central",
            "enabled_states": list(cfg.enabled_states),
            "central_reachable": discovery.get("central_reachable"),
            "reported_total": discovery.get("reported_total"),
            "retrieved_total": discovery.get("retrieved_total"),
            "working_public": discovery.get("working_public"),
            "raw_opportunities": discovery.get("raw_opportunities"),
            "unique_records": discovery.get("unique_records"),
            "net_new": discovery.get("net_new"),
            "pagination_complete": discovery.get("pagination_complete"),
            "account_category_restriction": discovery.get("account_category_restriction"),
            "account_entitlement": discovery.get("account_entitlement"),
            "canonical_merge": discovery.get("canonical_merge"),
            "blocker": discovery.get("blocker"),
        }
        report["auth"] = discovery.get("auth")
        report["blocker"] = discovery.get("blocker")
        if (discovery.get("auth") or {}).get("status") in {AUTH_FAILED, DISABLED}:
            report["blocker"] = report.get("blocker") or (discovery.get("auth") or {}).get("status")
    except Exception as exc:
        log.exception("Euna scheduled pipeline failed")
        report["blocker"] = type(exc).__name__
    report["connection"] = owner_connection_status()
    return report

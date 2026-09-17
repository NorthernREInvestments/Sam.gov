"""Discovery run orchestration — fixture-first, no SAM/OpenAI."""

from __future__ import annotations
from application_clock import now_utc

import uuid
from datetime import datetime, timezone
from typing import Any

from discovery.adapters import get_implemented_adapter, list_implemented_adapters
from discovery.health import apply_health_to_source_row, record_source_attempt
from discovery.persist import upsert_canonical_opportunity
from discovery.registry import seed_discovery_sources


def _new_run_id() -> str:
    return f"drun-{uuid.uuid4().hex[:16]}"


def run_fixture_discovery(
    session: Any,
    *,
    source_payloads: dict[str, str | bytes | dict[str, Any]],
    dry_run: bool = False,
    persist_registry: bool = True,
) -> dict[str, Any]:
    """
    Run discovery against fixture payloads only.
    external request counts always zero.
    """
    from models import DiscoveryRun, DiscoverySource

    if persist_registry:
        seed_discovery_sources(session)

    run_id = _new_run_id()
    started = now_utc()
    run = DiscoveryRun(
        run_id=run_id,
        started_at=started,
        dry_run=dry_run,
        external_request_counts_json={
            "SAM": 0,
            "OpenAI": 0,
            "USAspending": 0,
            "web": 0,
            "paid": 0,
        },
        cost_usd=0,
    )
    session.add(run)
    session.flush()

    metrics = {
        "sources_attempted": 0,
        "sources_successful": 0,
        "sources_failed": 0,
        "raw_notices_seen": 0,
        "new_opportunities": 0,
        "updated_opportunities": 0,
        "duplicates": 0,
        "core_product_count": 0,
        "product_plus_service_count": 0,
        "unknown_count": 0,
        "service_count": 0,
        "rejected_count": 0,
        "documents_discovered": 0,
        "per_source": {},
    }

    for source_id, payload in source_payloads.items():
        metrics["sources_attempted"] += 1
        adapter = get_implemented_adapter(source_id)
        src_row = session.query(DiscoverySource).filter_by(source_id=source_id).first()
        try:
            if adapter is None:
                raise ValueError(f"No implemented adapter for {source_id}")
            opps = adapter.parse_fixture(payload)
            metrics["raw_notices_seen"] += len(opps)
            added = updated = dups = docs = 0
            for opp in opps:
                result = upsert_canonical_opportunity(session, opp, dry_run=dry_run)
                if result.get("is_new"):
                    added += 1
                    metrics["new_opportunities"] += 1
                elif result.get("is_duplicate_sighting"):
                    dups += 1
                    metrics["duplicates"] += 1
                    metrics["updated_opportunities"] += 1
                    updated += 1
                else:
                    updated += 1
                    metrics["updated_opportunities"] += 1
                cls = result.get("classification")
                if cls == "CORE_PRODUCT":
                    metrics["core_product_count"] += 1
                elif cls == "PRODUCT_PLUS_SERVICE":
                    metrics["product_plus_service_count"] += 1
                elif cls == "SERVICE":
                    metrics["service_count"] += 1
                else:
                    metrics["unknown_count"] += 1
                if result.get("operator_status") == "REJECTED":
                    metrics["rejected_count"] += 1
                docs += int(result.get("document_count") or 0)
            metrics["documents_discovered"] += docs
            health = record_source_attempt(
                success=True,
                records_seen=len(opps),
                records_added=added,
                records_updated=updated,
            )
            if src_row and not dry_run:
                apply_health_to_source_row(src_row, health)
            metrics["sources_successful"] += 1
            metrics["per_source"][source_id] = {"ok": True, "count": len(opps), "health": health["health_status"]}
        except Exception as exc:
            health = record_source_attempt(
                success=False,
                failure_type=type(exc).__name__,
                prior_consecutive_failures=(src_row.consecutive_failures if src_row else 0),
            )
            if src_row and not dry_run:
                apply_health_to_source_row(src_row, health)
            metrics["sources_failed"] += 1
            metrics["per_source"][source_id] = {
                "ok": False,
                "error": str(exc),
                "health": health["health_status"],
                "isolated": True,
            }

    run.finished_at = now_utc()
    run.sources_attempted = metrics["sources_attempted"]
    run.sources_successful = metrics["sources_successful"]
    run.sources_failed = metrics["sources_failed"]
    run.raw_notices_seen = metrics["raw_notices_seen"]
    run.new_opportunities = metrics["new_opportunities"]
    run.updated_opportunities = metrics["updated_opportunities"]
    run.duplicates = metrics["duplicates"]
    run.core_product_count = metrics["core_product_count"]
    run.product_plus_service_count = metrics["product_plus_service_count"]
    run.unknown_count = metrics["unknown_count"]
    run.service_count = metrics["service_count"]
    run.rejected_count = metrics["rejected_count"]
    run.documents_discovered = metrics["documents_discovered"]
    run.metrics_json = metrics
    if not dry_run:
        session.flush()

    return {
        "run_id": run_id,
        "metrics": metrics,
        "adapters_available": list_implemented_adapters(),
        "external_request_counts": {
            "SAM": 0,
            "OpenAI": 0,
            "USAspending": 0,
            "web": 0,
            "paid": 0,
        },
        "cost_usd": 0,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }

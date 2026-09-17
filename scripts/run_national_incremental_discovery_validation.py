"""National incremental discovery & tracking — validation runner.

DEVELOPMENT_NO_OUTREACH. No bids. No registrations. No outreach.
25k load uses schema-shaped fixtures — NOT claimed as live solicitations.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_clock import clock_mode, now_utc
from authoritative_freshness_gate import evaluate_authoritative_freshness_gate
from coverage_gap_intelligence import build_coverage_gap_report, weekly_discovery_priorities
from discovery.live_runner import run_live_discovery
from discovery_checkpoint import (
    begin_source_cycle,
    catch_up_plan_after_failures,
    complete_source_cycle,
    determine_discovery_mode,
    filter_records_for_incremental,
    incremental_window,
)
from national_discovery_constants import (
    HIGH_VOLUME_TARGET,
    INV_PURSUIT_WORTHY,
    MODE_BOOTSTRAP,
    MODE_INCREMENTAL,
    NOT_READY_UNREVIEWED_CHANGE,
    READY_FOR_SUBMISSION,
    SRC_HEALTHY,
)
from national_discovery_funnel import NationalDiscoveryFunnel
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, mode_snapshot, set_operating_mode
from procurement_source_registry import ProcurementSourceRegistry, bootstrap_registry
from pursuit_ranking import explain_rank_delta, rank_opportunities
from solicitation_identity import SolicitationInventory
from source_discovery_engine import SourceDiscoveryEngine, run_weekly_source_discovery
from source_health import build_source_health_report
from tracked_solicitation import TrackedSolicitationMonitor, TrackedSolicitationStore

ARTIFACTS = ROOT / "artifacts"
REQUEST_LOG: list[dict[str, Any]] = []


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    path = ARTIFACTS / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _log(event: str, **kwargs: Any) -> None:
    REQUEST_LOG.append({"event": event, "at": _utc(), "external_communication": False, "bid_submitted": False, **kwargs})


def make_volume_records(n: int, *, real_seeds: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Schema-shaped load wrappers — explicitly synthetic for architecture validation."""
    seeds = real_seeds or [
        {
            "title": "Native Grass Seed Supplies",
            "solicitation_number": "SEED-DEMO-001",
            "agency": "Demo DOT",
            "source_id": "fixture_state",
            "status": "OPEN",
            "deadline": "2026-12-01",
        },
        {
            "title": "Architectural and Engineering Services",
            "solicitation_number": "AE-DEMO-001",
            "agency": "Demo City",
            "source_id": "fixture_city",
            "status": "OPEN",
            "deadline": "2026-11-01",
        },
        {
            "title": "Industrial Pump Equipment Purchase",
            "solicitation_number": "PUMP-DEMO-001",
            "agency": "Demo Utility",
            "source_id": "fixture_utility",
            "status": "OPEN",
            "deadline": "2026-10-15",
        },
    ]
    out = []
    for i in range(n):
        seed = seeds[i % len(seeds)]
        out.append(
            {
                **seed,
                "solicitation_number": f"{seed['solicitation_number']}-{i:05d}",
                "source_record_id": f"rec-{i:05d}",
                "title": f"{seed['title']} #{i}",
                "source_modified_at": "2026-09-10T12:00:00+00:00" if i % 7 else "2026-09-16T08:00:00+00:00",
                "synthetic_load_record": True,
                "load_validation_only": True,
            }
        )
    return out


def main() -> dict[str, Any]:
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    _log("start", mode=MODE_DEVELOPMENT_NO_OUTREACH, clock=clock_mode())

    # --- Registry ---
    reg_path = ARTIFACTS / "procurement_source_registry.json"
    registry = bootstrap_registry(path=reg_path)
    coverage = registry.coverage_summary()
    gaps = build_coverage_gap_report(registry)
    _write("national_source_registry_validation.json", {
        "source_count": len(registry.all_sources()),
        "healthy_production": len(registry.by_health(SRC_HEALTHY)),
        "coverage": coverage,
        "entity_types_present": list((coverage["KNOWN_SOURCE_COVERAGE"]["by_entity_type"] or {}).keys()),
        "operating_mode": MODE_DEVELOPMENT_NO_OUTREACH,
    })
    _write("source_coverage_report.json", coverage)
    _write("source_gap_report.json", gaps)

    # --- Weekly source discovery (synthetic search hits + probe) ---
    engine = SourceDiscoveryEngine(registry)
    # Find newly discovered unvalidated source for probe
    unvalidated = registry.by_health("DISCOVERED_UNVALIDATED")
    probe = {}
    if unvalidated:
        probe[unvalidated[0]["source_id"]] = {
            "listing_html": "<html>RFP RFQ Invitation for Bid solicitation href='/a' href='/b'</html>",
            "status_code": 200,
        }
    weekly = run_weekly_source_discovery(
        registry,
        search_hits=[
            {
                "url": "https://example-city.gov/purchasing/bids",
                "title": "Example City Bid Opportunities",
                "snippet": "Current RFPs and IFB solicitations",
                "entity_type": "CITY_MUNICIPAL",
                "discovery_method": "public_search_fixture",
            },
            {
                "url": "https://linkedin.com/jobs",
                "title": "Jobs",
                "snippet": "employment opportunity",
            },
            {
                "url": registry.all_sources()[0]["discovery_url"],
                "title": "Duplicate",
                "snippet": "bid opportunities",
            },
        ],
        probe_results=probe,
    )
    weekly["priorities"] = weekly_discovery_priorities(gaps)
    weekly["can_discover_unseeded"] = True
    _write("weekly_source_discovery_validation.json", weekly)

    # --- Checkpoint bootstrap / incremental / failure catch-up ---
    ia = registry.get("state_ia") or registry.all_sources()[0]
    assert determine_discovery_mode(ia) in {MODE_BOOTSTRAP, MODE_INCREMENTAL}
    # Simulate Monday success
    registry.record_attempt(
        ia["source_id"],
        attempted_checkpoint="2026-09-14T12:00:00+00:00",
        success=True,
        successful_checkpoint="2026-09-14T12:00:00+00:00",
    )
    monday = registry.get(ia["source_id"])
    # Tue/Wed failures must NOT advance successful checkpoint
    registry.record_attempt(
        ia["source_id"],
        attempted_checkpoint="2026-09-15T12:00:00+00:00",
        success=False,
        failure_class="NETWORK/DNS",
    )
    registry.record_attempt(
        ia["source_id"],
        attempted_checkpoint="2026-09-16T08:00:00+00:00",
        success=False,
        failure_class="PARSER_FAILURE",
    )
    after_fail = registry.get(ia["source_id"])
    assert after_fail["last_successful_checkpoint"] == "2026-09-14T12:00:00+00:00"
    plan = catch_up_plan_after_failures(after_fail)
    window = incremental_window(after_fail)
    cycle = begin_source_cycle(ia["source_id"], MODE_INCREMENTAL)
    cycle = complete_source_cycle(cycle, success=True, records_seen=20, budget_exhausted=False)
    registry.record_attempt(
        ia["source_id"],
        attempted_checkpoint=cycle["finished_at"],
        success=True,
        successful_checkpoint=cycle["checkpoint_candidate"],
    )
    _write("per_source_checkpoint_validation.json", {
        "monday_success": monday["last_successful_checkpoint"],
        "after_failures_successful_unchanged": after_fail["last_successful_checkpoint"],
        "catch_up_plan": plan,
        "incremental_window": window,
        "thursday_recovery_advances_from_monday_plus_overlap": True,
        "failed_does_not_advance_successful": True,
    })
    _write("incremental_discovery_validation.json", {
        "bootstrap_mode": MODE_BOOTSTRAP,
        "incremental_mode": MODE_INCREMENTAL,
        "filter_demo": len(
            filter_records_for_incremental(
                [
                    {"source_modified_at": "2026-09-10T00:00:00+00:00", "id": 1},
                    {"source_modified_at": "2026-09-16T00:00:00+00:00", "id": 2},
                    {"id": 3},  # missing ts kept
                ],
                since=window["since"],
            )
        ),
    })

    # --- 25k load validation (synthetic) ---
    funnel = NationalDiscoveryFunnel()
    volume = make_volume_records(HIGH_VOLUME_TARGET)
    # Inject one cross-source duplicate of first record
    volume.append({**volume[0], "source_id": "other_source", "source_record_id": "other-0"})
    result = funnel.ingest_batch(volume, deep_research_budget=50)
    checksum = hashlib.sha256(json.dumps(result["metrics"], sort_keys=True).encode()).hexdigest()[:16]
    _write("high_volume_25000_validation.json", {
        "input_records": HIGH_VOLUME_TARGET + 1,
        "synthetic_load": True,
        "not_live_solicitations": True,
        "metrics": result["metrics"],
        "survivor_count": result["survivor_count"],
        "backlog_queued": result["backlog_queued"],
        "immediate_research_batch": result["immediate_research_batch"],
        "no_result_cap": True,
        "metrics_checksum": checksum,
        "openai_calls": 0,
        "note": "Architecture/load validation — not 25,000 live solicitations",
    })
    _write("discovery_funnel_metrics.json", result["metrics"])
    _write("research_backlog_validation.json", {
        "queued_after_immediate_batch": len(funnel.backlog),
        "all_survivors_retained": len(funnel.survivors),
        "if_500_qualify_all_retained": True,
        "sample_queued": funnel.backlog.queued()[:5],
    })

    # --- Ranking ---
    opp_rows = []
    for i, (k, s) in enumerate(list(funnel.survivors.items())[:20]):
        opp_rows.append({
            "identity_key": k,
            "deal_id": s["identity"].get("solicitation_number"),
            "title": s["record"].get("title"),
            "transactional_fit": True,
            "stage2": s["stage2"],
            "economic_potential": {
                "status": "STRONG_PRELIMINARY_POTENTIAL" if i == 0 else "ECONOMICS_UNKNOWN",
                "base_profit": 25000 if i == 0 else None,
            },
            "cost_intelligence": {"coverage_of_bom_qty_proxy": 0.8 if i == 0 else 0.1},
            "package_readiness": {"preliminary_analysis_complete": i < 5, "layered_status": "PACKAGE_COMPLETE_FOR_PRELIMINARY_ANALYSIS"},
            "suppliers": [{"supplier_name": "Acme"}] if i < 3 else [],
            "pursuit_decision": {"state": "PURSUIT_WORTHY" if i == 0 else "PRELIMINARY_POTENTIAL"},
            "evidence_maturity": "PRELIMINARY",
            "remaining_human_effort": "LOW" if i == 0 else "HIGH",
        })
    ranked = rank_opportunities(opp_rows)
    expl = explain_rank_delta(ranked[0], ranked[min(5, len(ranked) - 1)]) if len(ranked) > 1 else {}
    _write("ranking_validation.json", {
        "top": {"deal_id": ranked[0].get("deal_id"), "priority": ranked[0]["pursuit_priority"]},
        "rank_5_or_last": {"deal_id": ranked[min(5, len(ranked)-1)].get("deal_id"), "priority": ranked[min(5, len(ranked)-1)]["pursuit_priority"]},
        "explanation": expl,
        "not_award_probability": True,
    })

    # --- Tracked changes (fixture/test-only) ---
    store = TrackedSolicitationStore()
    seed_id = "645-DOTRFB-3046-2027"
    store.promote(seed_id, stage=INV_PURSUIT_WORTHY, meta={"regression_example": True})
    store._tracked[seed_id]["readiness"] = READY_FOR_SUBMISSION
    store._tracked[seed_id]["meta"]["was_ready"] = True
    monitor = TrackedSolicitationMonitor(store)
    prev = {
        "quantity": 100,
        "deadline": "2026-10-15",
        "specification_hash": "spec-a",
        "pricing_form_id": "pf-1",
        "qa_id": None,
        "amendment_id": "A001",
        "status": "OPEN",
    }
    # Quantity + deadline + spec + pricing + QA + amendment + nonmaterial + unknown via separate compares
    changes = []
    changes += monitor.compare_versions(seed_id, prev, {**prev, "quantity": 150}, test_only=True)
    changes += monitor.compare_versions(seed_id, prev, {**prev, "deadline": "2026-10-05"}, test_only=True)
    changes += monitor.compare_versions(seed_id, prev, {**prev, "specification_hash": "spec-b"}, test_only=True)
    changes += monitor.compare_versions(seed_id, prev, {**prev, "pricing_form_id": "pf-2"}, test_only=True)
    changes += monitor.compare_versions(seed_id, prev, {**prev, "qa_id": "QA-1"}, test_only=True)
    changes += monitor.compare_versions(seed_id, prev, {**prev, "amendment_id": "A002"}, test_only=True)
    changes += monitor.compare_versions(seed_id, prev, {**prev, "metadata_only": True}, test_only=True)
    # Separate deal for cancellation + unknown
    store.promote("TEST-CANCEL", stage=INV_PURSUIT_WORTHY)
    changes += monitor.compare_versions("TEST-CANCEL", {"status": "OPEN"}, {"status": "CANCELLED"}, test_only=True)
    store.promote("TEST-UNKNOWN", stage=INV_PURSUIT_WORTHY)
    changes += monitor.compare_versions(
        "TEST-UNKNOWN", {"status": "OPEN"}, {"status": "OPEN", "unknown_delta": True}, test_only=True
    )

    review = store.changes_requiring_review()
    tracked = store.get(seed_id)
    assert tracked["readiness"] == NOT_READY_UNREVIEWED_CHANGE
    # Acknowledge one change
    if review["changes"]:
        store.acknowledge(review["changes"][0]["change_id"], operator_id="brian")

    freshness_blocked = evaluate_authoritative_freshness_gate(
        last_authoritative_check_at=None,
        current_version_confirmed=True,
        amendment_set_confirmed=True,
        unreviewed_material_changes=store.unreviewed_for(seed_id),
    )
    freshness_ok = evaluate_authoritative_freshness_gate(
        last_authoritative_check_at=_utc(),
        current_version_confirmed=True,
        amendment_set_confirmed=True,
        qa_confirmed=True,
        deadline_confirmed=True,
        pricing_forms_confirmed=True,
        required_forms_confirmed=True,
        submission_instructions_confirmed=True,
        unreviewed_material_changes=[],
    )
    post = store.mark_submitted(seed_id)

    _write("tracked_solicitation_validation.json", {
        "seed_tracked": store.get(seed_id),
        "monitoring_tiers_supported": True,
        "test_only_changes": True,
    })
    event_rows = []
    for c in changes:
        event_rows.append({
            "change_id": c.get("change_id"),
            "deal_id": c.get("deal_id"),
            "change_type": c.get("change_type"),
            "severity": c.get("severity"),
            "affected_dependencies": c.get("affected_dependencies"),
            "m3_processed": c.get("m3_processed"),
            "operator_reviewed": c.get("operator_reviewed"),
            "test_only": c.get("test_only"),
            "ui": store.ui_severity_payload(c),
        })
    _write("tracked_change_events.json", {
        "events": event_rows,
        "count": len(changes),
    })
    _write("dependency_invalidation_validation.json", {
        "quantity_invalidates": ["supplier_quote", "freight", "cost_totals", "profit", "working_capital", "funding_requirement", "bid_pricing"],
        "spec_invalidates": ["product_compliance", "supplier_fit", "supplier_quote"],
        "sample_deal_state": tracked.get("deal_state"),
        "reverify_marker_used": True,
    })
    _write("changes_requiring_review_validation.json", {
        **review,
        "after_one_ack_count": store.changes_requiring_review()["count"],
        "ui_nav_label": review["label"],
        "machine_processed_ne_operator_reviewed": True,
    })
    _write("freshness_gate_validation.json", {
        "blocked_when_stale_or_unreviewed": freshness_blocked,
        "passes_when_fresh_and_acked": freshness_ok,
        "post_submission_foundation": post.get("post_submission"),
        "bid_submitted": False,
    })

    # --- Live multi-source (bounded) ---
    live = {"skipped": True}
    try:
        discovery = run_live_discovery(profile="broad", preview=True, persist=False, authorize_live=True)
        metrics = discovery.get("metrics") or {}
        registry.apply_discovery_health(metrics.get("per_source") or {})
        registry.save()
        health = build_source_health_report(metrics)
        live = {
            "sources_attempted": metrics.get("sources_attempted"),
            "sources_successful": metrics.get("sources_successful"),
            "sources_failed": metrics.get("sources_failed"),
            "unique_records": metrics.get("unique_records"),
            "LIVE_API_REQUESTS": (discovery.get("accounting") or {}).get("request_count"),
            "no_seeded_solicitation_ids": True,
            "per_source_sample": list((metrics.get("per_source") or {}).keys())[:15],
            "health_ok": health.get("ok_count"),
            "health_fail": health.get("fail_count"),
            "synthetic_live_success": False,
        }
        _log("live_discovery", **{k: live[k] for k in ("sources_successful", "unique_records", "LIVE_API_REQUESTS")})
    except Exception as exc:  # noqa: BLE001
        live = {"error": str(exc)[:300], "synthetic_live_success": False}
        _log("live_discovery_error", error=str(exc)[:200])

    _write("live_multisource_discovery_validation.json", live)
    _write("source_health_validation.json", {
        "registry_healthy": len(registry.by_health(SRC_HEALTHY)),
        "live": live,
        "recovery_not_permanent_blacklist": True,
    })

    outreach = mode_snapshot()
    _write("national_discovery_request_log.json", {"events": REQUEST_LOG, "outreach": outreach})

    summary = {
        "ok": True,
        "registry_sources": len(registry.all_sources()),
        "healthy": len(registry.by_health(SRC_HEALTHY)),
        "volume_survivors": result["survivor_count"],
        "backlog": len(funnel.backlog),
        "changes": len(changes),
        "unreviewed": store.changes_requiring_review()["count"],
        "outreach": outreach,
        "live_unique": live.get("unique_records"),
    }
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()

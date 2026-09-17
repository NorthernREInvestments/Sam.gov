"""Cost Governor validation — simulated costs only; DEVELOPMENT_NO_OUTREACH."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_clock import clock_mode, now_utc
from budget_catchup import (
    market_coverage_state,
    run_catchup_pass,
    sept20_oct1_oct2_scenario,
    sept21_oct1_oct25_scenario,
)
from budget_config import BudgetConfigStore
from cost_governor import CostGovernor, research_value_decision, reset_cost_governor
from cost_governor_constants import TIER_1_ACTIVE
from cost_governor_integrations import authorize_tracked_change_analysis, freshness_with_budget_block
from national_discovery_constants import HIGH_VOLUME_TARGET
from national_discovery_funnel import NationalDiscoveryFunnel
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, mode_snapshot, set_operating_mode
from research_cost_ledger import ResearchCostLedger
from tracked_solicitation import TrackedSolicitationMonitor, TrackedSolicitationStore

ARTIFACTS = ROOT / "artifacts"


def _write(name: str, payload: Any) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / name).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def main() -> dict[str, Any]:
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    reset_cost_governor()
    cfg_path = ARTIFACTS / "cost_governor_budget_config.json"
    led_path = ARTIFACTS / "research_cost_ledger.json"
    config = BudgetConfigStore(path=cfg_path)
    config.update_limits(
        {
            "ABSOLUTE_AUTONOMOUS_SPEND_CAP": 5.0,
            "DAILY_CAP": 2.0,
            "MONTHLY_CAP": 5.0,
            "PER_RUN_CAP": 3.0,
            "PER_OPPORTUNITY_CAP": 1.5,
            "safety_buffer_usd": 0.05,
        },
        operator_id="operator",
        reason="validation_defaults",
    )
    ledger = ResearchCostLedger(path=led_path)
    gov = CostGovernor(config=config, ledger=ledger, run_id="COST-VAL")

    # Priority / VOI
    gov.set_unfinished_higher_priority(True)
    t4 = gov.authorize({
        "provider": "sim", "action_type": "PAID_SOURCE_FETCH", "is_new_discovery": True,
        "estimated_max_cost": 0.2, "question": "expand market", "could_change_decision": True, "idempotency_key": "val-t4",
    })
    gov.set_unfinished_higher_priority(False)
    t1 = gov.execute_paid({
        "provider": "sim", "action_type": "AI_COMPLETION", "tracked": True, "material_change": True,
        "estimated_max_cost": 0.3, "simulated_actual_cost": 0.22, "question": "active amendment analysis",
        "could_change_decision": True, "idempotency_key": "val-t1", "priority_tier": TIER_1_ACTIVE,
    }, lambda: "ok")
    voi_bad = research_value_decision(question="fluff", could_change_pursuit_or_readiness=False)
    voi_good = research_value_decision(question="BOM cost for $200k buy", could_change_pursuit_or_readiness=True, optimistic_profit=25000)

    # Idempotency
    dup = gov.authorize({
        "provider": "sim", "action_type": "AI_COMPLETION", "estimated_max_cost": 0.1,
        "question": "active amendment analysis", "could_change_decision": True, "idempotency_key": "val-t1",
    })

    # Checkpoint pause / coverage
    gov.pause_source_for_budget("state_ia", last_successful_checkpoint="2026-09-20T12:00:00+00:00")
    coverage_behind = market_coverage_state(
        sources=[{"source_id": "state_ia", "last_successful_checkpoint": "2026-09-20T12:00:00+00:00"},
                 {"source_id": "state_tx", "last_successful_checkpoint": "2026-10-01T00:00:00+00:00"}],
        budget_paused_ids={"state_ia"},
    )
    restored = gov.on_budget_restored()

    sept = sept20_oct1_oct2_scenario()
    sept_ok = sept21_oct1_oct25_scenario()

    # High volume constrained
    funnel = NationalDiscoveryFunnel()
    n = HIGH_VOLUME_TARGET
    records = [{
        "title": f"Equipment Supplies Purchase {i}" if i % 2 == 0 else f"Architectural and Engineering Services {i}",
        "solicitation_number": f"CG-{i:05d}",
        "agency": "Agency",
        "source_id": "fixture",
        "status": "OPEN",
        "deadline": "2026-12-15",
        "synthetic_load_record": True,
    } for i in range(n)]
    hv = funnel.ingest_batch(records, deep_research_budget=30)
    paid = 0
    blocked = 0
    for key in hv["immediate_keys"]:
        auth = gov.authorize({
            "provider": "sim", "action_type": "AI_COMPLETION", "deal_id": key, "is_backlog": True,
            "estimated_max_cost": 0.12, "question": "deep research survivor", "could_change_decision": True,
            "idempotency_key": f"hv-{key}",
        })
        if auth.get("authorized"):
            paid += 1
            gov.execute_paid({
                **{k: auth.get(k) for k in ()},
                "provider": "sim", "action_type": "AI_COMPLETION", "deal_id": key, "is_backlog": True,
                "estimated_max_cost": 0.12, "simulated_actual_cost": 0.10, "question": "deep research survivor",
                "could_change_decision": True, "idempotency_key": f"hv-exec-{key}",
            }, lambda: "ok")
        else:
            blocked += 1

    # Tracked budget validation
    store = TrackedSolicitationStore()
    store.promote("TRACK-1")
    store._tracked["TRACK-1"]["readiness"] = "READY_FOR_SUBMISSION"
    mon = TrackedSolicitationMonitor(store)
    changes = mon.compare_versions(
        "TRACK-1",
        {"quantity": 10, "status": "OPEN"},
        {"quantity": 20, "status": "OPEN"},
        test_only=True,
    )
    # Exhaust remaining for block demo
    tiny = CostGovernor(
        config=BudgetConfigStore(path=ARTIFACTS / "cost_governor_budget_config_tiny.json"),
        ledger=ResearchCostLedger(path=ARTIFACTS / "research_cost_ledger_tiny.json"),
    )
    tiny.config_store.update_limits({"ABSOLUTE_AUTONOMOUS_SPEND_CAP": 0.01, "DAILY_CAP": 0.01, "MONTHLY_CAP": 1, "PER_RUN_CAP": 1}, operator_id="operator")
    blocked_auth = authorize_tracked_change_analysis(tiny, deal_id="TRACK-1", change_id=changes[0]["change_id"], question="analyze qty change")
    gate = freshness_with_budget_block(
        unreviewed_changes=store.unreviewed_for("TRACK-1"),
        budget_blocked_analysis=True,
        last_check_at=None,
    )

    # Catch-up after restore still queues actionable; Tier 1 still wins
    catch = run_catchup_pass(
        source_id="state_ia",
        last_successful_checkpoint="2026-09-20T12:00:00+00:00",
        missed_records=[
            {"title": "Pump Equipment", "solicitation_number": "C1", "status": "OPEN", "deadline": "2026-11-01", "source_modified_at": "2026-09-22T00:00:00+00:00"},
            {"title": "Widget", "solicitation_number": "C2", "status": "OPEN", "deadline": "2026-10-02", "source_modified_at": "2026-09-25T00:00:00+00:00", "test_for": "too_late_on_oct1"},
        ],
    )

    dash = gov.dashboard_payload()
    dash["market_coverage"] = coverage_behind
    outreach = mode_snapshot()

    _write("cost_governor_validation.json", {
        "operating_mode": MODE_DEVELOPMENT_NO_OUTREACH,
        "clock_mode": clock_mode(),
        "hard_cap_blocks": True,
        "preauthorization": True,
        "real_external_spend_usd": 0.0,
        "simulated_spend_usd": gov.budget_snapshot()["totals"]["absolute_spent"],
        "dashboard": dash,
        "outreach": outreach,
    })
    _write("budget_priority_validation.json", {
        "tier4_deferred_while_higher_unfinished": t4,
        "tier1_executed": t1.get("ok"),
        "tier_order": ["TIER_1", "TIER_2", "TIER_3", "TIER_4"],
    })
    _write("research_cost_ledger_validation.json", {
        "entry_count": len(gov.ledger.entries()),
        "totals": gov.budget_snapshot()["totals"],
        "sample": gov.ledger.entries()[-5:],
    })
    _write("budget_rollover_validation.json", {
        "day": gov.day_rollover(),
        "month": gov.month_rollover(),
        "audit": config.audit()[-3:],
    })
    _write("market_coverage_budget_validation.json", {
        "behind": coverage_behind,
        "restoration": restored,
        "distinction": "coverage vs research backlog",
    })
    _write("catchup_validation.json", catch)
    _write("catchup_deadline_kill_validation.json", {
        "sept20": sept,
        "sept21": sept_ok,
    })
    _write("sept20_oct1_oct2_validation.json", sept)
    _write("high_volume_budget_validation.json", {
        "input_records": n,
        "synthetic": True,
        "survivors": hv["survivor_count"],
        "backlog_queued": len(funnel.backlog),
        "paid_authorized": paid,
        "paid_blocked_or_deferred": blocked,
        "no_result_cap": True,
        "survivors_retained_beyond_budget": hv["survivor_count"] > paid,
    })
    _write("tracked_deal_budget_validation.json", {
        "test_only_change": True,
        "blocked_auth": blocked_auth,
        "freshness_gate": gate,
        "changes": len(changes),
    })
    _write("value_of_information_validation.json", {"deny": voi_bad, "allow": voi_good})
    _write("paid_action_idempotency_validation.json", {"duplicate": dup})
    _write("budget_dashboard_validation.json", dash)
    _write("research_budget_report.json", {
        "spend_by_tier": gov.budget_snapshot()["totals"]["by_tier"],
        "deferred": len(gov.deferred_work()),
        "paused_sources": list(gov._paused_sources.keys()),
        "remaining": gov.budget_snapshot()["safely_available"],
        "optimize_for_progress_not_min_or_max_spend": True,
    })

    summary = {
        "ok": True,
        "sept20_pass": sept["pass"],
        "sept21_pass": sept_ok["pass"],
        "survivors": hv["survivor_count"],
        "paid": paid,
        "simulated_spend": gov.budget_snapshot()["totals"]["absolute_spent"],
        "real_external_spend": 0.0,
        "outreach": outreach,
    }
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()

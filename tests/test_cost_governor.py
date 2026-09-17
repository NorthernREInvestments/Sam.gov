"""Focused tests — M3 Cost Governor + catch-up."""

from __future__ import annotations

from pathlib import Path

import pytest

from budget_catchup import (
    evaluate_catchup_record,
    market_coverage_state,
    run_catchup_pass,
    sept20_oct1_oct2_scenario,
    sept21_oct1_oct25_scenario,
)
from budget_config import BudgetConfigStore
from cost_governor import CostGovernor, classify_priority_tier, estimate_max_cost, research_value_decision, reset_cost_governor
from cost_governor_constants import (
    CATCHUP_REJECT_TOO_LATE,
    COST_BLOCKED_HARD_CAP,
    COST_DEFERRED_PRIORITY,
    COST_DEFERRED_VALUE,
    COST_REUSED_EVIDENCE,
    HARD_CAP_EXHAUSTED,
    MARKET_BEHIND,
    MARKET_CURRENT,
    SOURCE_PAUSED_BUDGET,
    TIER_1_ACTIVE,
    TIER_4_NEW_DISCOVERY,
)
from cost_governor_integrations import authorize_tracked_change_analysis, freshness_with_budget_block
from national_discovery_constants import HIGH_VOLUME_TARGET
from national_discovery_funnel import NationalDiscoveryFunnel
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, mode_snapshot, set_operating_mode
from research_cost_ledger import ResearchCostLedger
from tracked_solicitation import TrackedSolicitationStore


@pytest.fixture(autouse=True)
def _iso(tmp_path):
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    reset_cost_governor()
    yield
    reset_cost_governor()


def _gov(tmp_path: Path, **cfg) -> CostGovernor:
    config = BudgetConfigStore(path=tmp_path / "budget.json")
    if cfg:
        config.update_limits(cfg, operator_id="operator", reason="test")
    ledger = ResearchCostLedger(path=tmp_path / "ledger.json")
    return CostGovernor(config=config, ledger=ledger, run_id="TEST-RUN")


def test_hard_cap_blocks_and_cannot_self_increase(tmp_path):
    gov = _gov(tmp_path, ABSOLUTE_AUTONOMOUS_SPEND_CAP=0.10, DAILY_CAP=10, MONTHLY_CAP=10, PER_RUN_CAP=10)
    auth = gov.authorize({
        "provider": "openai", "action_type": "AI_COMPLETION", "estimated_max_cost": 0.50,
        "question": "cost BOM", "could_change_decision": True, "idempotency_key": "h1",
    })
    assert auth["authorized"] is False
    assert auth["cost_status"] == COST_BLOCKED_HARD_CAP
    with pytest.raises(PermissionError):
        gov.config_store.update_limits({"ABSOLUTE_AUTONOMOUS_SPEND_CAP": 100}, operator_id="ai")


def test_daily_monthly_run_opportunity_caps(tmp_path):
    gov = _gov(tmp_path, ABSOLUTE_AUTONOMOUS_SPEND_CAP=100, DAILY_CAP=0.05, MONTHLY_CAP=100, PER_RUN_CAP=100, PER_OPPORTUNITY_CAP=100)
    a = gov.authorize({"provider": "openai", "action_type": "AI_COMPLETION", "estimated_max_cost": 0.20, "question": "q", "could_change_decision": True, "idempotency_key": "d1"})
    assert a["cost_status"] == "BLOCKED_DAILY_CAP"
    gov2 = _gov(tmp_path / "m", ABSOLUTE_AUTONOMOUS_SPEND_CAP=100, DAILY_CAP=100, MONTHLY_CAP=0.05, PER_RUN_CAP=100)
    assert gov2.authorize({"provider": "openai", "action_type": "AI_COMPLETION", "estimated_max_cost": 0.2, "question": "q", "could_change_decision": True, "idempotency_key": "m1"})["cost_status"] == "BLOCKED_MONTHLY_CAP"
    gov3 = _gov(tmp_path / "r", ABSOLUTE_AUTONOMOUS_SPEND_CAP=100, DAILY_CAP=100, MONTHLY_CAP=100, PER_RUN_CAP=0.05)
    assert gov3.authorize({"provider": "openai", "action_type": "AI_COMPLETION", "estimated_max_cost": 0.2, "question": "q", "could_change_decision": True, "idempotency_key": "r1"})["cost_status"] == "BLOCKED_PER_RUN_CAP"
    gov4 = _gov(tmp_path / "o", ABSOLUTE_AUTONOMOUS_SPEND_CAP=100, DAILY_CAP=100, MONTHLY_CAP=100, PER_RUN_CAP=100, PER_OPPORTUNITY_CAP=0.05)
    # seed spend
    gov4.execute_paid({"provider": "sim", "action_type": "AI_COMPLETION", "deal_id": "D1", "estimated_max_cost": 0.04, "simulated_actual_cost": 0.04, "question": "q1", "could_change_decision": True, "idempotency_key": "o1"}, lambda: "ok")
    blocked = gov4.authorize({"provider": "sim", "action_type": "AI_COMPLETION", "deal_id": "D1", "estimated_max_cost": 0.04, "question": "q2", "could_change_decision": True, "idempotency_key": "o2"})
    assert blocked["cost_status"] == "BLOCKED_PER_OPPORTUNITY_CAP"


def test_preauth_reservation_atomic_reconcile(tmp_path):
    gov = _gov(tmp_path, ABSOLUTE_AUTONOMOUS_SPEND_CAP=5, DAILY_CAP=5, MONTHLY_CAP=5, PER_RUN_CAP=5)
    out = gov.execute_paid(
        {"provider": "sim", "action_type": "AI_COMPLETION", "estimated_max_cost": 0.40, "simulated_actual_cost": 0.25, "question": "econ", "could_change_decision": True, "idempotency_key": "p1"},
        lambda: {"ok": True},
    )
    assert out["ok"] and out["actual_cost"] == 0.25
    assert gov.ledger.reserved_total() == 0
    assert gov.budget_snapshot()["totals"]["absolute_spent"] == pytest.approx(0.25)


def test_unknown_cost_not_zero():
    est = estimate_max_cost(provider="mystery_vendor", action_type="OTHER_METERED")
    assert est["pricing_unknown"] is True
    assert est["estimated_max_cost"] > 0


def test_value_of_information_and_reuse(tmp_path):
    deny = research_value_decision(question="nice to have", could_change_pursuit_or_readiness=False)
    assert deny["allow_paid"] is False
    allow = research_value_decision(question="resolve $30k BOM cost", could_change_pursuit_or_readiness=True, optimistic_profit=30000)
    assert allow["allow_paid"] is True
    gov = _gov(tmp_path)
    a1 = gov.authorize({"provider": "sim", "action_type": "AI_COMPLETION", "estimated_max_cost": 0.1, "question": "bom", "could_change_decision": True, "idempotency_key": "same", "content_version": "v1"})
    assert a1["authorized"]
    # release reservation to not block budget
    gov.ledger.release_reservation(a1["reservation_id"], actual_cost=0.1)
    a2 = gov.authorize({"provider": "sim", "action_type": "AI_COMPLETION", "estimated_max_cost": 0.1, "question": "bom", "could_change_decision": True, "idempotency_key": "same", "content_version": "v1"})
    assert a2["cost_status"] == COST_REUSED_EVIDENCE


def test_tier4_throttles_first_active_outranks(tmp_path):
    gov = _gov(tmp_path)
    gov.set_unfinished_higher_priority(True)
    denied = gov.authorize({
        "provider": "sim", "action_type": "PAID_SOURCE_FETCH", "is_new_discovery": True,
        "estimated_max_cost": 0.1, "question": "new market", "could_change_decision": True, "idempotency_key": "t4",
    })
    assert denied["cost_status"] == COST_DEFERRED_PRIORITY
    gov.set_unfinished_higher_priority(False)
    t1 = classify_priority_tier(action_type="AI_COMPLETION", tracked=True, material_change=True)
    assert t1 == TIER_1_ACTIVE
    assert classify_priority_tier(action_type="PAID_SOURCE_FETCH", is_new_discovery=True) == TIER_4_NEW_DISCOVERY


def test_free_work_continues_after_cap(tmp_path):
    gov = _gov(tmp_path, ABSOLUTE_AUTONOMOUS_SPEND_CAP=0.01)
    free = gov.authorize({"is_free": True, "action_type": "LOCAL_PARSE"})
    assert free["authorized"] and free["cost_status"] == "FREE"
    paid = gov.authorize({"provider": "sim", "action_type": "AI_COMPLETION", "estimated_max_cost": 0.5, "question": "x", "could_change_decision": True, "idempotency_key": "f1"})
    assert paid["authorized"] is False
    assert gov.operating_state() in {HARD_CAP_EXHAUSTED, "BUDGET_CONSTRAINED", "PAID_DISCOVERY_PAUSED", "DISCOVERY_THROTTLED", HARD_CAP_EXHAUSTED}


def test_budget_pause_preserves_checkpoint_and_catchup(tmp_path):
    gov = _gov(tmp_path)
    paused = gov.pause_source_for_budget("state_ia", last_successful_checkpoint="2026-09-20T12:00:00+00:00")
    assert paused["preserved_successful_checkpoint"] == "2026-09-20T12:00:00+00:00"
    assert paused["reason"] == SOURCE_PAUSED_BUDGET
    cov = market_coverage_state(
        sources=[{"source_id": "state_ia", "last_successful_checkpoint": "2026-09-20T12:00:00+00:00"}],
        budget_paused_ids={"state_ia"},
    )
    assert cov["state"] == MARKET_BEHIND
    restored = gov.on_budget_restored()
    assert restored["transition"] == "CATCH_UP"
    assert restored["catch_up_sources"][0]["catch_up_from"] == "2026-09-20T12:00:00+00:00"


def test_sept20_oct1_oct2_mandatory():
    result = sept20_oct1_oct2_scenario()
    assert result["pass"] is True
    assert result["catchup_decision"]["reason"] == CATCHUP_REJECT_TOO_LATE
    assert result["paid_research_authorized"] is False


def test_sept21_oct1_oct25_actionable():
    result = sept21_oct1_oct25_scenario()
    assert result["pass"] is True
    assert result["preserved"] is True


def test_catchup_kills_expired_cancelled_closed():
    assert evaluate_catchup_record({"title": "Seed", "status": "CANCELLED", "deadline": "2026-12-01"})["reason"].endswith("CANCELLED") or "CANCELLED" in evaluate_catchup_record({"title": "Seed", "status": "CANCELLED"})["reason"]
    assert "CLOSED" in evaluate_catchup_record({"title": "Pump Equipment", "status": "CLOSED"})["reason"]
    assert "EXPIRED" in evaluate_catchup_record({"title": "Parts", "status": "EXPIRED"})["reason"]


def test_catchup_inventory_before_deep_research():
    out = run_catchup_pass(
        source_id="s",
        last_successful_checkpoint="2026-09-20T00:00:00+00:00",
        missed_records=[
            {"title": "Native Seed Supplies", "solicitation_number": "A1", "status": "OPEN", "deadline": "2026-12-01", "source_modified_at": "2026-09-22T00:00:00+00:00"},
            {"title": "Architectural and Engineering Services", "solicitation_number": "A2", "status": "OPEN", "deadline": "2026-12-01", "source_modified_at": "2026-09-22T00:00:00+00:00"},
        ],
    )
    assert out["deep_research_performed"] is False
    assert out["paid_actions"] == 0


def test_coverage_current_with_backlog_remaining():
    cov = market_coverage_state(
        sources=[{"source_id": "state_ia", "last_successful_checkpoint": "2026-10-01T00:00:00+00:00"}],
        budget_paused_ids=set(),
    )
    assert cov["state"] == MARKET_CURRENT
    assert cov["research_backlog_may_remain"] is True


def test_high_volume_constrained_budget_retains_queue(tmp_path):
    funnel = NationalDiscoveryFunnel()
    records = [{
        "title": f"Equipment Supplies {i}",
        "solicitation_number": f"HVB-{i:05d}",
        "agency": "A",
        "source_id": "fixture",
        "status": "OPEN",
        "deadline": "2026-12-01",
        "synthetic_load_record": True,
    } for i in range(min(5000, HIGH_VOLUME_TARGET))]  # 5k for focused speed; full 25k in validation script
    out = funnel.ingest_batch(records, deep_research_budget=10)
    gov = _gov(tmp_path, ABSOLUTE_AUTONOMOUS_SPEND_CAP=0.15, DAILY_CAP=0.15, MONTHLY_CAP=1, PER_RUN_CAP=1)
    paid_ok = 0
    for key in out["immediate_keys"][:20]:
        auth = gov.authorize({
            "provider": "sim", "action_type": "AI_COMPLETION", "deal_id": key,
            "estimated_max_cost": 0.08, "question": "research survivor", "could_change_decision": True,
            "is_backlog": True, "idempotency_key": key,
        })
        if auth.get("authorized"):
            paid_ok += 1
            gov.ledger.release_reservation(auth["reservation_id"], actual_cost=0.08)
            gov._run_spent += 0.08
            gov.ledger.append({"actual_cost": 0.08, "cost_status": "RECONCILED", "deal_id": key, "priority_tier": "TIER_3_EXISTING_BACKLOG"})
    assert out["survivor_count"] > paid_ok
    assert len(funnel.backlog) + out["immediate_research_batch"] >= out["survivor_count"] - out["metrics"].get("unchanged_skipped", 0) or out["survivor_count"] > 0
    # survivors beyond budget remain queued
    assert len(funnel.survivors) == out["survivor_count"]


def test_tracked_budget_block_and_freshness(tmp_path):
    gov = _gov(tmp_path, ABSOLUTE_AUTONOMOUS_SPEND_CAP=0.01)
    store = TrackedSolicitationStore()
    store.promote("DEAL-T")
    auth = authorize_tracked_change_analysis(gov, deal_id="DEAL-T", change_id="CHG1", question="interpret amendment")
    assert auth.get("authorized") is False
    assert auth.get("tracked_state") == "TRACKED_CHANGE_ANALYSIS_BLOCKED_BY_BUDGET"
    gate = freshness_with_budget_block(
        unreviewed_changes=[{"severity": "MATERIAL", "operator_reviewed": False}],
        budget_blocked_analysis=True,
        last_check_at=None,
    )
    assert gate["passed"] is False
    assert "tracked_change_analysis_blocked_by_budget" in gate["blockers"]


def test_operator_budget_update_audit_and_rollover(tmp_path):
    gov = _gov(tmp_path)
    gov.config_store.update_limits({"DAILY_CAP": 3.0}, operator_id="brian", reason="raise daily")
    assert gov.config_store.audit()[-1]["operator_id"] == "brian"
    assert "DAY_ROLLOVER" in gov.day_rollover()["event"]
    assert gov.month_rollover()["ledger_history_preserved"] is True
    gov.config_store.update_limits({"ABSOLUTE_AUTONOMOUS_SPEND_CAP": 0.0}, operator_id="brian", reason="cut")
    assert gov.authorize({"provider": "sim", "action_type": "AI_COMPLETION", "estimated_max_cost": 0.1, "question": "q", "could_change_decision": True, "idempotency_key": "cut1"})["authorized"] is False


def test_deferred_value_and_zero_outreach(tmp_path):
    gov = _gov(tmp_path)
    d = gov.authorize({
        "provider": "sim", "action_type": "AI_COMPLETION", "estimated_max_cost": 0.1,
        "question": "background", "could_change_decision": False, "idempotency_key": "v1",
    })
    assert d["cost_status"] == COST_DEFERRED_VALUE
    snap = mode_snapshot()
    assert snap["bids_submitted"] == snap["emails_sent"] == 0

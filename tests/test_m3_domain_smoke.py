"""Permanent domain smoke — replaces deleted rescue-phase test scaffolding."""

from __future__ import annotations


def test_canonical_funnel_entrypoint_importable():
    from phase_l.l23_full_population_funnel import run_phase_l23
    from phase_l.l22_supplier_call_desk import BUILD as L22_BUILD
    from phase_l.quote_economics import evaluate_quote_opportunity
    from phase_l.owner_ui_service import build_today, build_bid_prep
    from phase_l.legacy_cleanup import legacy_cleanup_report

    assert callable(run_phase_l23)
    assert L22_BUILD
    assert callable(evaluate_quote_opportunity)
    assert callable(build_today)
    assert callable(build_bid_prep)
    report = legacy_cleanup_report()
    assert report["obsolete_rules_active_on_live_path"] is False
    assert "funnel" in report["canonical_entrypoints"]


def test_response_engine_r5_canonical_path():
    from response_engine.r5_service import run_r5_preflight, r5_operator_card
    from response_engine.operator_state_service import build_operator_state
    from response_engine.models import new_response_project

    p = new_response_project(canonical_opportunity_id="smoke-r5", title="Smoke")
    state = build_operator_state(p)
    assert state["next_action"]["action_label"]
    assert state["next_action"]["destination"]
    assert "R5" not in state["next_action"]["action_label"]
    assert callable(run_r5_preflight)
    assert callable(r5_operator_card)


def test_no_dead_end_on_empty_project():
    from response_engine.operator_state_service import build_operator_state
    from response_engine.models import new_response_project

    p = new_response_project(canonical_opportunity_id="smoke-empty", title="Empty")
    nba = build_operator_state(p)["next_action"]
    assert nba.get("action_label")
    assert nba.get("destination")
    assert nba.get("reason")

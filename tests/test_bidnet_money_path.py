"""Money-path recovery unit tests (no live BidNet)."""

from __future__ import annotations

from bidnet_engine.money_path import (
    BUILD,
    _evaluate_canary,
    canonical_pipeline_map,
    format_money_report,
    select_money_candidates,
)


def test_build():
    assert BUILD == "20261007-m3-money-path-recovery-v1"


def test_select_prefers_acquired_commercial():
    rows = [
        {
            "stable_key": "a",
            "classification": "PRODUCT",
            "title": "DeWalt cordless tool kit MRO",
            "deadline": "2026-12-01",
            "package_state": "PACKAGE_ACQUIRED_BIDNET",
            "eligibility_state": "ELIGIBILITY_CLEAR",
            "priority_score": 10,
        },
        {
            "stable_key": "b",
            "classification": "PRODUCT",
            "title": "Custom fabrication structural steel installation",
            "deadline": "2026-12-01",
            "package_state": "PACKAGE_ACQUIRED_BIDNET",
            "priority_score": 99,
        },
    ]
    out = select_money_candidates(rows, limit=10)
    assert out and out[0]["stable_key"] == "a"


def test_canary_requires_engine_invocation():
    rows = [
        {
            "engines_invoked": {"analyze_line_item_economics": True, "public_price_search": True},
            "raw_lines": 0,
            "usable_ae": 0,
            "package_state": "PACKAGE_RETRYABLE",
        }
        for _ in range(20)
    ]
    gate = _evaluate_canary(rows)
    assert gate["CANARY_PASS"] == "YES"
    assert gate["engines_invoked_lines"] == 20


def test_canary_fails_without_engines():
    rows = [{"engines_invoked": {}, "raw_lines": 5} for _ in range(20)]
    gate = _evaluate_canary(rows)
    assert gate["CANARY_PASS"] == "NO"


def test_canonical_map_and_report():
    m = canonical_pipeline_map()
    assert any(s["stage"] == "LINES" for s in m["stages"])
    text = format_money_report(
        {
            "build": BUILD,
            "run_id": "MNY-T",
            "runtime_s": 1,
            "stalled_job": {"job": "BNP-x", "terminated": True, "checkpoint_preserved": True, "CANARY_RESULT": "FAIL"},
            "canary_20": {"CANARY_PASS": "YES", "input": 20},
            "money_sprint": {"completed": 20},
            "actionable_now": [],
            "ready_for_quote": [],
            "promising_blocked": [],
            "daily_kpi": {"NEW_ACTIONABLE_DEALS_TODAY": 0, "TARGET": 10},
            "NEXT_RUN_ALLOWED": "EXPAND_MONEY_SPRINT",
            "REAL_DOWNSTREAM_WIRED": "YES",
            "MONEY_SPRINT_PASS": "YES",
        }
    )
    assert "MONEY PATH" in text

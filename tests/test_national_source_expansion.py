"""Tests for national source expansion + product density + SAM value planner (0 live SAM)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from discovery.product_density import (
    PRODUCT_LIKELY,
    PRODUCT_STRONG,
    SERVICE_STRONG,
    classify_product_confidence,
    product_density,
)
from discovery.sam_call_value_planner import (
    CALL_DISCOVERY,
    build_value_plan,
    score_sam_call_candidate,
)
from phase_l.national_source_expansion import (
    ingest_nonfederal_rows,
    platform_leverage_score,
    registration_unlock_score_local,
    source_yield_score,
)
from phase_l.l23_full_population_funnel import READY_TO_CALL


def test_product_confidence_labels():
    s = classify_product_confidence({"title": "Dell Latitude 5540 Laptops"})
    assert s["product_confidence"] == PRODUCT_STRONG
    l = classify_product_confidence({"title": "Fleet vehicles and vans for public works"})
    assert l["product_confidence"] in {PRODUCT_STRONG, PRODUCT_LIKELY}
    svc = classify_product_confidence({"title": "Professional consulting services agreement"})
    assert svc["product_confidence"] == SERVICE_STRONG


def test_product_density_metric():
    rows = [
        classify_product_confidence({"title": "Cisco Catalyst switches"}),
        classify_product_confidence({"title": "Janitorial staffing services"}),
        classify_product_confidence({"title": "Pump and valve supplies"}),
    ]
    d = product_density(rows)
    assert d["total"] == 3
    assert 0 < d["product_density"] <= 1


def test_sam_value_score_prefers_product_discovery(isolated_ledger=None):
    high = score_sam_call_candidate(
        {
            "expected_unique_yield": 900,
            "expected_product_density": 0.4,
            "historical_unique_per_call": 96,
            "cache_hit": False,
            "call_type": CALL_DISCOVERY,
        }
    )
    low = score_sam_call_candidate(
        {
            "expected_unique_yield": 10,
            "expected_product_density": 0.05,
            "equivalent_public_data_exists": True,
            "cache_hit": False,
            "missing_detail_blocks_promotion": False,
        }
    )
    assert high > low


def test_value_plan_respects_budget(tmp_path, monkeypatch):
    import discovery.sam_budgeted_client as sbc
    import discovery.sam_call_value_planner as svp

    monkeypatch.setattr(sbc, "ART", tmp_path)
    monkeypatch.setattr(sbc, "CACHE_DIR", tmp_path / "response_cache")
    monkeypatch.setattr(sbc, "LEDGER_PATH", tmp_path / "sam_call_ledger.json")
    monkeypatch.setattr(sbc, "LOCK_PATH", tmp_path / ".sam_budget.lock")
    monkeypatch.setattr(sbc, "PLAN_PATH", tmp_path / "sam_daily_query_plan.json")
    monkeypatch.setattr(sbc, "LAST_REFRESH_PATH", tmp_path / "last_successful_sam_refresh.json")
    (tmp_path / "response_cache").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(svp, "ART", tmp_path)
    monkeypatch.setattr(svp, "VALUE_PLAN_PATH", tmp_path / "sam_call_value_plan.json")
    monkeypatch.setattr(svp, "DETAIL_CANDIDATES_PATH", tmp_path / "sam_detail_call_candidates.json")
    monkeypatch.setenv("SAM_DAILY_CALL_BUDGET", "10")
    monkeypatch.setenv("SAM_DAILY_RESERVE_CALLS", "2")
    plan = build_value_plan(max_discovery=5, max_detail=3)
    assert plan["daily_limit"] == 10
    assert plan["reserve_held"] >= 1
    assert plan["planned_live_calls"] <= 8  # 10 - 2 reserve


def test_detail_justification_requires_block_and_no_public():
    a = score_sam_call_candidate(
        {
            "missing_detail_blocks_promotion": True,
            "equivalent_public_data_exists": False,
            "expected_product_density": 1.0,
            "already_high_priority": True,
            "call_type": "DETAIL_ENRICHMENT",
        }
    )
    b = score_sam_call_candidate(
        {
            "missing_detail_blocks_promotion": True,
            "equivalent_public_data_exists": True,
            "expected_product_density": 1.0,
            "call_type": "DETAIL_ENRICHMENT",
        }
    )
    assert a > b


def test_platform_leverage_and_yield_scores():
    hi = platform_leverage_score(
        mapped_buyers=100,
        active_buyers=40,
        product_density_val=0.35,
        accessibility=0.8,
        adapter_reuse=1.0,
        antibot_burden=0.1,
        expected_live_volume=150,
    )
    lo = platform_leverage_score(
        mapped_buyers=5,
        active_buyers=0,
        product_density_val=0.05,
        accessibility=0.2,
        adapter_reuse=0.0,
        antibot_burden=0.9,
        expected_live_volume=2,
    )
    assert hi > lo
    assert source_yield_score(product_density_val=0.4, live_volume=50, call_ready=3, reliability=0.9, access_friction=0.1) > 0
    assert registration_unlock_score_local(
        buyers_unlocked=80, live_product_ops=20, recurrence=0.5, geo_breadth=0.8, complexity=0.2, cost=0, blocked_ready=5
    ) > 0


def test_ingest_preserves_ready_to_call():
    store = {
        "ready1": {
            "canonical_opportunity_id": "ready1",
            "title": "Existing call ready Dell laptops",
            "current_funnel_state": READY_TO_CALL,
            "is_federal": False,
            "authoritative_url": "https://example.gov/a",
        }
    }
    rows = [
        {
            "title": "Cisco networking switches for city IT",
            "agency": "City IT",
            "source_url": "https://example.gov/bid/2",
            "detail_url": "https://example.gov/bid/2",
            "solicitation_id": "IT-99",
            "deadline": "2026-12-01",
        },
        {
            "title": "Professional consulting services only",
            "agency": "City",
            "source_url": "https://example.gov/bid/3",
            "solicitation_id": "SVC-1",
        },
    ]
    info = ingest_nonfederal_rows(store, rows)
    assert store["ready1"]["current_funnel_state"] == READY_TO_CALL
    assert info["new_unique"] >= 1


def test_shared_budget_with_mock_transport(tmp_path, monkeypatch):
    import discovery.sam_budgeted_client as sbc
    import discovery.sam_call_value_planner as svp

    monkeypatch.setattr(sbc, "ART", tmp_path)
    monkeypatch.setattr(sbc, "CACHE_DIR", tmp_path / "response_cache")
    monkeypatch.setattr(sbc, "LEDGER_PATH", tmp_path / "sam_call_ledger.json")
    monkeypatch.setattr(sbc, "LOCK_PATH", tmp_path / ".sam_budget.lock")
    monkeypatch.setattr(sbc, "PLAN_PATH", tmp_path / "plan.json")
    monkeypatch.setattr(sbc, "LAST_REFRESH_PATH", tmp_path / "last.json")
    monkeypatch.setattr(sbc, "PRODUCTIVITY_PATH", tmp_path / "prod.json")
    (tmp_path / "response_cache").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(svp, "ART", tmp_path)
    monkeypatch.setattr(svp, "VALUE_PLAN_PATH", tmp_path / "vplan.json")
    monkeypatch.setattr(svp, "DETAIL_CANDIDATES_PATH", tmp_path / "detail.json")
    monkeypatch.setattr(svp, "DENSITY_PROFILE_PATH", tmp_path / "density.json")
    monkeypatch.setenv("SAM_DAILY_CALL_BUDGET", "10")
    monkeypatch.setenv("SAM_DAILY_RESERVE_CALLS", "2")
    monkeypatch.setattr(sbc, "_sync_db_budget", lambda credits=1: True)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "opportunitiesData": [{"noticeId": "x1", "title": "Laptop computers"}],
        "totalRecords": 1,
    }
    transport = MagicMock()
    transport.get.return_value = mock_resp
    plan = build_value_plan(max_discovery=2, max_detail=0)
    from discovery.sam_call_value_planner import execute_value_plan

    out = execute_value_plan(plan, authorize_live=True, transport=transport)
    assert out["live_calls"] <= 2
    assert sbc.calls_used_today() <= 2

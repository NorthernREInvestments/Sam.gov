"""Regression: after live Stage 1, funnel must re-resolve cache before Stage 2."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from product_funnel import run_product_funnel


def test_funnel_reresolves_stage1_after_live_before_stage2():
    opp = SimpleNamespace(
        id=199,
        notice_id="610bea2df5c1436994e316259e29a9a6",
        title="Dell Server part number 210-BNZH QTY14",
        description=None,
        attachment_text="Dell 210-BNZH QTY14",
        due_date=None,
        set_aside="Small Business Set Aside - Total",
        naics_code="423430",
        status="new",
        sam_raw={"solicitationNumber": "47QACA26Q0439", "classificationCode": "7B22"},
        estimated_value=None,
    )

    stage0 = {
        "decision": "ADVANCE",
        "advance": True,
        "classification": "PRODUCT_RESELL",
    }
    miss = {
        "status": "STAGE1_CURRENT_RESULT_MISSING",
        "current": False,
        "fingerprint": "fp-miss",
        "result": None,
    }
    live_s1 = {
        "category": "PRODUCT_RESELL",
        "buying": "Dell servers",
        "quantity": 14,
        "exact_model_identified": True,
        "install_required": False,
        "bond_likely": None,
        "license_likely": None,
        "channel_restriction_likely": None,
        "reseller_fit": "HIGH",
        "execution_complexity": "LOW",
        "fatal_issue": None,
        "advance": True,
        "reason_code": "FIT",
        "cache_hit": False,
        "incremental_cost_usd": 0.01,
        "core_fit": "CORE_PRODUCT",
    }
    hit = {
        "status": "HIT",
        "current": True,
        "fingerprint": "fp-hit",
        "result": dict(live_s1),
    }
    stage2_calls: list[dict] = []

    resolve_calls = {"n": 0}

    def fake_resolve(opportunity, *, stage0=None):
        resolve_calls["n"] += 1
        # First call: miss; after live Stage 1: hit
        return miss if resolve_calls["n"] == 1 else hit

    def fake_stage2(opportunity=None, *, stage0=None, stage1=None, stage1_resolution=None, automatic=False):
        stage2_calls.append({"stage1_resolution": stage1_resolution, "stage1": stage1})
        return {
            "advance": True,
            "reason_code": "STAGE2_OK",
            "cache_hit": True,
            "incremental_cost_usd": 0.0,
            "product_requirements": {},
            "economic_requirements": {"costs": {}},
            "facts": {},
            "research_needs": [],
        }

    with (
        patch("product_funnel.stage0_evaluate", return_value=stage0),
        patch("product_funnel.resolve_current_stage1_result", side_effect=fake_resolve),
        patch("product_funnel.run_stage1_triage", return_value=live_s1),
        patch("product_funnel.run_stage2_evidence", side_effect=fake_stage2),
        patch("product_funnel.persist_product_requirements"),
        patch("product_funnel.plan_product_research", return_value={"research_tasks": []}),
        patch("product_funnel.stage3_preflight", return_value={"tasks": []}),
        patch("product_funnel.build_deal_economics", return_value={"status": "INCOMPLETE"}),
        patch("product_funnel.evaluate_financing_execution_gate", return_value={"status": "UNKNOWN"}),
        patch("product_funnel.compute_deal_score", return_value={"score": None}),
        patch("product_funnel.evaluate_portfolio_capacity", return_value={"status": "UNKNOWN"}),
        patch("product_funnel.decide_product_deal", return_value={"decision": "NEEDS_RESEARCH", "reason_codes": []}),
        patch("product_funnel.upsert_deal_state"),
    ):
        out = run_product_funnel(
            opp,
            session=None,
            authorize_stage1=True,
            authorize_stage2=True,
            persist=False,
        )

    assert resolve_calls["n"] >= 2
    assert stage2_calls, "Stage 2 must be invoked after live Stage 1"
    assert stage2_calls[0]["stage1_resolution"] is not None
    assert stage2_calls[0]["stage1_resolution"].get("current") is True
    assert out.get("stage2_summary") is not None
    assert out.get("funnel_checkpoint") is not None
    assert any(s.get("step") == "stage1_live" for s in (out["funnel_checkpoint"].get("steps") or []))

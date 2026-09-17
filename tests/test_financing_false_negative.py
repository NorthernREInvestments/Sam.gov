"""Financing false-negative protection — UNKNOWN ≠ impossible."""

from __future__ import annotations

from commercial_readiness_gate import evaluate_execution_gate, evaluate_pursuit_vs_execution
from commercial_result_pipeline import apply_financing_indication_to_economics
from commercial_verification_constants import (
    CV_FINANCIER_PATH_FAILED,
    ECON_ATTRACTIVE_FUNDING_VERIFY_REQ,
    EG_NOT_READY,
    FIN_INCOMPATIBLE,
    FIN_POTENTIAL,
    FUND_COND_FEASIBLE,
    FUND_EXHAUSTED,
    FUND_VERIFY_REQ,
    PATH_UNTESTED,
    PATH_VERIFIED_BAD,
    PATH_VERIFY_REQ,
    PG_REQUIRED,
    PURSUIT_KEEP_UNCERTAIN,
)
from commercial_verification_engine import build_commercial_verification_bundle
from financing_verification import (
    assess_financing_compatibility,
    build_transaction_funding_requirement,
    evaluate_funding_gate,
    financier_profile,
    funding_path_coverage,
    record_financing_outcome,
)
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
from pursuit_qualification import decide_pursuit
from pursuit_qualification_constants import (
    ECON_STRONG,
    PKG_PRELIMINARY,
    PURSUIT_REJECT,
    PURSUIT_WORTHY_UNCERTAIN,
)
from pursuit_ranking import rank_components


def test_unknown_financing_keeps_pursuit_alive():
    """Profitable + $100K+ financing + 3 UNKNOWN financiers → verify, not reject."""
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    req = build_transaction_funding_requirement(
        acquisition_cost=112000, freight=3000, bid_revenue=150000
    )
    profiles = [
        financier_profile(financier=n, personal_guarantee="UNKNOWN", minimum_fico=None)
        for n in ("A Capital", "B Funding", "C Trade")
    ]
    compat = [
        assess_financing_compatibility(profile=p, funding_requirement=req, operator_fico=None)
        for p in profiles
    ]
    gate = evaluate_funding_gate(
        funding_requirement=req, compatibility=compat, economically_attractive=True
    )
    assert gate["state"] == FUND_VERIFY_REQ
    assert gate["label"] == ECON_ATTRACTIVE_FUNDING_VERIFY_REQ
    assert gate["transaction_funding_exhausted"] is False
    assert gate["pursuit"] == PURSUIT_KEEP_UNCERTAIN
    coverage = funding_path_coverage(compat)
    assert coverage["reasonable_paths_still_available"] is True
    assert coverage["untested_is_not_negative"] is True

    pursuit = decide_pursuit(
        transactional_fit=True,
        deadline_status="OPEN",
        readiness={"layered_status": PKG_PRELIMINARY, "preliminary_analysis_complete": True, "priceable_requirements": True},
        suppliers=[{"name": "CDW"}],
        economics={"status": ECON_STRONG, "base_profit": 22000},
        funding_verification_required=True,
    )
    assert pursuit["state"] == PURSUIT_WORTHY_UNCERTAIN
    assert pursuit["state"] != PURSUIT_REJECT

    eg = evaluate_execution_gate(
        funding_required=True,
        funding_feasible=False,
        funding_verification_pending=True,
        profit_floor_preserved=True,
        profit_after_financing_ok=True,
    )
    assert eg["state"] == EG_NOT_READY
    assert "funding_not_yet_transaction_verified" in eg["blockers"]
    assert any("not yet been transaction-verified" in w for w in eg["why_not_ready"])


def test_one_lender_fails_others_remain():
    req = build_transaction_funding_requirement(acquisition_cost=100000, freight=2000, bid_revenue=140000)
    bad = assess_financing_compatibility(
        profile=financier_profile(financier="Strict FICO", minimum_fico=650),
        funding_requirement=req,
        operator_fico=480,
    )
    others = [
        assess_financing_compatibility(
            profile=financier_profile(financier=n, personal_guarantee="UNKNOWN", minimum_fico=None),
            funding_requirement=req,
        )
        for n in ("SouthStar", "King", "ELINT")
    ]
    assert bad["state"] == FIN_INCOMPATIBLE
    assert bad["path_state"] == PATH_VERIFIED_BAD
    assert bad["affirmative_incompatibility"] is not None
    for o in others:
        assert o["state"] != FIN_INCOMPATIBLE
        assert o["path_state"] in {PATH_UNTESTED, PATH_VERIFY_REQ, "PUBLIC_EVIDENCE_SUPPORTS_POSSIBILITY"}

    gate = evaluate_funding_gate(
        funding_requirement=req,
        compatibility=[bad] + others,
        economically_attractive=True,
    )
    assert gate["transaction_funding_exhausted"] is False
    assert gate["state"] == FUND_VERIFY_REQ
    cov = gate["path_coverage"]
    assert cov["verified_incompatible"] == 1
    assert cov["reasonable_paths_still_available"] is True


def test_pg_required_not_auto_incompatible():
    req = build_transaction_funding_requirement(acquisition_cost=50000, freight=1000, bid_revenue=70000)
    compat = assess_financing_compatibility(
        profile=financier_profile(
            financier="PG Fund",
            personal_guarantee="REQUIRED",
            personal_credit_pull="NONE",
            personal_credit_dependency="NONE",
            minimum_fico=None,
        ),
        funding_requirement=req,
        pg_acceptable=True,
    )
    assert compat["pg_state"] == PG_REQUIRED
    assert compat["state"] in {FIN_POTENTIAL, "MATERIAL_UNCERTAINTY"}
    assert compat["state"] != FIN_INCOMPATIBLE
    assert compat["pg_automatically_incompatible"] is False


def test_true_hard_exhaustion():
    req = build_transaction_funding_requirement(acquisition_cost=100000, freight=0, bid_revenue=130000)
    paths = []
    for n in ("L1", "L2", "L3"):
        c = assess_financing_compatibility(
            profile=financier_profile(financier=n, minimum_fico=700),
            funding_requirement=req,
            operator_fico=400,
        )
        paths.append(c)
    assert all(p["state"] == FIN_INCOMPATIBLE for p in paths)
    gate = evaluate_funding_gate(
        funding_requirement=req, compatibility=paths, economically_attractive=True
    )
    assert gate["state"] == FUND_EXHAUSTED
    assert gate["transaction_funding_exhausted"] is True
    assert gate["exhaustion_reasons"]


def test_execution_advances_with_indication_not_bypass():
    req = build_transaction_funding_requirement(acquisition_cost=112000, freight=2800, bid_revenue=150000)
    profiles = [financier_profile(financier="X", personal_guarantee="UNKNOWN") for _ in range(3)]
    compat = [assess_financing_compatibility(profile=p, funding_requirement=req) for p in profiles]
    before = evaluate_funding_gate(
        funding_requirement=req, compatibility=compat, economically_attractive=True
    )
    assert before["state"] == FUND_VERIFY_REQ
    after = evaluate_funding_gate(
        funding_requirement=req,
        compatibility=compat,
        economically_attractive=True,
        indication_received=True,
    )
    assert after["state"] == FUND_COND_FEASIBLE
    # Execution still needs other gates
    eg = evaluate_execution_gate(
        funding_required=True,
        funding_feasible=True,
        compliance_passes=False,
        profit_floor_preserved=True,
        profit_after_financing_ok=True,
    )
    assert eg["state"] == EG_NOT_READY
    assert "compliance_not_passing" in eg["blockers"]


def test_financier_path_failed_not_global_cv_failed():
    r = apply_financing_indication_to_economics(
        bid_revenue=150000,
        acquisition=112000,
        freight=2800,
        financing_cost=3000,
        max_financing_cost=10000,
        min_fico_required=650,
        operator_fico=480,
    )
    assert r["verification_outcome"] == CV_FINANCIER_PATH_FAILED
    assert r["recalculate_remaining_paths"] is True
    assert r["global_commercial_failure"] is False


def test_ranking_unknown_vs_exhausted():
    base = {
        "transactional_fit": True,
        "economic_potential": {"status": ECON_STRONG, "base_profit": 22000},
        "cost_intelligence": {"coverage_of_bom_qty_proxy": 0.5},
        "package_readiness": {"preliminary_analysis_complete": True},
        "suppliers": [{"a": 1}],
        "pursuit_decision": {"state": PURSUIT_WORTHY_UNCERTAIN},
    }
    unk = rank_components({**base, "funding_gate": {"state": FUND_VERIFY_REQ}})
    bad = rank_components({**base, "funding_gate": {"state": FUND_EXHAUSTED}})
    assert unk["components"]["financing_compatibility"]["unknown_vs_verified_bad"] == "UNKNOWN"
    assert bad["components"]["financing_compatibility"]["unknown_vs_verified_bad"] == "VERIFIED_BAD"
    assert bad["total"] < unk["total"] - 15


def test_learning_foundation_no_generalization():
    ev = record_financing_outcome(
        financier="STAR Funding",
        decision="DECLINED",
        decline_reason="startup_history",
        transaction_size=112000,
        pg="REQUIRED",
    )
    assert ev["generalizes_to_all_lenders"] is False
    assert ev["ml_inference"] is False


def test_live_bundles_not_false_negative():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    for oid, acq, rev in (
        ("LIVE-IT-ELECTRONICS-RFQ", 112000, 150000),
        ("LIVE-EQUIPMENT-SNOW-BLADE", 22200, 32000),
    ):
        b = build_commercial_verification_bundle(
            opportunity_id=oid,
            acquisition_estimate=acq,
            acquisition_confidence="DEFENSIBLE_ESTIMATE",
            freight_estimate=2800,
            bid_revenue=rev,
            economically_attractive=True,
            product_description="widget",
            quantity=10,
        )
        assert b["funding_gate"]["transaction_funding_exhausted"] is False
        assert b["pursuit_vs_execution"]["pursuit_alive"] is True
        assert b["pursuit_vs_execution"]["deal_cannot_be_done"] is False
        assert b["outreach"]["financing_applications"] == 0

    iowa = build_commercial_verification_bundle(
        opportunity_id="645-DOTRFB-3046-2027",
        acquisition_estimate=None,
        acquisition_confidence="UNKNOWN",
        economically_attractive=False,
        product_description="seed",
        quantity=2107.6,
    )
    assert iowa["funding_gate"]["transaction_funding_exhausted"] is False
    assert iowa["pursuit_vs_execution"]["deal_cannot_be_done"] is False

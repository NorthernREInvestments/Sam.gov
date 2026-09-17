"""Focused tests for Historical Benchmark V1 + backtest integrity."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from application_clock import CLOCK_SYSTEM, clock_mode, reset_clock
from historical_benchmark_constants import (
    BENCHMARK_VERSION,
    FORBIDDEN_DISP,
    LIVE_IOWA_2975,
    MODE_FULL_DISCOVERY,
    MODE_KNOWN_OPPORTUNITY,
    TIER_BRONZE,
    TIER_GOLD,
    TIER_SILVER,
    TIER_VALIDATION_ONLY,
)
from historical_benchmark_models import (
    UNKNOWN,
    assess_benchmark_tier,
    benchmark_case,
)
from historical_backtest_runner import (
    run_economics_stage,
    run_supplier_stage,
    score_backtest,
)
from historical_case_constants import ROLE_PRE_BID_DISCOVERY, ROLE_PRE_BID_REQUIREMENT
from historical_case_inventory import verify_live_iowa_isolation
from historical_outcome_vault import (
    HistoricalOutcomeVault,
    OutcomeVaultIntegrityError,
    OutcomeVaultLockedError,
)
from prebid_decision_freeze import (
    DecisionIntegrityError,
    FrozenDecisionMutationError,
    build_prebid_decision,
    detect_tamper,
    freeze_prebid_decision,
    verify_decision_hash,
)
from temporal_evidence_api import TemporalEvidenceStore, get_evidence_available_as_of
from temporal_evidence_firewall import make_temporal_evidence


ARTIFACTS = Path(__file__).resolve().parent.parent / "artifacts"


@pytest.fixture(autouse=True)
def _clock():
    reset_clock()
    yield
    reset_clock()


def _gold_case(**kwargs):
    base = dict(
        benchmark_case_id="HBM-TEST-GOLD",
        agency="Test Agency",
        solicitation_number="RFB-123",
        product_description="LED traffic signals",
        posting_date="2024-01-01T00:00:00+00:00",
        bid_deadline="2024-01-21T00:00:00+00:00",
        award_date="2024-02-01T00:00:00+00:00",
        awardee="Vendor Co",
        award_amount=50000,
        synthetic_dates_used=False,
    )
    base.update(kwargs)
    return benchmark_case(**base)


def test_benchmark_gold_requirements():
    c = _gold_case()
    t = assess_benchmark_tier(c, prebid_recoverable=True)
    assert t["tier"] == TIER_GOLD


def test_benchmark_silver_requirements():
    c = _gold_case(posting_date=UNKNOWN, bid_deadline=UNKNOWN)
    t = assess_benchmark_tier(c, prebid_recoverable=False)
    assert t["tier"] == TIER_SILVER


def test_bronze_and_validation_only():
    c = benchmark_case(
        benchmark_case_id="B",
        agency="A",
        product_description="generators",
        awardee="V",
        award_amount=10000,
    )
    assert assess_benchmark_tier(c)["tier"] == TIER_BRONZE
    syn = _gold_case(synthetic_dates_used=True)
    assert assess_benchmark_tier(syn, prebid_recoverable=True)["tier"] == TIER_VALIDATION_ONLY


def test_synthetic_cannot_become_gold():
    syn = _gold_case(synthetic_dates_used=True)
    assert assess_benchmark_tier(syn, prebid_recoverable=True)["tier"] != TIER_GOLD


def test_live_opportunity_excluded():
    vault = HistoricalOutcomeVault()
    with pytest.raises(ValueError):
        vault.store(LIVE_IOWA_2975, {"awardee": "x"})


def test_outcome_vault_inaccessible_before_freeze():
    vault = HistoricalOutcomeVault()
    vault.store("C1", {"awardee": "V", "award_amount": 1})
    with pytest.raises(OutcomeVaultLockedError):
        vault.get_for_prebid("C1")
    with pytest.raises(OutcomeVaultLockedError):
        vault.get_for_scoring("C1")


def test_outcome_vault_accessible_after_freeze():
    vault = HistoricalOutcomeVault()
    vault.store("C1", {"awardee": "V", "award_amount": 9})
    d = build_prebid_decision(
        case_id="C1",
        simulation_start_at="2024-01-02T00:00:00+00:00",
        knowledge_cutoff_at="2024-01-02T00:00:00+00:00",
        discovered="WOULD_HAVE_DISCOVERED",
        classification={"policy_disposition": "CORE"},
        requirements_status="REQUIREMENT_PARTIAL",
        supplier_status="QUOTE_REQUIRED",
        economics_status="ECONOMICS_UNKNOWN",
        estimated_economics={},
        funding_status={"status": "NEEDS_VERIFICATION", "overlay": "CURRENT_OPERATOR_FUNDING_OVERLAY"},
        deadline_status={"status": "TIME_SUFFICIENT"},
        deal_readiness="NOT_BID_READY",
        next_operator_action="request_quote",
        disposition="WOULD_HAVE_REQUESTED_QUOTE",
        backtest_mode=MODE_KNOWN_OPPORTUNITY,
    )
    frozen = freeze_prebid_decision(d)
    out = vault.unlock_for_scoring("C1", decision_hash=frozen.hash, expected_hash=frozen.hash)
    assert out["awardee"] == "V"


def test_tampered_decision_fails_integrity():
    d = build_prebid_decision(
        case_id="C1",
        simulation_start_at="2024-01-02T00:00:00+00:00",
        knowledge_cutoff_at="2024-01-02T00:00:00+00:00",
        discovered="X",
        classification={},
        requirements_status="REQUIREMENT_UNKNOWN",
        supplier_status="INSUFFICIENT_EVIDENCE",
        economics_status="ECONOMICS_UNKNOWN",
        estimated_economics={},
        funding_status={},
        deadline_status={},
        deal_readiness="X",
        next_operator_action="x",
        disposition="INSUFFICIENT_EVIDENCE",
        backtest_mode=MODE_FULL_DISCOVERY,
    )
    frozen = freeze_prebid_decision(d)
    assert detect_tamper(frozen.to_dict(), tampered_field="disposition", tampered_value="WOULD_HAVE_CONTINUED")
    with pytest.raises(FrozenDecisionMutationError):
        frozen.mutate(disposition="x")


def test_hash_mismatch_blocks_vault():
    vault = HistoricalOutcomeVault()
    vault.store("C1", {"awardee": "V"})
    with pytest.raises(OutcomeVaultIntegrityError):
        vault.unlock_for_scoring("C1", decision_hash="abc", expected_hash="xyz")


def test_prebid_manifest_only_allowed_evidence():
    store = TemporalEvidenceStore()
    cutoff = "2024-01-10T00:00:00+00:00"
    store.add(
        make_temporal_evidence(
            case_id="C1",
            title="sol",
            evidence_role=ROLE_PRE_BID_DISCOVERY,
            knowledge_cutoff_at=cutoff,
            source_publication_date="2024-01-01T00:00:00+00:00",
            retrieval_date="2026-09-15T00:00:00+00:00",
            payload={"title": "generators"},
        )
    )
    store.add(
        make_temporal_evidence(
            case_id="C1",
            title="award",
            evidence_role="POST_BID_AWARD_EVIDENCE",
            knowledge_cutoff_at=cutoff,
            source_publication_date="2024-03-01T00:00:00+00:00",
            retrieval_date="2026-09-15T00:00:00+00:00",
            payload={"award_amount": 99999},
        )
    )
    pre = get_evidence_available_as_of(store, cutoff, for_pre_bid_decision=True, case_id="C1")
    assert len(pre) == 1
    assert all(r["evidence_role"] != "POST_BID_AWARD_EVIDENCE" for r in pre)


def test_winning_price_cannot_affect_economics():
    econ = run_economics_stage([], winning_price=123456)
    assert econ["winning_price_used"] is False
    assert econ["estimated_supplier_cost"] == UNKNOWN


def test_winning_vendor_cannot_fill_supplier_gap():
    s = run_supplier_stage([], winning_vendor="Winner Inc")
    assert s["winning_vendor_used"] is False
    assert s["status"] in {"SUPPLIER_NOT_FOUND", "INSUFFICIENT_EVIDENCE"}


def test_no_would_have_won():
    assert FORBIDDEN_DISP == "WOULD_HAVE_WON"
    with pytest.raises(ValueError):
        build_prebid_decision(
            case_id="C",
            simulation_start_at="2024-01-01T00:00:00+00:00",
            knowledge_cutoff_at="2024-01-01T00:00:00+00:00",
            discovered="x",
            classification={},
            requirements_status="x",
            supplier_status="x",
            economics_status="x",
            estimated_economics={},
            funding_status={},
            deadline_status={},
            deal_readiness="x",
            next_operator_action="x",
            disposition=FORBIDDEN_DISP,
            backtest_mode=MODE_KNOWN_OPPORTUNITY,
        )


def test_modes_differ():
    assert MODE_FULL_DISCOVERY != MODE_KNOWN_OPPORTUNITY


def test_benchmark_version_stable():
    assert BENCHMARK_VERSION == "M3_HISTORICAL_BENCHMARK_V1"


def test_funding_overlay_label_in_decision():
    d = build_prebid_decision(
        case_id="C",
        simulation_start_at="2024-01-01T00:00:00+00:00",
        knowledge_cutoff_at="2024-01-01T00:00:00+00:00",
        discovered="x",
        classification={},
        requirements_status="x",
        supplier_status="x",
        economics_status="x",
        estimated_economics={},
        funding_status={
            "overlay": "CURRENT_OPERATOR_FUNDING_OVERLAY",
            "historical_lender_policy_assumed": False,
        },
        deadline_status={},
        deal_readiness="x",
        next_operator_action="x",
        disposition="INSUFFICIENT_EVIDENCE",
        backtest_mode=MODE_KNOWN_OPPORTUNITY,
    )
    assert d["funding_status"]["overlay"] == "CURRENT_OPERATOR_FUNDING_OVERLAY"
    assert d["funding_status"]["historical_lender_policy_assumed"] is False


def test_live_iowa_isolation():
    r = verify_live_iowa_isolation(ARTIFACTS)
    assert r["live_solicitation"] == LIVE_IOWA_2975
    assert clock_mode() == CLOCK_SYSTEM


def test_decision_immutable_after_freeze():
    d = build_prebid_decision(
        case_id="C",
        simulation_start_at="2024-01-01T00:00:00+00:00",
        knowledge_cutoff_at="2024-01-01T00:00:00+00:00",
        discovered="x",
        classification={},
        requirements_status="x",
        supplier_status="x",
        economics_status="x",
        estimated_economics={},
        funding_status={},
        deadline_status={},
        deal_readiness="x",
        next_operator_action="x",
        disposition="INSUFFICIENT_EVIDENCE",
        backtest_mode=MODE_KNOWN_OPPORTUNITY,
    )
    frozen = freeze_prebid_decision(d)
    verify_decision_hash(frozen)
    with pytest.raises(FrozenDecisionMutationError):
        frozen["disposition"] = "WOULD_HAVE_CONTINUED"


def test_no_outreach_in_benchmark_modules():
    for name in (
        "historical_benchmark_constants.py",
        "historical_backtest_runner.py",
        "historical_outcome_vault.py",
    ):
        src = (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")
        assert "mailto:" not in src

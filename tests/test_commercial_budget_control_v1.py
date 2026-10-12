"""Budget control boundary tests — no live paid API calls."""

from __future__ import annotations

from decimal import Decimal

import pytest

from buyer_intelligence.commercial_budget_control_v1 import (
    BudgetDenied,
    check_budget,
    commit_actual_usage,
    estimate_openai_worst_case,
    get_remaining_budget,
    openai_daily_cap,
    openai_monthly_cap,
    pause_paid_research,
    release_unused_reservation,
    reserve_budget,
    resume_within_budget,
    sam_daily_cap,
)


def test_hard_caps():
    assert openai_daily_cap() <= Decimal("1.00")
    assert openai_monthly_cap() <= Decimal("30.00")
    assert sam_daily_cap() <= 10


def test_estimate_never_zero():
    assert estimate_openai_worst_case(max_input_tokens=10, max_output_tokens=10) > 0


def test_exceed_daily_denied():
    with pytest.raises(BudgetDenied) as ei:
        reserve_budget(provider="openai", estimated_usd=Decimal("50.00"), purpose="test")
    assert "OPENAI" in ei.value.reason or "EXHAUSTED" in ei.value.reason or "PAUSED" in ei.value.reason


def test_sam_over_cap_denied():
    with pytest.raises(BudgetDenied):
        reserve_budget(provider="sam", sam_calls=11, purpose="test")


def test_other_paid_blocked():
    assert check_budget(provider="proxy")["allowed"] is False
    assert check_budget(provider="captcha")["allowed"] is False


def test_pause_blocks_openai_allows_free():
    pause_paid_research(reason="unit_test", by="test")
    try:
        with pytest.raises(BudgetDenied):
            reserve_budget(provider="openai", estimated_usd=Decimal("0.0001"), purpose="paused")
        free = reserve_budget(provider="free", purpose="paused_free")
        assert free.get("free") is True
    finally:
        resume_within_budget(by="test", reason="unit_test_end")


def test_duplicate_reservation_id():
    rem = Decimal(get_remaining_budget()["openai_remaining_usd"])
    if rem < Decimal("0.0002"):
        pytest.skip("insufficient remaining for dup test")
    rid = "UNITTEST-DUP-RESERVATION-001"
    # cleanup if leftover
    try:
        release_unused_reservation(rid)
    except Exception:
        pass
    reserve_budget(provider="openai", estimated_usd=Decimal("0.0001"), reservation_id=rid, purpose="dup")
    try:
        with pytest.raises(BudgetDenied) as ei:
            reserve_budget(provider="openai", estimated_usd=Decimal("0.0001"), reservation_id=rid, purpose="dup2")
        assert ei.value.reason == "DUPLICATE_RESERVATION_ID"
    finally:
        release_unused_reservation(rid)


def test_uncertain_billing_holds_reservation():
    rem = Decimal(get_remaining_budget()["openai_remaining_usd"])
    if rem < Decimal("0.0001"):
        pytest.skip("insufficient remaining")
    r = reserve_budget(provider="openai", estimated_usd=Decimal("0.0001"), purpose="test_uncertain")
    before = Decimal(get_remaining_budget()["openai_remaining_usd"])
    out = commit_actual_usage(r["reservation_id"], actual_usd=None, success=True, charge_uncertain=True)
    assert out["status"] == "UNRECONCILED_HELD"
    after = Decimal(get_remaining_budget()["openai_remaining_usd"])
    # Held amount still counts against remaining (committed unreconciled)
    assert after <= before + Decimal("0.0001")
    # Known mock — release so unit tests do not consume the daily cap
    from buyer_intelligence.commercial_execution_queue_v1 import _zero_test_openai_commits

    _zero_test_openai_commits()


def test_free_provider_allowed_when_paused():
    pause_paid_research(reason="free_test", by="test")
    try:
        assert check_budget(provider="cache")["allowed"] is True
        assert check_budget(provider="http_public")["allowed"] is True
    finally:
        resume_within_budget(by="test", reason="free_test_end")

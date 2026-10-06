"""Tests for basket full-funnel reconcile."""

from __future__ import annotations

from basket_full_funnel_reconcile.economics import classify_basket_ready
from basket_full_funnel_reconcile.funnel import write_stage_contracts, conservation_audit
from basket_full_funnel_reconcile.models import (
    BASKET_READY_PUBLIC_PRICE,
    BUILD,
    CANONICAL_STAGES,
    PRICED_EXECUTABLE,
    TARGET_BOTH_SIDES,
)
from basket_full_funnel_reconcile.owner_ui import owner_next_action


def test_build_id():
    assert BUILD == "20261005-m3-basket-full-funnel-reconcile-v1"
    assert TARGET_BOTH_SIDES == 16


def test_stage_contracts():
    c = write_stage_contracts()
    assert "BASKET_READY" in c["contracts"]
    assert "ECONOMICS_READY" in c["contracts"]
    assert len(CANONICAL_STAGES) >= 17


def test_basket_ready_all_priced():
    lr = {
        "TOTAL_LINES": 2,
        "PRICED_LINES": 2,
        "QUOTE_REQUIRED_LINES": 0,
        "line_coverage": 1.0,
        "value_weighted_coverage": 1.0,
        "lines": [
            {"terminal_state": PRICED_EXECUTABLE},
            {"terminal_state": PRICED_EXECUTABLE},
        ],
    }
    out = classify_basket_ready(lr)
    assert out["basket_class"] == BASKET_READY_PUBLIC_PRICE


def test_conservation_zero_diff():
    counts = {
        "DISCOVERED": {"entered": 10, "advanced": 8, "retryable": 1, "blocked": 0, "rejected": 1, "terminal": 0},
    }
    audit = conservation_audit(counts)
    assert audit["opportunity_diff"] == 0
    assert audit["pass"] is True


def test_next_action_reject_unprofitable():
    result = {
        "economics": {"economic_terminal": "UNPROFITABLE", "financing_status": "FINANCING_UNKNOWN"},
        "basket": {"basket_class": "BASKET_BLOCKED"},
        "lines": {"QUOTE_REQUIRED_LINES": 0, "UNRESOLVED_LINES": 0},
    }
    assert owner_next_action(result)["code"] == "REJECT"

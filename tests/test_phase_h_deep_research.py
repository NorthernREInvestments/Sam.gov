"""Phase H unit tests (no live network required for core logic)."""

from __future__ import annotations

from phase_h.deep_research import (
    STATE_QUOTE_OUTREACH,
    STATE_RESEARCHED,
    _economics_state,
    _history_class,
    _phase_h_readiness,
)


def test_history_class_levels():
    assert _history_class([]) == "NO_HISTORY_FOUND"
    assert _history_class([{"Amount": 100}]) == "MODERATE_HISTORY"
    assert _history_class([{"Amount": 1}, {"Amount": 2}, {"Amount": 3}]) == "STRONG_HISTORY"


def test_sam_listing_url_from_notice_id():
    from phase_h.deep_research import _sam_listing_url

    url = _sam_listing_url({"canonical_id": "dece14c3ef3944d895d47925a76fcd64"})
    assert url and url.endswith("/view")
    assert "dece14c3ef3944d895d47925a76fcd64" in url


def test_economics_promising_when_revenue_and_identity():
    st = _economics_state(
        revenue=50000.0,
        history_class="MODERATE_HISTORY",
        max_cost={"maximum_allowable_supplier_cost": 40000.0},
        public_price=None,
        identity="STRONG_MATCH",
        docs_ok=True,
    )
    assert st == "ECONOMICS_PROMISING_QUOTE_REQUIRED"


def test_quote_outreach_requires_history_basis():
    packet = {"supplier_cost_known": False, "phase_h_max_supplier_cost": {"maximum_allowable_supplier_cost": 1000}}
    # No history → not quote ready via primary path
    r = _phase_h_readiness(
        identity="STRONG_MATCH",
        docs_reviewed=True,
        history_class="NO_HISTORY_FOUND",
        economics_state="ECONOMICS_BLOCKED_BY_UNKNOWN_REVENUE",
        funding_state="UNKNOWN",
        deadline_blocked=False,
        hard_blockers=[],
        packet=packet,
    )
    assert r == STATE_RESEARCHED
    # With history + max cost → quote outreach
    r2 = _phase_h_readiness(
        identity="STRONG_MATCH",
        docs_reviewed=False,
        history_class="MODERATE_HISTORY",
        economics_state="ECONOMICS_PROMISING_QUOTE_REQUIRED",
        funding_state="UNKNOWN",
        deadline_blocked=False,
        hard_blockers=[],
        packet=packet,
    )
    assert r2 == STATE_QUOTE_OUTREACH

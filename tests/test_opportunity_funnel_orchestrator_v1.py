"""One-pass opportunity funnel orchestrator."""

from __future__ import annotations


def test_fast_qualify_expired():
    from buyer_intelligence.opportunity_funnel_orchestrator_v1 import (
        OUTCOME_EXPIRED,
        fast_qualify,
    )

    r = fast_qualify({"opportunity_id": "x", "days_left": -1, "title": "Copy Paper"})
    assert r["ok"] is False
    assert r["outcome"] == OUTCOME_EXPIRED


def test_fast_qualify_pass_product():
    from buyer_intelligence.opportunity_funnel_orchestrator_v1 import fast_qualify

    r = fast_qualify(
        {"opportunity_id": "y", "days_left": 12, "title": "Copy Paper Case Supplies"}
    )
    assert r["ok"] is True


def test_classify_software_error_defers():
    from buyer_intelligence.opportunity_funnel_orchestrator_v1 import (
        OUTCOME_SOFTWARE_ERROR,
        classify_process_one_outcome,
    )

    c = classify_process_one_outcome(
        {"terminal_blocker": "DETAIL_FETCH_EMPTY", "documents_downloaded": 0}
    )
    assert c["outcome"] == OUTCOME_SOFTWARE_ERROR
    assert c["defer"] is True


def test_classify_complete_package():
    from buyer_intelligence.opportunity_funnel_orchestrator_v1 import (
        OUTCOME_COMPLETE,
        STAGE_BID_PREP,
        classify_process_one_outcome,
    )

    c = classify_process_one_outcome(
        {
            "documents_downloaded": 4,
            "line_count": 44,
            "complete_spec": True,
            "public_unit_price": "12.50",
            "rfq": {"status": "AWAITING_OWNER_APPROVED_OUTREACH"},
            "econ_state": "PROFITABLE_BUT_QUOTE_MISSING",
        }
    )
    assert c["outcome"] == OUTCOME_COMPLETE
    assert c["stage_reached"] == STAGE_BID_PREP
    assert c["defer"] is False

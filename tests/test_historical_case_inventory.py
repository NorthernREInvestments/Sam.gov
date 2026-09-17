"""Focused tests: historical case inventory + temporal evidence firewall."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from application_clock import (
    CLOCK_HISTORICAL_SIMULATION,
    CLOCK_SYSTEM,
    FrozenClock,
    clock_mode,
    freeze_time,
    knowledge_cutoff_at,
    reset_clock,
    set_clock,
)
from historical_case_constants import (
    BACKTEST_HYPOTHETICAL_ONLY,
    BACKTEST_NOT_SUITABLE,
    CLASS_CORE_PRODUCT_RESALE,
    CLASS_HYPOTHETICAL_EXAMPLE,
    CLASS_SERVICE_DOMINANT,
    FORBIDDEN_COUNTERFACTUAL,
    LIVE_IOWA_SOLICITATION,
    ROLE_POST_BID_AWARD,
    ROLE_POST_BID_OUTCOME,
    ROLE_PRE_BID_DISCOVERY,
    ROLE_PRE_BID_PRICE,
    ROLE_PRE_BID_REQUIREMENT,
    STATUS_SOURCE_CLAIM,
    STATUS_SOURCE_CLAIM_ONLY,
    STATUS_UNKNOWN,
    TEMPORAL_AVAILABLE_AT_CUTOFF,
    TEMPORAL_AVAILABLE_BEFORE_CUTOFF,
    TEMPORAL_POST_CUTOFF,
    TEMPORAL_PUBLICATION_DATE_UNKNOWN,
)
from historical_case_inventory import (
    assess_backtest_suitability,
    assess_source_corpus,
    assess_traceability,
    build_case_record,
    build_inventory,
    classify_for_primary_queue,
    extract_case_leads_from_kizzy_corpus,
    select_simulation_cutoff,
    verify_live_iowa_isolation,
)
from historical_case_models import (
    UNKNOWN,
    claim_vs_verified,
    historical_case_lead,
    historical_case_timeline,
    historical_economic_outcome,
    set_outcome_field,
    source_claim,
    timeline_event,
)
from temporal_evidence_api import (
    TemporalEvidenceStore,
    assert_live_mode_uncontaminated,
    get_evidence_available_as_of,
)
from temporal_evidence_firewall import (
    classify_temporal_state,
    filter_pre_bid_context,
    make_temporal_evidence,
    require_knowledge_cutoff_in_historical_mode,
)


ARTIFACTS = Path(__file__).resolve().parent.parent / "artifacts"


@pytest.fixture(autouse=True)
def _restore_clock():
    reset_clock()
    yield
    reset_clock()


def test_frozen_clock_historical_simulation_requires_cutoff():
    as_of = datetime(2023, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    set_clock(FrozenClock(as_of, mode=CLOCK_HISTORICAL_SIMULATION))
    assert clock_mode() == CLOCK_HISTORICAL_SIMULATION
    assert knowledge_cutoff_at() == as_of
    assert require_knowledge_cutoff_in_historical_mode() == as_of


def test_knowledge_cutoff_required_outside_raises_when_not_historical():
    reset_clock()
    with pytest.raises(RuntimeError):
        require_knowledge_cutoff_in_historical_mode()


def test_pre_cutoff_evidence_allowed():
    cutoff = datetime(2023, 6, 1, tzinfo=timezone.utc)
    rec = make_temporal_evidence(
        title="solicitation",
        evidence_role=ROLE_PRE_BID_DISCOVERY,
        knowledge_cutoff_at=cutoff,
        source_publication_date="2023-05-01T00:00:00+00:00",
        retrieval_date="2026-09-15T00:00:00+00:00",
    )
    assert rec["temporal_state"] == TEMPORAL_AVAILABLE_BEFORE_CUTOFF
    assert rec["allowed_for_simulation"] is True


def test_evidence_exactly_at_cutoff_allowed_deterministically():
    cutoff = datetime(2023, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    rec = make_temporal_evidence(
        title="at cutoff",
        evidence_role=ROLE_PRE_BID_REQUIREMENT,
        knowledge_cutoff_at=cutoff,
        source_publication_date=cutoff.isoformat(),
        historical_availability_date=cutoff.isoformat(),
        retrieval_date="2026-01-01T00:00:00+00:00",
    )
    assert rec["temporal_state"] == TEMPORAL_AVAILABLE_AT_CUTOFF
    assert rec["allowed_for_simulation"] is True


def test_post_cutoff_evidence_blocked():
    cutoff = datetime(2023, 6, 1, tzinfo=timezone.utc)
    rec = make_temporal_evidence(
        title="award",
        evidence_role=ROLE_POST_BID_AWARD,
        knowledge_cutoff_at=cutoff,
        source_publication_date="2023-08-01T00:00:00+00:00",
        retrieval_date="2026-09-15T00:00:00+00:00",
    )
    assert rec["temporal_state"] == TEMPORAL_POST_CUTOFF
    assert rec["allowed_for_simulation"] is False


def test_unknown_publication_date_preserved():
    cutoff = datetime(2023, 6, 1, tzinfo=timezone.utc)
    rec = make_temporal_evidence(
        title="undated",
        evidence_role=ROLE_PRE_BID_DISCOVERY,
        knowledge_cutoff_at=cutoff,
        source_publication_date=None,
        retrieval_date="2026-09-15T00:00:00+00:00",
    )
    assert rec["temporal_state"] == TEMPORAL_PUBLICATION_DATE_UNKNOWN
    assert rec["allowed_for_simulation"] is False


def test_retrieval_date_does_not_equal_historical_availability():
    cutoff = datetime(2023, 6, 1, tzinfo=timezone.utc)
    rec = make_temporal_evidence(
        title="old pdf retrieved late",
        evidence_role=ROLE_PRE_BID_REQUIREMENT,
        knowledge_cutoff_at=cutoff,
        source_publication_date="2023-04-01T00:00:00+00:00",
        historical_availability_date="2023-04-01T00:00:00+00:00",
        retrieval_date="2026-09-15T00:00:00+00:00",
    )
    assert rec["retrieval_date"] != rec["historical_availability_date"]
    assert rec["retrieval_is_not_historical_availability"] is True
    assert rec["allowed_for_simulation"] is True


def test_historical_availability_may_precede_retrieval():
    state, _ = classify_temporal_state(
        datetime(2023, 6, 1, tzinfo=timezone.utc),
        historical_availability_date="2023-01-01T00:00:00+00:00",
        retrieval_date="2026-01-01T00:00:00+00:00",
    )
    assert state == TEMPORAL_AVAILABLE_BEFORE_CUTOFF


def test_award_notice_blocked_from_pre_bid_context():
    cutoff = datetime(2023, 6, 1, tzinfo=timezone.utc)
    award = make_temporal_evidence(
        title="award notice",
        evidence_role=ROLE_POST_BID_AWARD,
        knowledge_cutoff_at=cutoff,
        source_publication_date="2023-07-01T00:00:00+00:00",
        retrieval_date="2026-01-01T00:00:00+00:00",
    )
    # Even if somehow pre-cutoff publication, POST_BID role blocks
    pre_cutoff_award = make_temporal_evidence(
        title="weird early award pub",
        evidence_role=ROLE_POST_BID_AWARD,
        knowledge_cutoff_at=cutoff,
        source_publication_date="2023-05-01T00:00:00+00:00",
        retrieval_date="2026-01-01T00:00:00+00:00",
    )
    allowed = filter_pre_bid_context([award, pre_cutoff_award])
    assert all(r["evidence_role"] != ROLE_POST_BID_AWARD for r in allowed)


def test_later_creator_video_blocked():
    cutoff = datetime(2023, 6, 1, tzinfo=timezone.utc)
    video = make_temporal_evidence(
        title="YouTube describing winner",
        evidence_role=ROLE_POST_BID_OUTCOME,
        knowledge_cutoff_at=cutoff,
        source_publication_date="2024-01-01T00:00:00+00:00",
        retrieval_date="2026-01-01T00:00:00+00:00",
    )
    assert video["allowed_for_simulation"] is False
    assert filter_pre_bid_context([video]) == []


def test_prior_historical_award_before_cutoff_may_be_allowed():
    cutoff = datetime(2023, 6, 1, tzinfo=timezone.utc)
    prior = make_temporal_evidence(
        title="prior contract price",
        evidence_role=ROLE_PRE_BID_PRICE,
        knowledge_cutoff_at=cutoff,
        source_publication_date="2022-01-01T00:00:00+00:00",
        retrieval_date="2026-01-01T00:00:00+00:00",
    )
    assert prior["allowed_for_simulation"] is True


def test_source_claim_does_not_become_verified_fact():
    sc = source_claim("claimed_profit", 25000, source_name="transcript")
    assert sc["claim_status"] == STATUS_SOURCE_CLAIM
    resolved = claim_vs_verified(source_claim_record=sc, verified=None)
    assert resolved["resolved_status"] == STATUS_SOURCE_CLAIM
    assert resolved["verified_fact"] is None


def test_claimed_profit_remains_source_claim_on_outcome():
    oc = historical_economic_outcome("HCL-TEST")
    set_outcome_field(oc, "historical_actual_profit", 25000, STATUS_SOURCE_CLAIM)
    assert oc["fields"]["historical_actual_profit"]["status"] == STATUS_SOURCE_CLAIM_ONLY
    assert oc["allowed_in_pre_bid_decision"] is False


def test_unknown_supplier_cost_remains_unknown():
    oc = historical_economic_outcome("HCL-TEST")
    assert oc["fields"]["historical_supplier_cost"]["value"] == UNKNOWN
    assert oc["fields"]["historical_supplier_cost"]["status"] == STATUS_UNKNOWN


def test_reconstructed_outcome_cannot_leak_into_simulation():
    cutoff = datetime(2023, 6, 1, tzinfo=timezone.utc)
    store = TemporalEvidenceStore()
    store.add(
        make_temporal_evidence(
            title="outcome profit",
            evidence_role=ROLE_POST_BID_OUTCOME,
            knowledge_cutoff_at=cutoff,
            source_publication_date="2023-09-01T00:00:00+00:00",
            retrieval_date="2026-01-01T00:00:00+00:00",
            payload={"historical_actual_profit": 25000},
        )
    )
    pre = get_evidence_available_as_of(store, cutoff, for_pre_bid_decision=True)
    assert pre == []
    scoring = get_evidence_available_as_of(
        store, cutoff, for_pre_bid_decision=False, include_post_cutoff_for_scoring=True
    )
    assert len(scoring) == 1


def test_case_timeline_preserves_provenance():
    tl = historical_case_timeline(
        "HCL-TEST",
        events=[
            timeline_event(
                "bid_deadline",
                "2023-06-15T17:00:00+00:00",
                provenance="sam_notice",
                evidence_id="E1",
            )
        ],
    )
    assert tl["provenance_preserved"] is True
    assert tl["events"]["bid_deadline"]["provenance"] == "sam_notice"
    assert tl["events"]["award_date"]["event_date"] == UNKNOWN


def test_simulation_cutoff_confidence():
    cut = select_simulation_cutoff(
        posting_date="2023-01-01T00:00:00+00:00",
        bid_deadline="2023-02-01T00:00:00+00:00",
    )
    assert cut["confidence"] in {
        "SIMULATION_CUTOFF_HIGH_CONFIDENCE",
        "SIMULATION_CUTOFF_MEDIUM_CONFIDENCE",
        "SIMULATION_CUTOFF_LOW_CONFIDENCE",
    }
    assert cut["simulation_as_of"] != UNKNOWN
    unavailable = select_simulation_cutoff()
    assert unavailable["confidence"] == "SIMULATION_CUTOFF_UNAVAILABLE"


def test_traceability_states():
    lead = historical_case_lead(
        case_id="HCL-T",
        source_name="t",
        source_type="TEST",
        case_classification=CLASS_CORE_PRODUCT_RESALE,
        claimed={"claimed_product": "widgets", "claimed_agency": "Agency X"},
    )
    weak = assess_traceability(lead)
    assert weak["traceability_state"] in {"WEAKLY_TRACEABLE", "UNTRACEABLE", "PARTIALLY_TRACEABLE"}
    strong = assess_traceability(
        lead,
        public_research={
            "solicitation_identified": True,
            "agency_identified": True,
            "bid_deadline_identified": True,
            "award_independently_verified": True,
            "awardee_independently_verified": True,
            "award_amount_independently_verified": True,
            "pre_bid_documents_available": True,
            "historical_public_documents_available": True,
        },
    )
    assert strong["traceability_state"] in {"STRONGLY_TRACEABLE", "FULLY_TRACEABLE"}


def test_backtest_suitability_states():
    hypo = historical_case_lead(
        case_id="H",
        source_name="t",
        source_type="TEST",
        case_classification=CLASS_HYPOTHETICAL_EXAMPLE,
    )
    r = assess_backtest_suitability(hypo, {"traceability_state": "UNTRACEABLE"}, {"confidence": "SIMULATION_CUTOFF_UNAVAILABLE"})
    assert r["backtest_suitability"] == BACKTEST_HYPOTHETICAL_ONLY

    svc = historical_case_lead(
        case_id="S",
        source_name="t",
        source_type="TEST",
        case_classification=CLASS_SERVICE_DOMINANT,
    )
    r2 = assess_backtest_suitability(svc, {"traceability_state": "WEAKLY_TRACEABLE"}, {"confidence": "SIMULATION_CUTOFF_UNAVAILABLE"})
    assert r2["backtest_suitability"] == BACKTEST_NOT_SUITABLE


def test_hypothetical_excluded_service_excluded_product_retained():
    leads = [
        historical_case_lead(
            case_id="1",
            source_name="t",
            source_type="TEST",
            case_classification=CLASS_HYPOTHETICAL_EXAMPLE,
        ),
        historical_case_lead(
            case_id="2",
            source_name="t",
            source_type="TEST",
            case_classification=CLASS_SERVICE_DOMINANT,
        ),
        historical_case_lead(
            case_id="3",
            source_name="t",
            source_type="TEST",
            case_classification=CLASS_CORE_PRODUCT_RESALE,
            claimed={"claimed_product": "machines"},
        ),
    ]
    assert classify_for_primary_queue(leads[0]) is False
    assert classify_for_primary_queue(leads[1]) is False
    assert classify_for_primary_queue(leads[2]) is True


def test_temporal_evidence_api_filtering():
    cutoff = datetime(2023, 6, 1, tzinfo=timezone.utc)
    store = TemporalEvidenceStore()
    store.add_evidence(
        title="ok",
        evidence_role=ROLE_PRE_BID_DISCOVERY,
        knowledge_cutoff_at=cutoff,
        source_publication_date="2023-05-01T00:00:00+00:00",
        retrieval_date="2026-01-01T00:00:00+00:00",
    )
    store.add_evidence(
        title="late",
        evidence_role=ROLE_POST_BID_AWARD,
        knowledge_cutoff_at=cutoff,
        source_publication_date="2023-08-01T00:00:00+00:00",
        retrieval_date="2026-01-01T00:00:00+00:00",
    )
    with freeze_time(cutoff, mode=CLOCK_HISTORICAL_SIMULATION):
        got = get_evidence_available_as_of(store, cutoff, for_pre_bid_decision=True)
    assert len(got) == 1
    assert got[0]["title"] == "ok"


def test_system_mode_unaffected():
    reset_clock()
    assert clock_mode() == CLOCK_SYSTEM
    snap = assert_live_mode_uncontaminated()
    assert snap["system_clock_active"] is True


def test_store_rejects_live_iowa_contamination():
    store = TemporalEvidenceStore()
    with pytest.raises(ValueError):
        store.add(
            make_temporal_evidence(
                title="live leak",
                evidence_role=ROLE_PRE_BID_DISCOVERY,
                knowledge_cutoff_at="2026-01-01T00:00:00+00:00",
                source_publication_date="2025-01-01T00:00:00+00:00",
                retrieval_date="2026-01-01T00:00:00+00:00",
                payload={"solicitation_number": LIVE_IOWA_SOLICITATION},
            )
        )


def test_no_forbidden_would_have_won_constant_used_as_valid():
    assert FORBIDDEN_COUNTERFACTUAL == "WOULD_HAVE_WON"


def test_corpus_assessment_and_extraction():
    corpus = assess_source_corpus()
    assert corpus["status"] in {
        "SOURCE_CORPUS_PARTIAL",
        "SOURCE_CORPUS_AVAILABLE",
        "SOURCE_CORPUS_NOT_AVAILABLE",
    }
    if corpus["transcript_corpus_available"]:
        leads = extract_case_leads_from_kizzy_corpus()
        assert len(leads) >= 4
        assert any(L["case_classification"] == CLASS_CORE_PRODUCT_RESALE for L in leads)
        assert any(L["case_classification"] == CLASS_SERVICE_DOMINANT for L in leads)
        assert any(L["case_classification"] == CLASS_HYPOTHETICAL_EXAMPLE for L in leads)


def test_live_iowa_isolation_helper():
    result = verify_live_iowa_isolation(ARTIFACTS)
    assert result["live_solicitation"] == LIVE_IOWA_SOLICITATION
    # Packet should exist from prior missions
    assert "live_packet_contaminated" in result


def test_build_inventory_smoke():
    inv = build_inventory()
    assert "source_corpus" in inv
    assert "leads" in inv
    if inv["source_corpus"]["transcript_corpus_available"]:
        rec = build_case_record(inv["leads"][0])
        assert "traceability" in rec
        assert "backtest_suitability" in rec


def test_no_outreach_counters_in_module_defaults():
    # Architectural: inventory module must not define outreach runners
    import historical_case_inventory as hci

    src = Path(hci.__file__).read_text(encoding="utf-8")
    assert "mailto:" not in src
    assert "twilio" not in src.lower()

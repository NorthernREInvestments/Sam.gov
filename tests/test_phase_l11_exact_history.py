"""Phase L.11 exact award-history + resilient hunt tests."""

from __future__ import annotations

from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.buyer_registry import (
    build_auth_history_gap_queue,
    infer_buyer_type,
    infer_category,
    state_coverage_report,
)
from phase_l.exact_history_recovery import (
    BUYER_FIRST_ORDER,
    GOV_UPGRADED_A,
    GOV_UPGRADED_C,
    HISTORY_AUTH_REQUIRED,
    PUBLIC_REGISTRATION_REQUIRED,
    BUYER_VENDOR_ACCOUNT_REQUIRED,
    RULE_GOV_A_SAME_BUYER_EXACT_AWARD,
    RULE_GOV_B_OTHER_GOV_EXACT_MODEL,
    classify_auth_wall,
    configuration_compatible,
    grade_recovered_award,
    run_exact_history_recovery,
    solicitation_search_variants,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, obsolete_rule_active
from phase_l.platform_history import detect_platform, run_buyer_pivot, weak_platform_audit
from phase_l.quality_audit import GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C, GOV_VALUE_D
from phase_l.resilient_hunt import (
    HUNT_COMPLETE_WITH_FAILURES,
    SOURCE_TIMEOUT,
    order_candidates_by_health,
    update_source_health,
)


def test_buyer_first_order():
    assert BUYER_FIRST_ORDER[0] == "same_buyer_solicitation_family"
    assert "same_buyer_bid_tab" in BUYER_FIRST_ORDER
    assert BUYER_FIRST_ORDER[-1] == "same_buyer_archived_solicitation"


def test_gov_a_same_buyer_exact():
    graded = grade_recovered_award(
        {
            "buyer": "CITY OF AUSTIN",
            "model": "F-150",
            "manufacturer": "Ford",
            "unit_price": 48000,
            "quantity": 2,
            "source": "buyer_memory:same_buyer_exact_manufacturer_model",
            "item": "Ford F-150",
        },
        row={"agency": "City of Austin", "title": "Ford F-150 fleet trucks"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert graded["grade"] == GOV_VALUE_A
    assert graded["rule_id"] == RULE_GOV_A_SAME_BUYER_EXACT_AWARD


def test_gov_b_other_gov_exact_model():
    graded = grade_recovered_award(
        {
            "buyer": "CITY OF DALLAS",
            "model": "F-150",
            "manufacturer": "Ford",
            "unit_price": 47000,
            "source": "other_gov",
            "item": "Ford F-150",
        },
        row={"agency": "City of Austin", "title": "Ford F-150"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert graded["grade"] == GOV_VALUE_B
    assert graded["rule_id"] == RULE_GOV_B_OTHER_GOV_EXACT_MODEL


def test_config_mismatch_not_gov_a():
    compat = configuration_compatible(
        {"title": "Bobcat loader with bucket package"},
        {"item": "Bobcat loader bare machine", "model": "T770", "manufacturer": "Bobcat"},
        commercial={"manufacturer": "Bobcat", "model": "T770"},
    )
    assert compat["exact"] is False
    graded = grade_recovered_award(
        {
            "buyer": "CITY OF AUSTIN",
            "model": "T770",
            "manufacturer": "Bobcat",
            "unit_price": 65000,
            "item": "Bobcat loader bare machine",
        },
        row={"agency": "City of Austin", "title": "Bobcat loader with bucket package"},
        commercial={"manufacturer": "Bobcat", "model": "T770"},
    )
    assert graded["grade"] != GOV_VALUE_A


def test_no_unit_from_total_without_qty():
    graded = grade_recovered_award(
        {"buyer": "CITY", "total": 100000, "model": "F-150", "manufacturer": "Ford", "item": "F-150"},
        row={"agency": "CITY", "title": "F-150"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert graded["grade"] == GOV_VALUE_D
    assert graded["reason"] == "total_without_quantity_not_unit"


def test_auth_classification_distinct():
    assert classify_auth_wall("supplier portal vendor account") == BUYER_VENDOR_ACCOUNT_REQUIRED
    assert classify_auth_wall("please register for free") == PUBLIC_REGISTRATION_REQUIRED
    assert "AUTH" in HISTORY_AUTH_REQUIRED


def test_solicitation_variants():
    v = solicitation_search_variants("IFB-2025-001")
    assert any("award" in x for x in v)
    assert any("tabulation" in x for x in v)


def test_exact_history_recovery_exhausts_cleanly():
    res = run_exact_history_recovery(
        {"title": "Generic office chairs", "agency": "Tiny Town"},
        commercial={},
        authorize_live=False,
    )
    assert res["state"] == "EXACT_HISTORY_RECOVERY"
    assert res["outcome"] in {
        "HISTORY_EVIDENCE_EXHAUSTED",
        "HISTORY_BUYER_RECORDS_NOT_FOUND",
        "HISTORY_AUTH_REQUIRED",
        GOV_UPGRADED_C,
    }
    assert res["grade_after"] in {GOV_VALUE_D, GOV_VALUE_C, "GOV_VALUE_UNKNOWN"}


def test_exact_history_upgrades_from_memory():
    mem = {
        "entries": {
            "CITY|F-150": {
                "buyer": "CITY OF AUSTIN",
                "model": "F-150",
                "manufacturer": "Ford",
                "unit_value": 49000,
                "solicitation_id": "IFB-1",
            }
        }
    }
    res = run_exact_history_recovery(
        {"title": "Ford F-150 trucks", "agency": "City of Austin", "solicitation_id": "IFB-1"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
        buyer_memory=mem,
        authorize_live=False,
    )
    assert res["grade_after"] == GOV_VALUE_A
    assert res["outcome"] == GOV_UPGRADED_A


def test_buyer_pivot_seeds():
    piv = run_buyer_pivot(
        {"agency": "City of Springfield", "title": "Fleet", "solicitation_id": "X1"},
        commercial={"model": "F-150"},
        authorize_live=False,
    )
    assert piv["urls_seeded"]
    assert any(a.get("step") == "buyer_pivot_url_seeds" for a in piv["attempts"])


def test_resilient_health_ordering():
    update_source_health("network_bidnet_texas", ok=True, latency_ms=100, records=50)
    update_source_health("bad_source", ok=False, latency_ms=5000, error=SOURCE_TIMEOUT)
    ordered = order_candidates_by_health(
        [
            {"source_id": "bad_source", "list_url": "http://x"},
            {"source_id": "network_bidnet_texas", "list_url": "http://y"},
        ]
    )
    assert ordered[0]["source_id"] == "network_bidnet_texas"


def test_coverage_and_auth_gap():
    rows = [
        {
            "agency": "City of Austin",
            "title": "Ford F-150",
            "our_bid_access": "YES",
            "raw_ref": {"source_id": "network_bidnet_texas"},
            "jurisdiction": "MULTI_AGENCY_NETWORK",
        }
    ]
    cov = state_coverage_report(rows)
    assert "TX" in cov["states"]
    assert cov["claims_50_state_coverage"] is False
    assert infer_buyer_type(rows[0]) == "city"
    assert infer_category(rows[0]) == "fleet"
    q = build_auth_history_gap_queue(rows, audits=[{"opportunity_id": "", "outcome": "HISTORY_AUTH_REQUIRED", "auth_class": "AUTH_ACCOUNT_REQUIRED"}])
    assert isinstance(q, list)


def test_weak_platforms_classified():
    weak = weak_platform_audit()
    names = {w["platform"] for w in weak}
    assert "OpenGov" in names or "Bonfire" in names
    assert detect_platform({"raw_ref": {"source_id": "network_bidnet_ohio"}}) == "BidNet"


def test_no_fixed_caps_and_legacy():
    assert STAGE3_NO_ROW_CAP
    assert assert_no_fixed_positive_cap(1000)
    assert obsolete_rule_active("LEGACY_LOOSE_CATEGORY_BENCHMARK_AS_VALIDATED") is False


def test_hunt_status_constants():
    assert HUNT_COMPLETE_WITH_FAILURES == "COMPLETE_WITH_SOURCE_FAILURES"
    assert SOURCE_TIMEOUT == "SOURCE_TIMEOUT"

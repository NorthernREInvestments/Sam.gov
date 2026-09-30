"""BUILD 7 — Discovery Planner tests."""

from __future__ import annotations

from m3_demand_signal import (
    DETECTED,
    HISTORICAL_PATTERN,
    SOURCES_SOUGHT,
    UNKNOWN,
    VALIDATED,
    empty_signal,
)
from m3_discovery_planner import (
    APPROVED,
    BUILD_TAG,
    DEFAULT_MAX_PLANS,
    DRAFT,
    build_discovery_plans,
    plan_from_signal,
)


def _validated_historical_signal(**extra):
    s = empty_signal(
        signal_id="HISTORICAL_PATTERN|nsn:4320-01-243-1951|DLA",
        signal_type=HISTORICAL_PATTERN,
        related_product={
            "product_id": 11,
            "product_dedupe_key": "nsn:4320-01-243-1951",
            "nsn": "4320-01-243-1951",
            "title": "PUMP, ROTARY",
        },
        product_id=11,
        product_dedupe_key="nsn:4320-01-243-1951",
        related_agency="DLA",
        related_buyer="DLA Aviation",
        source="award_product_projection",
        evidence=[{"source": "historical", "snippet": "2 awards", "confidence": "HIGH"}],
        confidence="HIGH",
        expected_timing="last_award_approx_10_months_ago",
        status=VALIDATED,
        monitoring_recipe={
            "automate_search": False,
            "monitor_sources": ["fed_sam_contract_opportunities", "dla_via_sam_spe_spr"],
            "agency_filter": "DLA",
        },
        opportunity_id="sol:SPE7M126DP1:dla",
    )
    s.update(extra)
    return s


def _weak_signal():
    return empty_signal(
        signal_id="FORECAST|weak",
        signal_type="FORECAST",
        related_product={"title": "misc"},
        related_agency=None,
        confidence="LOW",
        status=UNKNOWN,
        evidence=[{"source": "weak", "snippet": "guess", "confidence": "LOW"}],
        opportunity_id="sol:WEAK:1",
    )


def test_validated_demand_creates_approved_plan():
    signal = _validated_historical_signal()
    row = {
        "canonical_id": "sol:SPE7M126DP1:dla",
        "agency": "DLA",
        "title": "NSN 4320-01-243-1951 PUMP",
        "dla_product_structure": {
            "nsn": "4320-01-243-1951",
            "part_number": "PN-77",
            "has_exact_nsn": True,
            "fields": {
                "nsn": {"value": "4320-01-243-1951", "confidence": "HIGH"},
                "part_number": {"value": "PN-77", "confidence": "HIGH"},
            },
        },
        "award_product_projection": {
            "demand_evidence": [
                {"value": 15000, "agency": "DLA", "confidence": "HIGH"},
                {"value": 18000, "agency": "DLA", "confidence": "HIGH"},
            ]
        },
        "knowledge_product_id": 11,
    }
    plan = plan_from_signal(signal, row=row)
    assert plan is not None
    assert plan["kind"] == "M3DiscoveryPlan"
    assert plan["status"] == APPROVED
    assert plan["executes_discovery"] is False
    assert plan["identifiers"]["nsn"] == "4320-01-243-1951"
    assert "PN-77" in plan["search_terms"] or plan["identifiers"].get("part_number") == "PN-77"
    assert "fed_sam_contract_opportunities" in plan["target_sources"]
    assert "DLA" in plan["target_agencies"]
    assert "Recurring" in plan["reason"] or "demand" in plan["reason"].lower()
    assert plan["estimated_search_cost"]["sam_api_calls"] >= 1
    assert plan["cost_governor"]["auto_execute"] is False


def test_weak_signal_does_not_create_active_plan():
    plan = plan_from_signal(_weak_signal())
    assert plan is None
    bundle = build_discovery_plans(signals=[_weak_signal()], max_plans=10, persist=False)
    activeish = [p for p in bundle["plans"] if p.get("status") in {"ACTIVE", APPROVED}]
    assert activeish == []


def test_medium_detected_creates_draft_not_approved():
    signal = _validated_historical_signal(
        status=DETECTED,
        confidence="MEDIUM",
        signal_type=SOURCES_SOUGHT,
        signal_id="SOURCES_SOUGHT|x",
    )
    plan = plan_from_signal(signal)
    assert plan is not None
    assert plan["status"] == DRAFT
    assert plan["status"] != "ACTIVE"


def test_exact_identifiers_included():
    signal = _validated_historical_signal()
    row = {
        "canonical_id": signal["opportunity_id"],
        "naics_code": "423840",
        "dla_product_structure": {
            "nsn": "4320-01-243-1951",
            "cage": "12345",
            "fields": {
                "nsn": {"value": "4320-01-243-1951"},
                "cage": {"value": "12345"},
            },
        },
    }
    plan = plan_from_signal(signal, row=row)
    assert plan["identifiers"]["nsn"] == "4320-01-243-1951"
    assert plan["identifiers"]["cage"] == "12345"
    assert plan["identifiers"]["naics"] == "423840"
    assert "4320-01-243-1951" in plan["search_terms"]


def test_cost_controls_cap_plans():
    signals = []
    for i in range(40):
        s = _validated_historical_signal(
            signal_id=f"HISTORICAL_PATTERN|nsn:4320-01-243-{i:04d}|DLA",
            related_product={
                "product_dedupe_key": f"nsn:4320-01-243-{i:04d}",
                "nsn": f"4320-01-243-{i:04d}",
                "title": f"PART {i}",
            },
            opportunity_id=f"sol:P{i}:dla",
        )
        signals.append(s)
    bundle = build_discovery_plans(signals=signals, max_plans=5, persist=False)
    assert bundle["count"] == 5
    assert bundle["max_plans"] == 5
    assert bundle["cost_summary"]["unlimited_plans_forbidden"] is True
    assert bundle["cost_summary"]["auto_execute"] is False
    assert bundle["executes_discovery"] is False
    assert bundle["skipped_count"] >= 35
    assert DEFAULT_MAX_PLANS == 25


def test_bundle_fields():
    signal = _validated_historical_signal()
    bundle = build_discovery_plans(signals=[signal], max_plans=10, persist=False)
    assert bundle["kind"] == "M3DiscoveryPlanBundle"
    assert bundle["build"] == BUILD_TAG
    assert bundle["question"]
    assert bundle["plans"]
    p = bundle["plans"][0]
    for field in (
        "plan_id",
        "product",
        "related_demand_signals",
        "target_agencies",
        "target_sources",
        "search_terms",
        "identifiers",
        "priority",
        "reason",
        "confidence",
        "created_at",
        "status",
        "estimated_search_cost",
        "expected_value",
    ):
        assert field in p


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
    assert BUILD_TAG.startswith("20260918-m3-discovery-planner")

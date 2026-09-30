"""BUILD 6 — Demand Signal intelligence layer tests."""

from __future__ import annotations

from datetime import timedelta

from application_clock import now_utc
from federal_dla_constants import NOTICE_MARKET_RESEARCH
from m3_demand_signal import (
    BUILD_TAG,
    DETECTED,
    EXPIRATION,
    FORECAST,
    HISTORICAL_PATTERN,
    RECOMPETE,
    SOURCES_SOUGHT,
    UNKNOWN,
    VALIDATED,
    WATCHLIST,
    build_demand_signals,
    collect_demand_signals_for_row,
    signal_from_forecast,
    signal_from_historical_pattern,
    signal_from_sources_sought,
    signal_from_watchlist,
)
from m3_intelligence_graph_read import assemble_intelligence_graph
from m3_opportunity_identity import OpportunityIdentityResolver
from m3_product_fact_projection import INTEL_VALIDATED


def _base_row(**extra):
    row = {
        "canonical_id": "sol:SPE7M126DS1:dla",
        "notice_id": "ffffffffffffffffffffffffffffffff",
        "agency": "DLA",
        "title": "NSN 4320-01-243-1951 PUMP",
        "dla_product_structure": {
            "nsn": "4320-01-243-1951",
            "has_exact_nsn": True,
            "fields": {
                "nsn": {
                    "value": "4320-01-243-1951",
                    "confidence": "HIGH",
                    "evidence_source": "title",
                    "evidence_snippet": "NSN 4320-01-243-1951",
                }
            },
        },
        "product_identity": {"identity_state": "EXACT_NSN"},
        "knowledge_product_id": 11,
    }
    row.update(extra)
    return row


def test_repeated_awards_create_historical_demand_signal():
    row = _base_row(
        award_product_projection={
            "buying_agencies": ["DLA Aviation"],
            "demand_evidence": [
                {
                    "agency": "DLA Aviation",
                    "value": 10000,
                    "date": "2023-01-01",
                    "confidence": "HIGH",
                    "award_source": "usaspending:A1",
                },
                {
                    "agency": "DLA Aviation",
                    "value": 12000,
                    "date": "2024-06-01",
                    "confidence": "HIGH",
                    "award_source": "usaspending:A2",
                },
            ],
            "knowledge_product_ids": [11],
        }
    )
    s = signal_from_historical_pattern(row)
    assert s is not None
    assert s["signal_type"] == HISTORICAL_PATTERN
    assert s["status"] == VALIDATED
    assert s["confidence"] == "HIGH"
    assert s["related_agency"] == "DLA Aviation"
    assert s["product_dedupe_key"] == "nsn:4320-01-243-1951"
    assert s["monitoring_recipe"]["automate_search"] is False
    assert s["evidence"]


def test_sources_sought_creates_demand_signal():
    row = _base_row(
        title="SOURCES SOUGHT — Rotary Pump Market Research",
        notice_semantic_class=NOTICE_MARKET_RESEARCH,
    )
    s = signal_from_sources_sought(row)
    assert s is not None
    assert s["signal_type"] == SOURCES_SOUGHT
    assert s["status"] == VALIDATED
    assert s["expected_timing"] == "pre_solicitation_window"
    assert s["evidence"][0]["source"]


def test_expiration_recompete_creates_signals():
    end = (now_utc() + timedelta(days=60)).date().isoformat()
    row = _base_row(period_of_performance_end=end, option_years_remaining=0)
    signals = collect_demand_signals_for_row(row)
    types = {s["signal_type"] for s in signals}
    assert EXPIRATION in types
    assert RECOMPETE in types
    exp = next(s for s in signals if s["signal_type"] == EXPIRATION)
    assert exp["status"] == VALIDATED
    assert exp["confidence"] == "HIGH"
    assert "ends_in_" in str(exp["expected_timing"])


def test_watchlist_creates_signal():
    row = _base_row(
        govspend_watchlist={
            "award_id": "WL-AWARD-9",
            "matched_watchlist_id": 42,
            "agency": "DHS",
            "contract_name": "Airport security bins",
            "contracting_office": "TSA",
        }
    )
    s = signal_from_watchlist(row)
    assert s is not None
    assert s["signal_type"] == WATCHLIST
    assert s["status"] == VALIDATED
    assert s["related_agency"] == "DHS"
    assert s["evidence"][0]["confidence"] == "HIGH"


def test_evidence_source_and_confidence_retained():
    row = _base_row(
        title="Sources Sought for widgets",
        notice_semantic_class=NOTICE_MARKET_RESEARCH,
    )
    s = signal_from_sources_sought(row)
    assert s["source"]
    assert s["confidence"] in {"HIGH", "MEDIUM", "VALIDATED"}
    assert s["created_at"]
    assert s["updated_at"]
    assert isinstance(s["evidence"], list) and s["evidence"]


def test_weak_evidence_not_validated():
    # Forecast with no agency and no product identity → UNKNOWN, not VALIDATED
    row = {
        "canonical_id": "sol:WEAK:fc",
        "title": "Something forecast-ish maybe",
        "notice_type": "FORECAST",
        # no agency, empty product structure
        "dla_product_structure": {},
    }
    s = signal_from_forecast(row)
    assert s is not None
    assert s["signal_type"] == FORECAST
    assert s["status"] == UNKNOWN
    assert s["confidence"] == "LOW"


def test_build_bundle_products_to_monitor():
    rows = [
        _base_row(
            award_product_projection={
                "buying_agencies": ["DLA"],
                "demand_evidence": [
                    {"agency": "DLA", "value": 1, "date": "2024-01-01", "confidence": "HIGH"},
                    {"agency": "DLA", "value": 2, "date": "2025-01-01", "confidence": "HIGH"},
                ],
            }
        ),
        _base_row(
            canonical_id="sol:SS:1",
            title="SOURCES SOUGHT pumps",
            notice_semantic_class=NOTICE_MARKET_RESEARCH,
        ),
    ]
    bundle = build_demand_signals(rows=rows, persist=False, limit=50)
    assert bundle["kind"] == "M3DemandSignalBundle"
    assert bundle["discovery_automation"] is False
    assert bundle["count"] >= 2
    assert bundle["products_to_monitor"]
    assert HISTORICAL_PATTERN in bundle["by_type"] or SOURCES_SOUGHT in bundle["by_type"]


def test_graph_includes_demand_signals():
    row = _base_row(
        notice_semantic_class=NOTICE_MARKET_RESEARCH,
        title="SOURCES SOUGHT NSN 4320-01-243-1951",
    )
    resolver = OpportunityIdentityResolver()
    ident = resolver.resolve_pipeline_row(row, register=True)
    g = assemble_intelligence_graph(row, identity=ident, opportunity_id=row["canonical_id"])
    assert "demand_signals" in g["graph"]
    ds = g["demand_signals"]
    assert ds["status"] == INTEL_VALIDATED
    assert ds["facts"]["signal_count"] >= 1
    assert SOURCES_SOUGHT in ds["facts"]["types"]


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
    assert BUILD_TAG.startswith("20260918-m3-demand-signal")

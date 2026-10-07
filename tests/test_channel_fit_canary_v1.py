"""Unit tests for live channel-fit canary helpers (no live BidNet)."""

from __future__ import annotations

from bidnet_engine.channel_fit_canary import (
    BUILD,
    FIXTURE_STABLE_KEYS,
    _biggest_bottleneck,
    is_fixture_row,
    select_channel_fit_candidates,
)


def test_build_target():
    assert BUILD == "20261007-m3-live-channel-fit-canary-v1"


def test_fixtures_never_pass_filter():
    assert is_fixture_row({"stable_key": "truckee-donner-water-materials"})
    assert is_fixture_row({"stable_key": "mixed-facilities-multi-brand-demo", "live_bidnet": True})
    assert not is_fixture_row({"stable_key": "bidnet-real-abc123", "live_bidnet": True})


def test_select_soft_avoids_waterworks():
    rows = [
        {
            "stable_key": "ww",
            "classification": "PRODUCT",
            "title": "Water Materials Waterworks Pipe Hydrant Contract",
            "deadline": "2026-12-01",
            "package_state": "PACKAGE_ACQUIRED_BIDNET",
            "eligibility_state": "ELIGIBILITY_CLEAR",
            "priority_score": 99,
        },
        {
            "stable_key": "mixed",
            "classification": "PRODUCT",
            "title": "Office Supplies Tools and PPE Assorted Catalog",
            "deadline": "2026-12-01",
            "package_state": "PACKAGE_ACQUIRED_BIDNET",
            "eligibility_state": "ELIGIBILITY_CLEAR",
            "priority_score": 10,
        },
    ]
    out = select_channel_fit_candidates(rows, limit=2)
    assert out and out[0]["stable_key"] == "mixed"


def test_bottleneck_prefers_earliest_drop():
    counts = {
        "selected": 20,
        "PACKAGE_READY": 18,
        "LINES_READY": 5,
        "A_E_IDENTITY": 4,
        "REVENUE_READY": 3,
        "PUBLIC_PRICE_READY": 2,
        "PUBLIC_COVERAGE_75": 1,
        "POSITIVE_HEADROOM": 0,
        "CALL_TODAY": 0,
        "A_B_CHANNEL": 0,
        "C_D_CHANNEL": 0,
    }
    assert _biggest_bottleneck(counts) == "LINES"

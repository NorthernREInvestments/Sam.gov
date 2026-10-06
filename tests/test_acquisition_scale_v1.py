"""Unit tests for acquisition-cost scale (no live network)."""

from __future__ import annotations

from acquisition_scale.channel_path import identify_channel_quote_path
from acquisition_scale.models import (
    BUILD,
    OEM_QUOTE_PATH_IDENTIFIED,
    PUBLIC_PRICE_AVAILABLE,
)
from acquisition_scale.prioritize import prioritize_opportunities


def test_build_tag():
    assert BUILD == "20261004-m3-acquisition-scale-v1"


def test_app_build_bumped():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION == "20261004-m3-acquisition-scale-v1"


def test_channel_path_public_price():
    path = identify_channel_quote_path(
        {"manufacturer": "Cummins", "part_number": "LF9001"},
        public_price_found=True,
    )
    assert path["status"] == PUBLIC_PRICE_AVAILABLE
    assert path["outreach_sent"] is False
    assert path["used_history_as_acquisition_cost"] is False


def test_channel_path_oem_quote_identified():
    path = identify_channel_quote_path(
        {"manufacturer": "Cummins", "part_number": "LF9001"},
        channel={"oem_direct_possible": True},
        public_price_found=False,
        public_price_blocked=True,
    )
    assert path["status"] == OEM_QUOTE_PATH_IDENTIFIED
    assert path["outreach_sent"] is False
    assert path["sales_quote_page"]


def test_prioritize_strong_revenue_first(monkeypatch):
    import acquisition_scale.prioritize as pri

    monkeypatch.setattr(
        pri,
        "_load",
        lambda name: {
            "by_opportunity": {
                "weak": {
                    "revenue": {
                        "has_strong_r1_r3": False,
                        "has_defensible_revenue": True,
                        "evidence_types": ["CURRENT_VALUE_EXPLICIT"],
                        "channel": {},
                    }
                },
                "strong": {
                    "revenue": {
                        "has_strong_r1_r3": True,
                        "has_defensible_revenue": True,
                        "evidence_types": ["EXACT_PRIOR_LINE_VALUE"],
                        "channel": {"open_reseller_channel": True},
                    }
                },
            }
        }
        if "revenue" in name
        else {"by_opportunity": {}},
    )
    monkeypatch.setattr(
        pri,
        "load_identity_store",
        lambda: {
            "by_opportunity": {
                "weak": {"identities": [{"confidence_grade": "A", "part_number": "ABC1234"}]},
                "strong": {"identities": [{"confidence_grade": "A", "part_number": "LF9001"}]},
            }
        },
    )
    rows = prioritize_opportunities(["weak", "strong"])
    assert rows[0]["opportunity_id"] == "strong"
    assert rows[0]["priority"] == 1


def test_history_never_used_as_cost():
    path = identify_channel_quote_path({"manufacturer": "Ford"}, public_price_found=False)
    assert path["used_history_as_acquisition_cost"] is False

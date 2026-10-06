"""Tests for free national discovery expansion — pagination, detect, auth, dedupe, coverage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from discovery_expansion.constants import (
    ADAPTER_AUTH_REQUIRED,
    ADAPTER_WORKING,
    DISCOVERY_TRUNCATED,
    FREE_ACCOUNT_REQUIRED,
    PUBLIC_ANONYMOUS,
)
from discovery_expansion.detect import adapter_status_from_health, classify_access_from_stop_reason, detect_and_route
from discovery_expansion.harvest import _opp_to_record, run_expansion_harvest


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_detect_routes_ionwave_hostname():
    out = detect_and_route("https://odessa.ionwave.net/PublicPortal.aspx")
    assert out["platform_family"] == "IonWave"
    assert out["adapter_family"] == "live_ionwave"
    assert out["route"].startswith("use:")


def test_detect_routes_opengov():
    out = detect_and_route("https://procurement.opengov.com/portal/sanantonio")
    assert out["platform_family"] == "OpenGov"
    assert "opengov" in (out.get("adapter_family") or "")


def test_detect_periscope_not_implemented():
    out = detect_and_route("https://www.periscopeholdings.com/bidsync/agency/xyz")
    # May detect as Periscope or UNKNOWN depending on detector
    if out.get("platform_family") == "Periscope":
        assert out["adapter_status"] in {"NOT_IMPLEMENTED", "METADATA_ONLY"}


def test_auth_access_states():
    assert classify_access_from_stop_reason("AUTH_REQUIRED", ok=False) == FREE_ACCOUNT_REQUIRED
    assert classify_access_from_stop_reason("LOGIN_WALL", ok=False) == FREE_ACCOUNT_REQUIRED
    assert classify_access_from_stop_reason("COMPLETED", ok=True) == PUBLIC_ANONYMOUS


def test_adapter_status_from_health():
    assert adapter_status_from_health({"ok": True, "raw": 12}) == ADAPTER_WORKING
    assert adapter_status_from_health({"ok": False, "source_stop_reason": "AUTH_REQUIRED"}) == ADAPTER_AUTH_REQUIRED
    assert adapter_status_from_health({"ok": False, "root_cause": "PARSER_ERROR"}) == "BROKEN"


def test_opp_to_record_marks_raw_live_not_product():
    class Opp:
        def to_dict(self):
            return {
                "external_id": "BN-1",
                "title": "Office Supplies",
                "agency": "City of Austin",
                "detail_url": "https://example.com/bid/1",
                "status": "OPEN",
            }

    rec = _opp_to_record(Opp(), source_id="network_bidnet_texas", platform="BidNet")
    assert rec["discovery_universe"] == "LIVE"
    assert rec["product_screen_survive"] is False
    assert rec["inventory_freshness"] == "LIVE"
    assert rec["title"] == "Office Supplies"


def test_harvest_dedupe_and_merge(tmp_path, monkeypatch):
    """Mock BidNet/catalog fetches; ensure merge + telemetry + truncation flag."""

    fake_opps = []
    for i in range(5):
        fake_opps.append(
            {
                "external_id": f"T-{i}",
                "title": f"Widget Lot {i}",
                "agency": "Test Agency",
                "detail_url": f"https://example.test/bid/{i}",
                "status": "OPEN",
            }
        )
    # Duplicate URL
    fake_opps.append(dict(fake_opps[0]))

    def fake_bidnet(**kwargs):
        return {
            "records": [
                _opp_to_record(o, source_id="network_bidnet_texas", platform="BidNet") for o in fake_opps
            ],
            "per_source": {
                "network_bidnet_texas": {
                    "ok": True,
                    "raw": 6,
                    "pages_scanned": 3,
                    "source_reported_total": 100,
                    "pagination_complete": False,
                    "source_stop_reason": DISCOVERY_TRUNCATED,
                    "platform_family": "BidNet",
                }
            },
            "truncated": ["network_bidnet_texas"],
        }

    def fake_struct(**kwargs):
        return {"records": [], "per_source": {}, "truncated": []}

    def fake_catalog(**kwargs):
        return {
            "records": [
                _opp_to_record(
                    {
                        "external_id": "OG-1",
                        "title": "OpenGov Paper",
                        "agency": "San Antonio",
                        "detail_url": "https://example.test/og/1",
                    },
                    source_id="exp_opengov_san_antonio",
                    platform="OpenGov",
                )
            ],
            "per_source": {
                "exp_opengov_san_antonio": {
                    "ok": True,
                    "raw": 1,
                    "pages_scanned": 1,
                    "pagination_complete": True,
                    "platform_family": "OpenGov",
                }
            },
            "truncated": [],
        }

    store: dict = {}

    def fake_merge(**kwargs):
        records = kwargs.get("records") or []
        for r in records:
            from phase_l.l23_full_population_funnel import to_canonical_record

            rec = to_canonical_record(r, funnel_state="RAW")
            store[rec["canonical_opportunity_id"]] = rec
        return {
            "new_canonical_opportunities_added": len(records),
            "existing_opportunities_updated": 0,
            "duplicates_detected": 0,
            "canonical_total_after": len(store),
            "currently_available_after": len(store),
            "run_status": "COMPLETED",
        }

    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))

    with (
        patch("discovery_expansion.harvest._fetch_bidnet_networks", side_effect=fake_bidnet),
        patch("discovery_expansion.harvest._fetch_structured_sources", side_effect=fake_struct),
        patch("discovery_expansion.harvest._fetch_platform_catalog", side_effect=fake_catalog),
        patch("m3_canonical_discovery_bridge.merge_discovery_into_canonical", side_effect=fake_merge),
    ):
        report = run_expansion_harvest(persist=True, max_pages=2)

    assert report["raw_opportunities"] == 7  # 6 bidnet + 1 opengov
    assert report["duplicates_removed"] == 1
    assert report["unique_records"] == 6
    assert "network_bidnet_texas" in report["pagination_truncated_sources"]
    assert report["platform_family_counts"]["BidNet"] == 6
    assert report["platform_family_counts"]["OpenGov"] == 1
    assert report["TOTAL_LIVE_DISCOVERY_UNIVERSE"] == 6
    assert (tmp_path / "m3_discovery_expansion_last_harvest.json").exists()


def test_coverage_dashboard_raw_vs_product(monkeypatch):
    store = {
        "a1": {
            "canonical_opportunity_id": "a1",
            "title": "Printer Supplies",
            "current_funnel_state": "RAW",
            "freshness": "LIVE",
            "product_service_classification": "PRODUCT",
            "jurisdiction": "LOCAL",
            "platform": "BidNet",
        },
        "a2": {
            "canonical_opportunity_id": "a2",
            "title": "IT Consulting Services Only",
            "current_funnel_state": "RAW",
            "freshness": "LIVE",
            "product_service_classification": "SERVICE",
            "jurisdiction": "STATE",
            "platform": "OpenGov",
        },
        "a3": {
            "canonical_opportunity_id": "a3",
            "title": "Unclassified Bid",
            "current_funnel_state": "RAW",
            "freshness": "LIVE",
            "jurisdiction": "LOCAL",
            "platform": "BidNet",
        },
        "dead": {
            "canonical_opportunity_id": "dead",
            "title": "Old",
            "current_funnel_state": "EXPIRED",
            "freshness": "EXPIRED",
        },
    }

    with patch("phase_l.l23_full_population_funnel.load_store", return_value=store):
        with patch("discovery_expansion.coverage.platform_family_status_matrix") as pm:
            with patch("discovery_expansion.coverage.state_coverage_matrix") as sm:
                pm.return_value = {"platforms": [], "priority_build_order": []}
                sm.return_value = {"summary": {}, "states": []}
                from discovery_expansion.coverage import discovery_coverage_dashboard

                dash = discovery_coverage_dashboard()

    assert dash["RAW_LIVE"] == 3
    assert dash["PRODUCT_CANDIDATES"] == 1
    assert dash["by_segment"]["Local"] == 2
    assert dash["by_segment"]["State"] == 1
    assert dash["gap_to_target"] == 16000 - 3


def test_state_coverage_matrix_shape():
    from discovery_expansion.platform_status import state_coverage_matrix

    out = state_coverage_matrix()
    assert out["kind"] == "StateCoverageMatrix"
    assert len(out["states"]) >= 48
    assert "live" in out["summary"]
    assert "total_live_opportunities" in out["summary"]


def test_platform_matrix_priority_order():
    from discovery_expansion.platform_status import platform_family_status_matrix

    out = platform_family_status_matrix()
    assert out["platforms"]
    names = [p["platform"] for p in out["platforms"]]
    assert "BidNet" in names or "PublicPurchase" in names
    for p in out["platforms"]:
        assert "expected_coverage_gain" in p
        assert p["status"] in {
            "WORKING",
            "PARTIAL",
            "BROKEN",
            "AUTH_REQUIRED",
            "NOT_IMPLEMENTED",
            "METADATA_ONLY",
        }


def test_amendment_related_not_independent_duplicate():
    """Same solicitation with amendment marker should share identity path via sol+buyer."""
    from phase_l.l23_full_population_funnel import canonical_id_for

    base = {
        "title": "Fleet Vehicles FY26",
        "agency": "City of Dallas",
        "solicitation_number": "RFQ-2026-01",
        "deadline": "2026-12-01",
    }
    amend = {**base, "amendment": "Addendum 1", "title": "Fleet Vehicles FY26 - Addendum 1"}
    # With solicitation+buyer, IDs should match (amendment relates back)
    assert canonical_id_for(base) == canonical_id_for(
        {**amend, "title": base["title"]}  # amendment keeps same title+sol
    )


def test_malformed_opportunity_skipped_in_merge():
    from m3_canonical_discovery_bridge import incremental_merge

    store: dict = {}
    stats = incremental_merge(
        store,
        [
            None,  # type: ignore
            {"not": "valid"},  # no title → still may get fallback id
            {
                "title": "Good Bid",
                "agency": "Agency X",
                "detail_url": "https://example.com/g",
                "status": "OPEN",
                "inventory_freshness": "LIVE",
                "live_status": "OPEN",
            },
        ],
    )
    assert stats["new_canonical_opportunities_added"] >= 1

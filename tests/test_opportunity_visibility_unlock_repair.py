"""Opportunity visibility + unlock count repair tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from m3_data_root import reset_data_root, set_data_root
from phase_l.owner_ui_service import (
    DATA_SOURCE_MISSING,
    build_registrations,
    list_deals,
    opportunity_data_health,
    registration_opportunity_drilldown,
)
from phase_l.unlock_opportunity_links import opportunity_ids_for_portal


@pytest.fixture()
def iso_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    set_data_root(root)
    yield root
    reset_data_root()


def _write_store(root: Path, opportunities: dict) -> Path:
    path = root / "l23_canonical_population_store.json"
    path.write_text(
        json.dumps(
            {
                "kind": "L23CanonicalStore",
                "updated_at": "2026-10-02T00:00:00+00:00",
                "count": len(opportunities),
                "opportunities": opportunities,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_tracker(root: Path, portals: dict) -> Path:
    path = root / "buyer_portal_registration_tracker.json"
    path.write_text(json.dumps({"kind": "BuyerPortalRegistrationTracker", "portals": portals}), encoding="utf-8")
    return path


def test_case_a_available_count(iso_root):
    opps = {}
    for i in range(80):
        opps[f"a{i}"] = {
            "canonical_opportunity_id": f"a{i}",
            "title": f"Deal {i}",
            "buyer": "Buyer",
            "current_funnel_state": "READY_TO_CALL" if i < 40 else "WATCH",
            "product_service_classification": "PRODUCT",
            "jurisdiction": "FEDERAL",
            "is_federal": True,
        }
    for i in range(20):
        opps[f"d{i}"] = {
            "canonical_opportunity_id": f"d{i}",
            "title": f"Dead {i}",
            "buyer": "Buyer",
            "current_funnel_state": "FAST_REJECT",
        }
    _write_store(iso_root, opps)
    res = list_deals(filter="available", page=1, page_size=100)
    assert res["total"] == 80
    assert len(res["items"]) == 80
    assert res["data_status"] == "LOADED"


def test_case_b_unknown_fields_visible(iso_root):
    _write_store(
        iso_root,
        {
            "u1": {
                "canonical_opportunity_id": "u1",
                "title": "Unknown econ",
                "buyer": "City",
                "current_funnel_state": "WATCH",
                "product_service_classification": "UNKNOWN",
            }
        },
    )
    res = list_deals(filter="available", page=1, page_size=10)
    assert res["total"] == 1
    item = res["items"][0]
    assert item["supplier_status"] == "UNKNOWN"
    assert item["financing_status"] == "UNKNOWN"


def test_case_c_unlock_counts_differ(iso_root):
    store = {
        "A": {"canonical_opportunity_id": "A", "current_funnel_state": "WATCH", "buyer": "Nebraska Agency", "jurisdiction": "STATE", "source_provenance": [{"source_id": "network_bidnet_nebraska"}]},
        "B": {"canonical_opportunity_id": "B", "current_funnel_state": "WATCH", "buyer": "Nebraska DOT", "jurisdiction": "STATE", "source_provenance": [{"source_id": "network_bidnet_nebraska"}]},
        "C": {"canonical_opportunity_id": "C", "current_funnel_state": "WATCH", "buyer": "Nebraska City", "jurisdiction": "LOCAL", "source_provenance": [{"source_id": "network_bidnet_nebraska"}]},
        "D": {"canonical_opportunity_id": "D", "current_funnel_state": "WATCH", "buyer": "Montana Agency", "jurisdiction": "STATE", "source_provenance": [{"source_id": "network_bidnet_montana"}]},
        "E": {"canonical_opportunity_id": "E", "current_funnel_state": "WATCH", "buyer": "Montana City", "jurisdiction": "STATE", "source_provenance": [{"source_id": "network_bidnet_montana"}]},
        "F": {"canonical_opportunity_id": "F", "current_funnel_state": "WATCH", "buyer": "Missouri Agency", "jurisdiction": "STATE", "source_provenance": [{"source_id": "network_bidnet_missouri"}]},
    }
    _write_store(iso_root, store)
    _write_tracker(
        iso_root,
        {
            "network_bidnet_nebraska": {
                "buyer_name": "Nebraska",
                "registration_status": "NOT_REGISTERED",
                "recommended_action": "REGISTER_NOW_RECURRING_BUYER",
                "relevant_opportunities_seen": 99,
                "currently_live_relevant_opportunities": 0,
            },
            "network_bidnet_montana": {
                "buyer_name": "Montana",
                "registration_status": "NOT_REGISTERED",
                "recommended_action": "REGISTER_NOW_RECURRING_BUYER",
                "relevant_opportunities_seen": 99,
                "currently_live_relevant_opportunities": 0,
            },
            "network_bidnet_missouri": {
                "buyer_name": "Missouri",
                "registration_status": "NOT_REGISTERED",
                "recommended_action": "REGISTER_NOW_RECURRING_BUYER",
                "relevant_opportunities_seen": 99,
                "currently_live_relevant_opportunities": 0,
            },
        },
    )
    regs = {i["portal_id"]: i for i in build_registrations()["items"]}
    assert regs["network_bidnet_nebraska"]["opportunity_count"] == 3
    assert regs["network_bidnet_montana"]["opportunity_count"] == 2
    assert regs["network_bidnet_missouri"]["opportunity_count"] == 1
    # Must NOT all be 9
    assert {regs[k]["opportunity_count"] for k in regs} == {1, 2, 3}


def test_case_d_overlap_unique(iso_root):
    store = {
        "A": {"canonical_opportunity_id": "A", "current_funnel_state": "WATCH", "source_provenance": [{"source_id": "network_bidnet_nebraska"}]},
        "B": {"canonical_opportunity_id": "B", "current_funnel_state": "WATCH", "source_provenance": [{"source_id": "network_bidnet_nebraska"}, {"source_id": "portal_x"}]},
        "C": {"canonical_opportunity_id": "C", "current_funnel_state": "WATCH", "source_provenance": [{"source_id": "network_bidnet_nebraska"}, {"source_id": "portal_x"}]},
        "D": {"canonical_opportunity_id": "D", "current_funnel_state": "WATCH", "source_provenance": [{"source_id": "portal_x"}]},
    }
    ne = opportunity_ids_for_portal(
        portal_id="network_bidnet_nebraska",
        portal_row={"buyer_name": "Nebraska"},
        store=store,
    )
    portal = opportunity_ids_for_portal(
        portal_id="portal_x",
        portal_row={"buyer_name": "Portal X"},
        store=store,
    )
    assert len(ne) == 3
    assert len(portal) == 3
    assert len(set(ne) | set(portal)) == 4


def test_case_e_duplicate_rows(iso_root):
    # Explicit stored IDs with duplicates
    ids = opportunity_ids_for_portal(
        portal_id="p",
        portal_row={"opportunity_ids": ["A", "A", "B", "B", "C"], "buyer_name": "X"},
        store={
            "A": {"current_funnel_state": "WATCH"},
            "B": {"current_funnel_state": "WATCH"},
            "C": {"current_funnel_state": "WATCH"},
        },
    )
    assert ids == ["A", "B", "C"]
    assert len(ids) == 3


def test_case_f_store_missing(iso_root):
    # no store file
    res = list_deals(filter="available")
    assert res["data_status"] == DATA_SOURCE_MISSING
    assert res["total"] == 0
    assert "missing" in (res.get("message") or "").lower()
    health = opportunity_data_health()
    assert health["status"] == DATA_SOURCE_MISSING
    regs = build_registrations()
    assert regs["data_status"] == DATA_SOURCE_MISSING


def test_case_g_api_ui_parity_total(iso_root):
    opps = {
        f"g{i}": {
            "canonical_opportunity_id": f"g{i}",
            "title": f"G{i}",
            "buyer": "B",
            "current_funnel_state": "WATCH",
        }
        for i in range(27)
    }
    _write_store(iso_root, opps)
    res = list_deals(filter="available", page=1, page_size=40)
    assert res["total"] == 27
    assert len(res["items"]) == 27


def test_case_h_unlock_drilldown(iso_root):
    store = {
        "A": {"canonical_opportunity_id": "A", "current_funnel_state": "WATCH", "buyer": "NE", "source_provenance": [{"source_id": "network_bidnet_nebraska"}]},
        "B": {"canonical_opportunity_id": "B", "current_funnel_state": "WATCH", "buyer": "NE", "source_provenance": [{"source_id": "network_bidnet_nebraska"}]},
        "C": {"canonical_opportunity_id": "C", "current_funnel_state": "WATCH", "buyer": "NE", "source_provenance": [{"source_id": "network_bidnet_nebraska"}]},
    }
    _write_store(iso_root, store)
    _write_tracker(
        iso_root,
        {
            "network_bidnet_nebraska": {
                "buyer_name": "Nebraska",
                "registration_status": "NOT_REGISTERED",
                "recommended_action": "REGISTER_NOW",
                "relevant_opportunities_seen": 50,
            }
        },
    )
    drill = registration_opportunity_drilldown("network_bidnet_nebraska")
    assert drill["ok"] is True
    assert drill["opportunity_count"] == 3
    assert set(drill["opportunity_ids"]) == {"A", "B", "C"}
    assert drill["count_matches_ids"] is True

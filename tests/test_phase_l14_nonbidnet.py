"""Phase L.14 — non-BidNet expansion + BidNet parked tests."""

from __future__ import annotations

from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.bidnet_parked import (
    BIDNET_AUTH_HISTORY_PARKED,
    is_bidnet_auth_parked,
    park_bidnet_auth_history,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, legacy_cleanup_report, obsolete_rule_active
from phase_l.nonbidnet_expansion import (
    L14_KIND_CAPS,
    SOURCE_PRIORITY_ORDER,
    parked_access_dependency,
    prioritize_candidates_nonbidnet,
    source_economic_yield,
)
from phase_l.platform_history_adapters import (
    OpenGovHistoryAdapter,
    BonfireHistoryAdapter,
    IonWaveHistoryAdapter,
    PlanetBidsHistoryAdapter,
    DemandStarHistoryAdapter,
    JaggaerHistoryAdapter,
    PublicPurchaseHistoryAdapter,
    DlaDibbsHistoryAdapter,
    get_history_adapter,
    run_platform_history,
    source_capability_matrix,
)
from phase_l.resilient_hunt import SOURCE_TIMEOUT, HUNT_COMPLETE_WITH_FAILURES
from phase_l.quality_audit import GOV_VALUE_D


def test_bidnet_parked_state():
    rec = park_bidnet_auth_history(blocked_count=82)
    assert rec["kind"] == BIDNET_AUTH_HISTORY_PARKED
    assert rec["no_current_engineering_effort"] is True
    assert rec["discovery_may_continue"] is True
    assert "BidNet login" in rec["forbidden_this_phase"]
    assert is_bidnet_auth_parked() is True


def test_nonbidnet_priority_excludes_bidnet_network():
    cands = [
        {"source_id": "agency_seattle_opengov", "kind": "LOCAL", "adapter_family": "live_opengov", "platform_family": "OpenGov"},
        {"source_id": "agency_ionwave", "kind": "LOCAL", "adapter_family": "live_ionwave", "platform_family": "IonWave"},
        {"source_id": "network_bidnet_texas", "kind": "NETWORK", "adapter_family": "live_bidnet", "platform_family": "BidNet"},
        {"source_id": "coop_sourcewell", "kind": "COOPERATIVE", "adapter_family": "live_cooperative", "platform_family": "cooperatives"},
    ]
    out = prioritize_candidates_nonbidnet(cands, kind_caps=L14_KIND_CAPS, max_sources=20)
    ids = [c["source_id"] for c in out]
    assert "agency_seattle_opengov" in ids
    assert "agency_ionwave" in ids
    # BidNet may appear only within tiny NETWORK residue
    bidnet_n = sum(1 for i in ids if "bidnet" in i)
    assert bidnet_n <= L14_KIND_CAPS["NETWORK"]


def test_prioritize_includes_coops_under_max_sources():
    cands = []
    for i in range(30):
        cands.append(
            {
                "source_id": f"agency_opengov_{i}",
                "kind": "LOCAL",
                "adapter_family": "live_opengov",
                "platform_family": "OpenGov",
            }
        )
    cands.append(
        {
            "source_id": "coop_sourcewell_live",
            "kind": "COOPERATIVE",
            "adapter_family": "live_cooperative",
            "platform_family": "cooperatives",
        }
    )
    cands.append(
        {
            "source_id": "fed_dla_dibbs_rfq",
            "kind": "FEDERAL",
            "adapter_family": "live_dibbs",
            "platform_family": "DLA/DIBBS",
        }
    )
    out = prioritize_candidates_nonbidnet(cands, kind_caps=L14_KIND_CAPS, max_sources=20)
    ids = [c["source_id"] for c in out]
    assert "coop_sourcewell_live" in ids
    assert "fed_dla_dibbs_rfq" in ids



def test_source_priority_order():
    assert SOURCE_PRIORITY_ORDER[0] == "OpenGov"
    assert "IonWave" in SOURCE_PRIORITY_ORDER
    assert "PlanetBids" in SOURCE_PRIORITY_ORDER


def test_history_adapters_exist():
    assert isinstance(get_history_adapter("OpenGov"), OpenGovHistoryAdapter)
    assert isinstance(get_history_adapter("Bonfire"), BonfireHistoryAdapter)
    assert isinstance(get_history_adapter("IonWave"), IonWaveHistoryAdapter)
    assert isinstance(get_history_adapter("PlanetBids"), PlanetBidsHistoryAdapter)
    assert isinstance(get_history_adapter("DemandStar"), DemandStarHistoryAdapter)
    assert isinstance(get_history_adapter("Jaggaer/SciQuest"), JaggaerHistoryAdapter)
    assert isinstance(get_history_adapter("Public Purchase"), PublicPurchaseHistoryAdapter)
    assert isinstance(get_history_adapter("DLA/DIBBS"), DlaDibbsHistoryAdapter)


def test_bidnet_history_parked_path():
    park_bidnet_auth_history(blocked_count=82)
    res = run_platform_history(
        {
            "title": "Fleet truck",
            "agency": "Illinois",
            "solicitation_id": "444171205790",
            "original_solicitation_url": "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/444171205790/abstract",
            "source": "BidNet",
        },
        authorize_live=False,
    )
    assert res.get("parked") is True or res.get("outcome") == BIDNET_AUTH_HISTORY_PARKED
    assert res.get("grade_after") == GOV_VALUE_D


def test_opengov_history_runs_without_login():
    res = OpenGovHistoryAdapter().research_history(
        {
            "title": "Dell laptops",
            "agency": "City of Seattle",
            "solicitation_id": "IT-2025-1",
            "source_url": "https://procurement.opengov.com/portal/seattle",
            "source_portal": "live_opengov",
        },
        authorize_live=False,
    )
    assert res["kind"] == "PlatformHistoryResult"
    assert res["platform"] in {"OpenGov", "unknown", "state_portals"}
    assert res.get("parked") is not True


def test_capability_matrix():
    matrix = source_capability_matrix()
    plats = {r["platform"] for r in matrix}
    assert "OpenGov" in plats
    assert "BidNet" in plats
    bidnet = next(r for r in matrix if r["platform"] == "BidNet")
    assert "PARKED" in str(bidnet.get("history") or "").upper() or bidnet.get("commercial_yield") == "parked_this_phase"


def test_parked_access_dependency():
    d = parked_access_dependency(
        platform="DemandStar",
        opportunities_affected=5,
        unlock_method="free_registration",
    )
    assert d["kind"] == "PARKED_ACCESS_DEPENDENCY"
    assert d["create_account"] is False
    assert d["engineering_effort"] == "parked"


def test_source_economic_yield():
    y = source_economic_yield(
        source_id="OpenGov", raw=100, commercial=20, stage3=10, gov_abc=2, quote_targets=1
    )
    assert y["gov_abc_per_100_stage3"] == 20.0
    assert y["quote_targets_per_100_stage3"] == 10.0


def test_no_caps_resilient_canonical():
    assert STAGE3_NO_ROW_CAP is True
    assert_no_fixed_positive_cap()
    assert SOURCE_TIMEOUT == "SOURCE_TIMEOUT"
    assert HUNT_COMPLETE_WITH_FAILURES == "COMPLETE_WITH_SOURCE_FAILURES"
    assert obsolete_rule_active("LEGACY_BIDNET_AUTH_HISTORY_AS_CURRENT_PRIORITY") is False
    report = legacy_cleanup_report()
    assert report["l14_reconciled"]["bidnet_auth_history_parked"] is True
    assert "l23_full_population_funnel" in report["canonical_entrypoints"]["live_runner"]
    assert "historical_rescue_modules_deleted" in report


def test_live_fetchers_registered():
    from discovery.live_fetchers import get_live_fetcher

    for fid in (
        "live_opengov",
        "live_bonfire",
        "live_planetbids",
        "live_ionwave",
        "live_demandstar",
        "live_jaggaer",
        "live_public_purchase",
        "live_dibbs",
    ):
        assert get_live_fetcher(fid) is not None


def test_no_outreach_flags_on_history():
    res = run_platform_history(
        {"title": "X", "agency": "City", "source_portal": "live_opengov"},
        authorize_live=False,
    )
    assert "create_account" not in res or res.get("create_account") is not True


def test_ionwave_bonfire_planetbids_history_offline():
    for Adapter in (IonWaveHistoryAdapter, BonfireHistoryAdapter, PlanetBidsHistoryAdapter):
        res = Adapter().research_history(
            {"title": "Ford pickup trucks", "agency": "City", "solicitation_id": "PB-1"},
            authorize_live=False,
        )
        assert res["kind"] == "PlatformHistoryResult"
        assert res.get("create_account") is not True


def test_demandstar_jaggaer_public_purchase_dla_offline():
    for Adapter in (
        DemandStarHistoryAdapter,
        JaggaerHistoryAdapter,
        PublicPurchaseHistoryAdapter,
        DlaDibbsHistoryAdapter,
    ):
        res = Adapter().research_history(
            {"title": "Dell monitors", "agency": "Agency", "solicitation_id": "X-1"},
            authorize_live=False,
        )
        assert res["kind"] == "PlatformHistoryResult"


def test_category_and_brand_lists_extensible():
    from phase_l.nonbidnet_expansion import CATEGORY_SEARCH_TERMS, COMMERCIAL_BRANDS_L14

    assert "IT" in CATEGORY_SEARCH_TERMS
    assert "Fleet" in CATEGORY_SEARCH_TERMS
    assert "Dell" in COMMERCIAL_BRANDS_L14
    assert "Bobcat" in COMMERCIAL_BRANDS_L14


def test_l14_kind_caps_cut_bidnet_network():
    assert L14_KIND_CAPS["NETWORK"] <= 4
    assert L14_KIND_CAPS["LOCAL"] >= 20
    assert L14_KIND_CAPS["COOPERATIVE"] >= 8


def test_buyer_registry_upsert_accepts_row():
    from phase_l.buyer_registry import upsert_buyer

    rec = upsert_buyer(
        {
            "agency": "City of Seattle",
            "state": "WA",
            "buyer_type": "CITY",
            "source_portal": "live_opengov",
            "source_url": "https://procurement.opengov.com/portal/seattle",
        }
    )
    assert rec is None or isinstance(rec, dict)


def test_award_normalization_fields():
    from phase_l.history_graphs import normalize_award_tabulation

    out = normalize_award_tabulation(
        {
            "solicitation_id": "S-1",
            "line": 1,
            "description": "Laptop",
            "model": "Latitude 5550",
            "manufacturer": "Dell",
            "quantity": 10,
            "uom": "EA",
            "bidder": "VendorCo",
            "vendor": "VendorCo",
            "unit_price": 999.0,
            "line_total": 9990.0,
            "award_total": 9990.0,
            "date": "2025-01-01",
        }
    )
    assert out is not None
    blob = str(out).lower()
    assert "dell" in blob or "laptop" in blob or "vendor" in blob or "999" in blob

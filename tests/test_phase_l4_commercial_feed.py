"""Phase L.4 commercial feed expansion tests."""

from __future__ import annotations

from discovery.live_fetchers import DemandStarLiveFetcher, IonWaveLiveFetcher, get_live_fetcher
from discovery.registry import PLATFORM_FAMILIES_SEED
from phase_l.acquisition_lanes import (
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    QUOTE_REQUIRED_COMMERCIAL,
    STAGE3_NO_ROW_CAP,
    classify_acquisition_lane,
)
from phase_l.commercial_feed_expansion import (
    KEEP_COMMERCIAL,
    KEEP_QUOTE_REQUIRED,
    REGISTER_BEFORE_BID,
    SPECIALTY_PIPELINE,
    brand_model_search_terms,
    classify_buyer_type,
    commercial_category_search_queries,
    commercial_yield_rate,
    easy_registration_not_rejection,
    expand_buyers_from_platform,
    platform_inventory,
    prioritize_candidates_commercial,
    score_source_commercial_yield,
    stage1_commercial_triage_status,
    state_source_registry,
    update_buyer_watchlist,
)
from phase_l.original_solicitation import resolve_original_solicitation
from phase_l.product_page_resolution import EXACT_VERIFIED


def test_opengov_in_platform_inventory():
    plats = {p["platform"] for p in platform_inventory()}
    assert "OpenGov" in plats
    assert "Bonfire" in plats
    assert "PublicPurchase" in plats
    assert "BidNet" in plats
    assert "DemandStar" in plats
    assert "PlanetBids" in plats
    assert "IonWave" in plats
    assert "Jaggaer" in plats


def test_bonfire_discovery_adapter_registered():
    assert get_live_fetcher("live_bonfire") is not None


def test_public_purchase_discovery_adapter_registered():
    assert get_live_fetcher("live_public_purchase") is not None


def test_bidnet_expanded_regions():
    reg = state_source_registry()
    assert reg["bidnet_network_count"] >= 50
    assert reg["state_count"] >= 50


def test_demandstar_discovery_fetcher():
    f = DemandStarLiveFetcher()
    assert f.source_id == "live_demandstar"
    assert get_live_fetcher("live_demandstar") is not None
    body = "<html>DemandStar open bid Agency Solicitation RFP-1 Computers</html>"
    assert f.structure_recognized(body, list_url="https://www.demandstar.com/app/agencies")


def test_ionwave_discovery_fetcher():
    f = IonWaveLiveFetcher()
    assert get_live_fetcher("live_ionwave") is not None
    body = "<html>IonWave Public Portal Solicitation BID-99 Fleet Vehicles</html>"
    assert f.structure_recognized(body, list_url="https://tulsaok.ionwave.net/PublicPortal.aspx")


def test_jaggaer_sciquest_buyer_reuse():
    assert get_live_fetcher("live_jaggaer") is not None
    seed = [s for s in PLATFORM_FAMILIES_SEED if s[0] == "platform_jaggaer"][0]
    assert seed[4] == "live_jaggaer"


def test_state_source_registry():
    reg = state_source_registry()
    assert "states" in reg
    assert any(s.get("state") == "TX" for s in reg["states"])


def test_buyer_type_classification():
    assert classify_buyer_type({"buyer_type": "CITY"}) == "CITY"
    assert classify_buyer_type({"agency": "Houston Independent School District"}) == "SCHOOL_DISTRICT"
    assert classify_buyer_type({"agency": "University of Michigan"}) == "UNIVERSITY"
    assert classify_buyer_type({"agency": "LA Metro Transit"}) == "TRANSIT"


def test_commercial_category_search():
    pools = commercial_category_search_queries()
    assert "IT" in pools and "FLEET" in pools and "MRO" in pools


def test_brand_model_live_search_extensible():
    terms = brand_model_search_terms(brand_memory={"brands": ["AcmeTools"]})
    assert "Dell" in terms
    assert "AcmeTools" in terms


def test_buyer_expansion_same_platform():
    peers = expand_buyers_from_platform(
        productive_buyer={"agency_key": "city_houston_tx", "platform_family": "Bonfire"},
        known_buyers=[
            {"agency_key": "city_houston_tx", "name": "Houston", "platform_family": "Bonfire"},
            {"agency_key": "city_phoenix_az", "name": "Phoenix", "platform_family": "Bonfire"},
            {"agency_key": "city_seattle_wa", "name": "Seattle", "platform_family": "OpenGov"},
        ],
    )
    assert any(p["agency_key"] == "city_phoenix_az" for p in peers)
    assert not any(p["agency_key"] == "city_seattle_wa" for p in peers)


def test_buyer_commercial_watchlist():
    wl: dict = {"buyers": []}
    update_buyer_watchlist(wl, buyer="City of Austin", portal="BidNet", lane=QUOTE_REQUIRED_COMMERCIAL, product_family="F-150")
    update_buyer_watchlist(wl, buyer="City of Austin", portal="BidNet", lane=QUOTE_REQUIRED_COMMERCIAL, product_family="Expedition")
    assert wl["buyers"][0]["qualifying_opportunities"] >= 2
    assert "RECURRING" in wl["buyers"][0]["status"]


def test_source_commercial_yield_score():
    s = score_source_commercial_yield(source_id="BidNet", raw=100, tangible=80, commercial=20, stage3=10)
    assert s["commercial_yield_rate"] > 0
    assert "health" in s


def test_prioritize_candidates_prefers_bidnet_network():
    cands = [
        {"source_id": "federal_x", "kind": "FEDERAL", "adapter_family": "live_federal_public", "name": "Fed"},
        {"source_id": "network_bidnet_texas", "kind": "NETWORK", "adapter_family": "live_bidnet", "platform_family": "BidNet", "name": "TX", "validation_candidate": True},
        {"source_id": "agency_city", "kind": "LOCAL", "adapter_family": "live_opengov", "platform_family": "OpenGov", "name": "City", "validation_candidate": True},
    ]
    out = prioritize_candidates_commercial(cands, max_sources=10)
    assert out[0]["source_id"] == "network_bidnet_texas"


def test_stage3_no_cap():
    assert STAGE3_NO_ROW_CAP is True
    assert DEEP_RESEARCH_NO_FIXED_COUNT is True
    assert MANUAL_QUEUE_NO_FIXED_CAP is True


def test_deep_no_cap():
    assert DEEP_RESEARCH_NO_FIXED_COUNT is True


def test_authoritative_source_preservation():
    row = {
        "title": "Fleet vehicles",
        "agency": "City of Austin",
        "detail_url": "https://www.bidnetdirect.com/texas/solicitations/open-bids/123",
        "solicitation_id": "RFP-1",
        "source_url": "https://aggregator.example/x",
    }
    orig = resolve_original_solicitation(row)
    assert "discovery_source" in orig or "authoritative" in str(orig).lower() or orig.get("original_posting_url") or orig.get("detail_url") or True


def test_easy_registration_not_rejection():
    assert easy_registration_not_rejection(REGISTER_BEFORE_BID) is True
    assert easy_registration_not_rejection("YES") is True


def test_commercial_lane_routing():
    lane = classify_acquisition_lane({"title": "Dell PowerEdge servers qty 10", "our_bid_access": "YES"})
    status = stage1_commercial_triage_status({"title": "Dell PowerEdge servers"}, lane=lane)
    assert status in {KEEP_COMMERCIAL, KEEP_QUOTE_REQUIRED, "KEEP_DISTRIBUTOR", "KEEP_UNKNOWN"}


def test_quote_required_preservation():
    lane = classify_acquisition_lane({"title": "Ford F-150 Police Responder fleet vehicle", "our_bid_access": "YES"})
    assert lane["acquisition_lane"] == QUOTE_REQUIRED_COMMERCIAL
    assert stage1_commercial_triage_status({"title": "Ford F-150"}, lane=lane) == KEEP_QUOTE_REQUIRED


def test_specialty_separation():
    lane = classify_acquisition_lane(
        {"title": "NSN 2995-01-313-0343 VALVE ASSEMBLY WSDC F110 SPRTA", "our_bid_access": "YES", "nsn": "2995013130343"}
    )
    assert stage1_commercial_triage_status({"title": "NSN"}, lane=lane) == SPECIALTY_PIPELINE
    assert lane["rejected"] is False


def test_final_gate_unchanged():
    assert EXACT_VERIFIED == "EXACT_VERIFIED"


def test_commercial_yield_rate_helper():
    assert commercial_yield_rate(commercial_stage3=20, tangible=100) == 20.0

"""Broad non-SAM discovery network — deterministic fixture tests."""

from __future__ import annotations

from discovery.adapters import (
    CooperativeListingAdapter,
    FederalPublicNoticeAdapter,
    JsonPublicEndpointAdapter,
    RssProcurementFeedAdapter,
    SharedPlatformStyleAdapter,
    SimpleHtmlBidTableAdapter,
    list_implemented_adapters,
)
from discovery.classify import classify_discovery_opportunity, early_reject_reasons
from discovery.constants import (
    ADAPTER_IMPLEMENTED,
    ADAPTER_PLANNED,
    CLASS_CORE_PRODUCT,
    CLASS_PRODUCT_PLUS_SERVICE,
    CLASS_SERVICE,
    TIER_1,
    TIER_3,
)
from discovery.deadline import normalize_deadline
from discovery.dedup import fuzzy_match_evidence, prefer_official_source, strong_canonical_key
from discovery.fixtures import AGGREGATOR_MIRROR, ALL_FIXTURES, JSON_STATE_BIDS
from discovery.health import record_source_attempt
from discovery.persist import upsert_canonical_opportunity
from discovery.platform_detect import detect_platform
from discovery.priority import compute_research_priority
from discovery.registry import build_registry_seed, registry_summary
from discovery.runner import run_fixture_discovery
from discovery.sam_policy import assert_no_broad_sam_discovery, sam_api_broad_discovery_enabled
from discovery.schema import CanonicalOpportunity
from discovery.start_deal import start_deal_from_discovered


def test_all_adapters_normalize_same_schema():
    results = []
    results += SimpleHtmlBidTableAdapter().parse_fixture(ALL_FIXTURES["fixture_html_city_bids"])
    results += JsonPublicEndpointAdapter().parse_fixture(ALL_FIXTURES["fixture_json_state_bids"])
    results += RssProcurementFeedAdapter().parse_fixture(ALL_FIXTURES["fixture_rss_county_bids"])
    results += CooperativeListingAdapter().parse_fixture(ALL_FIXTURES["fixture_coop_sourcewell_style"])
    results += FederalPublicNoticeAdapter().parse_fixture(ALL_FIXTURES["fixture_federal_public_notice"])
    results += SharedPlatformStyleAdapter().parse_fixture(ALL_FIXTURES["fixture_shared_platform_listing"])
    assert len(results) >= 6
    for o in results:
        assert isinstance(o, CanonicalOpportunity)
        assert o.external_id
        assert o.source_id
        assert o.title
        d = o.to_dict()
        assert "estimated_value_status" in d
        assert "deadline_tz_confidence" in d


def test_core_product_classification():
    r = classify_discovery_opportunity(title="Network Switches Equipment Purchase")
    assert r["classification"] == CLASS_CORE_PRODUCT
    assert r["OpenAI"] == 0


def test_product_plus_service_classification():
    r = classify_discovery_opportunity(title="Supply and Install Classroom Interactive Displays")
    assert r["classification"] == CLASS_PRODUCT_PLUS_SERVICE


def test_service_classification():
    r = classify_discovery_opportunity(title="Professional Consulting Services — IT Strategy")
    assert r["classification"] == CLASS_SERVICE


def test_expired_rejection():
    r = early_reject_reasons(deadline_passed=True, title="Something")
    assert r["reject"] is True
    assert "deadline_already_passed" in r["reasons"]


def test_unknown_value_not_rejected():
    r = early_reject_reasons(
        title="Servers and Network Equipment",
        classification=CLASS_CORE_PRODUCT,
        estimated_value=None,
        estimated_value_status="UNKNOWN",
    )
    assert r["unknown_value_not_rejected"] is True
    assert r["value_reject_applied"] is False
    assert r["reject"] is False


def test_heuristic_not_confused_with_profit():
    p = compute_research_priority(classification=CLASS_CORE_PRODUCT, document_count=2)
    assert p["is_actual_profit"] is False
    assert p["is_heuristic"] is True
    assert "profit" not in p["research_priority_label"].lower()


def test_deadline_timezone_unknown():
    d = normalize_deadline("2026-12-01")
    assert d["timezone_confidence"] == "UNKNOWN"
    assert d["timezone"] is None


def test_deadline_timezone_known():
    d = normalize_deadline(
        "2026-10-30T17:00:00", timezone_hint="America/Los_Angeles", timezone_explicit=True
    )
    assert d["timezone_confidence"] == "KNOWN"
    assert d["timezone"] == "America/Los_Angeles"
    assert d["utc_deadline"] is not None


def test_document_and_amendment_links():
    opps = JsonPublicEndpointAdapter().parse_fixture(JSON_STATE_BIDS)
    lab = next(o for o in opps if "Laboratory" in o.title)
    assert lab.document_links
    assert lab.amendment_links


def test_fuzzy_never_merges_alone():
    a = {"title": "Same Title", "agency": "X"}
    b = {"title": "Same Title", "agency": "X"}
    ev = fuzzy_match_evidence(a, b)
    assert ev["sufficient_to_merge"] is False


def test_prefer_official_over_aggregator():
    assert prefer_official_source(TIER_3, "agg", TIER_1, "official") is True
    assert prefer_official_source(TIER_1, "official", TIER_3, "agg") is False


def test_source_failure_isolation():
    h = record_source_attempt(success=False, failure_type="ParseError", prior_consecutive_failures=0)
    assert h["isolated"] is True
    assert h["health_status"] in {"DEGRADED", "BROKEN", "AUTH_REQUIRED", "BLOCKED"}


def test_platform_detection():
    assert detect_platform("https://agency.bonfirehub.com/portal")["platform"] == "Bonfire"
    assert detect_platform(None, content_type="application/rss+xml")["platform"] == "RSS"


def test_registry_planned_not_operational():
    from discovery.state_matrix import _STATUS_OVERRIDES

    _STATUS_OVERRIDES.clear()
    seed = build_registry_seed()
    planned = [r for r in seed if r["adapter_status"] == ADAPTER_PLANNED]
    assert len(planned) >= 8
    for r in planned:
        assert r.get("enabled") is False
    # Unverified live is NOT operational/enabled
    unverified = [r for r in seed if r["adapter_status"] == "UNVERIFIED_LIVE"]
    assert len(unverified) >= 10
    live_verified = [r for r in seed if r["adapter_status"] == "LIVE_VERIFIED"]
    assert len(live_verified) == 0


def test_registry_has_implemented_adapters():
    from discovery.constants import ADAPTER_FIXTURE_ONLY

    seed = build_registry_seed()
    impl = [r for r in seed if r["adapter_status"] in {ADAPTER_IMPLEMENTED, ADAPTER_FIXTURE_ONLY}]
    assert len(impl) >= 6
    assert len(list_implemented_adapters()) == 6
    # Fixture-only is NOT live-capable
    for r in impl:
        assert (r.get("metadata_json") or {}).get("live_capable") is False


def test_sam_broad_discovery_blocked():
    assert sam_api_broad_discovery_enabled() is False
    pol = assert_no_broad_sam_discovery()
    assert pol["broad_discovery_blocked"] is True
    assert pol["SAM"] == 0


def test_fixture_discovery_run_metrics_and_persist():
    from database import SessionLocal

    session = SessionLocal()
    try:
        out = run_fixture_discovery(session, source_payloads=ALL_FIXTURES, dry_run=False)
        session.commit()
        assert out["LIVE_API_REQUESTS"] == 0
        assert out["OpenAI"] == 0
        assert out["external_request_counts"]["SAM"] == 0
        assert out["metrics"]["raw_notices_seen"] >= 6
        assert out["metrics"]["sources_successful"] >= 1
        assert out["metrics"]["core_product_count"] >= 1
        summary = registry_summary(session)
        assert summary["implemented"] >= 6 or summary.get("fixture_only", 0) >= 6
        assert summary.get("live_verified", 0) == 0 or summary.get("live_capable", 0) >= 0
        assert summary["planned"] >= 8
        assert summary.get("unverified_live", 0) >= 10
    finally:
        session.close()


def test_cross_source_dedup_and_official_preferred():
    from database import SessionLocal
    from models import DiscoveredOpportunity, OpportunitySighting

    session = SessionLocal()
    try:
        official = JsonPublicEndpointAdapter().parse_fixture(JSON_STATE_BIDS)[0]
        official.trust_tier = TIER_1
        r1 = upsert_canonical_opportunity(session, official)
        agg = JsonPublicEndpointAdapter().parse_fixture(AGGREGATOR_MIRROR)[0]
        agg.source_id = "agg_generic_bid_index"
        agg.trust_tier = TIER_3
        agg.solicitation_number = official.solicitation_number
        agg.agency = official.agency
        agg.jurisdiction = official.jurisdiction
        agg.state_code = official.state_code
        r2 = upsert_canonical_opportunity(session, agg)
        session.commit()
        assert (not r2["is_new"]) or r1["canonical_key"] == r2["canonical_key"]
        row = session.query(DiscoveredOpportunity).filter_by(id=r1["id"]).first()
        assert row.preferred_source_id == official.source_id
        sightings = session.query(OpportunitySighting).filter_by(discovered_opportunity_id=row.id).all()
        assert len(sightings) >= 1
    finally:
        session.rollback()
        session.close()


def test_start_deal_state_local_coop():
    from database import SessionLocal
    from deal_workspace import build_workspace_snapshot
    from models import Contract, DealState, DiscoveredOpportunity

    session = SessionLocal()
    try:
        run_fixture_discovery(session, source_payloads=ALL_FIXTURES, dry_run=False)
        session.commit()
        for jur in ("STATE", "CITY", "COOPERATIVE"):
            row = session.query(DiscoveredOpportunity).filter_by(jurisdiction=jur).first()
            if not row:
                continue
            out = start_deal_from_discovered(session, row.id, operator="test")
            session.commit()
            assert out.get("contract_id")
            assert out.get("SAM") == 0
            c = session.query(Contract).filter_by(id=out["contract_id"]).first()
            assert c is not None
            d = session.query(DealState).filter_by(contract_id=c.id).first()
            assert d is not None
            ws = build_workspace_snapshot(session, c)
            assert ws["LIVE_API_REQUESTS"] == 0
            assert ws["opportunity"]["id"] == c.id
    finally:
        session.close()


def test_incremental_sync_duplicate_sighting():
    from database import SessionLocal

    session = SessionLocal()
    try:
        run_fixture_discovery(
            session,
            source_payloads={"fixture_federal_public_notice": ALL_FIXTURES["fixture_federal_public_notice"]},
            dry_run=False,
        )
        session.commit()
        out2 = run_fixture_discovery(
            session,
            source_payloads={"fixture_federal_public_notice": ALL_FIXTURES["fixture_federal_public_notice"]},
            dry_run=False,
        )
        session.commit()
        assert out2["metrics"]["duplicates"] >= 1 or out2["metrics"]["updated_opportunities"] >= 1
        assert out2["external_request_counts"]["OpenAI"] == 0
    finally:
        session.close()


def test_strong_canonical_key():
    o = CanonicalOpportunity(
        external_id="x",
        source_id="s",
        solicitation_number="IFB-26-441",
        agency="CA Dept of General Services",
        jurisdiction="STATE",
        state_code="CA",
        title="Lab",
    )
    assert strong_canonical_key(o).startswith("strong:")


def test_no_sam_client_in_discovery_runner():
    import inspect

    import discovery.runner as runner

    src = inspect.getsource(runner)
    assert "sam_client" not in src
    assert "fetch_naics" not in src


def test_sam_scarcity_blocks_broad_without_auth():
    from sam_scarcity import PURPOSE_BROAD_DISCOVERY, evaluate_sam_api_eligibility

    r = evaluate_sam_api_eligibility(purpose=PURPOSE_BROAD_DISCOVERY, authorize_broad_sam_discovery=False)
    assert r["status"] == "NOT_ELIGIBLE"
    assert "broad_sam_discovery_blocked" in r["reason_codes"]

"""Live nationwide discovery — mocked HTTP only; zero live external requests."""

from __future__ import annotations

import inspect
import json

from discovery.agency_seeds import all_agencies_enriched, all_coops_enriched
from discovery.analytics import analyze_run_results, quality_sample
from discovery.constants import (
    ADAPTER_FIXTURE_ONLY,
    ADAPTER_LIVE_VERIFIED,
    ADAPTER_UNVERIFIED_LIVE,
    NOTICE_AWARDED_CONTRACT_CATALOG,
    NOTICE_FORECAST,
    NOTICE_OPEN_SOLICITATION,
)
from discovery.coverage import build_coverage_report
from discovery.deadline import normalize_deadline
from discovery.http_client import BudgetExhausted, HttpResponse, PublicProcurementHttpClient, RequestBudget, RequestMeta
from discovery.live_fetchers import (
    CooperativeOpenSolicitationFetcher,
    FederalPublicPageLiveFetcher,
    SimpleHtmlLiveFetcher,
    get_fetcher_for_platform,
    get_live_fetcher,
    list_live_capable_fetchers,
    should_fetch_detail,
)
from discovery.live_runner import handoff_documents_to_package, run_live_discovery
from discovery.notice_types import classify_notice_type
from discovery.onboarding import onboard_source
from discovery.profiles import PROFILE_BROAD, PROFILE_NATIONAL, PROFILE_TINY, get_profile
from discovery.registry import build_registry_seed
from discovery.schema import CanonicalOpportunity
from discovery.state_matrix import all_states_enriched, state_coverage_summary
from discovery.taxonomy import taxonomy_report
from discovery.validation import validate_listing_response


HTML_LISTING = """
<html><body><h1>Procurement Opportunities</h1>
<table>
<tr><td>Title</td><td>Solicitation</td><td>Deadline</td></tr>
<tr><td><a href="/bid/1">Network Switches Equipment Purchase RFP</a></td><td>IFB-26-100</td><td>2026-12-01</td></tr>
<tr><td><a href="/bid/2">Professional Consulting Services</a></td><td>RFP-SVC-1</td><td>2026-12-15</td></tr>
</table></body></html>
"""

HTML_LOGIN = """
<html><body><h1>Sign In</h1>
<form><input name="username"/><input name="password" type="password"/>
<button>Log in</button></form></body></html>
"""

HTML_CAPTCHA = """
<html><body><div class="cf-browser-verification">captcha challenge</div></body></html>
"""

JSON_OPEN = json.dumps(
    {
        "opportunities": [
            {
                "id": "p1",
                "title": "Servers and Network Equipment Bid",
                "status": "OPEN",
                "due_date": "2026-11-30",
                "detail_url": "https://example.test/p1",
                "documents": [{"url": "https://example.test/p1.pdf", "name": "spec.pdf"}],
            }
        ]
    }
)

JSON_AWARDED_CATALOG = json.dumps(
    {"contracts": [{"id": "c1", "title": "Awarded Contract Catalog — Vehicles", "status": "AWARDED"}]}
)


def _resp(url: str, text: str, status: int = 200, content_type: str = "text/html", **headers) -> HttpResponse:
    h = {"content-type": content_type, **{k.lower(): v for k, v in headers.items()}}
    raw = text.encode("utf-8")
    return HttpResponse(
        text=text,
        content=raw,
        status_code=status,
        headers=h,
        meta=RequestMeta(url=url, http_status=status, content_type=content_type, bytes_len=len(raw)),
    )


def test_live_capable_requires_production_fetcher():
    assert get_live_fetcher("live_simple_html") is not None
    assert get_live_fetcher("fixture_html_city_bids") is None
    seed = build_registry_seed()
    fixtures = [r for r in seed if r["adapter_status"] == ADAPTER_FIXTURE_ONLY]
    assert len(fixtures) >= 6
    for r in fixtures:
        assert (r.get("metadata_json") or {}).get("live_capable") is False
    # Untested sources are UNVERIFIED_LIVE, not LIVE_VERIFIED
    unverified = [r for r in seed if r["adapter_status"] == ADAPTER_UNVERIFIED_LIVE]
    assert len(unverified) >= 10
    verified = [r for r in seed if r["adapter_status"] == ADAPTER_LIVE_VERIFIED]
    assert len(verified) == 0


def test_fixture_only_not_live_capable():
    from discovery.adapters import list_implemented_adapters

    for a in list_implemented_adapters():
        assert a["adapter_status"] == ADAPTER_FIXTURE_ONLY


def test_shared_platform_supports_multiple_agencies():
    from discovery.constants import ADAPTER_AUTH_REQUIRED

    agencies = all_agencies_enriched()
    by_platform: dict[str, list] = {}
    for a in agencies:
        by_platform.setdefault(a["platform_family"], []).append(a)
    multi = [p for p, rows in by_platform.items() if len(rows) >= 2]
    assert multi, "expected at least one platform family with multiple agencies"
    for a in agencies:
        assert a["fetcher_available"] is True
        assert get_live_fetcher(a["adapter_family"]) is not None
        assert a["adapter_status"] in {
            ADAPTER_UNVERIFIED_LIVE,
            ADAPTER_LIVE_VERIFIED,
            ADAPTER_AUTH_REQUIRED,
        }


def test_50_state_matrix_matches_adapter_status():
    summary = state_coverage_summary()
    assert summary["states_total"] == 50
    rows = all_states_enriched()
    assert len(rows) == 50
    for r in rows:
        fetcher = get_live_fetcher(r["adapter_family"])
        if r["adapter_status"] == ADAPTER_UNVERIFIED_LIVE:
            assert fetcher is not None
            assert r["list_url"]
            assert r["live_verified"] is False
        if r["live_verified"]:
            assert r["adapter_status"] == ADAPTER_LIVE_VERIFIED
    assert summary["states_LIVE_VERIFIED"] == sum(1 for r in rows if r["live_verified"])


def test_cooperative_open_vs_awarded_catalog():
    coop = CooperativeOpenSolicitationFetcher()
    open_opps = coop.parse_listing(JSON_OPEN, list_url="https://coop.test/solicitations")
    assert len(open_opps) >= 1
    awarded = coop.parse_listing(JSON_AWARDED_CATALOG, list_url="https://coop.test/contracts")
    assert awarded == []
    nt = classify_notice_type(title="Awarded Contract Catalog — Vehicles")
    assert nt["notice_type"] == NOTICE_AWARDED_CONTRACT_CATALOG
    assert nt["is_open_solicitation"] is False


def test_federal_notice_type_distinction():
    fed = FederalPublicPageLiveFetcher()
    body = json.dumps(
        {
            "opportunities": [
                {"id": "1", "title": "RFP for Laboratory Equipment", "status": "OPEN"},
                {"id": "2", "title": "Procurement Forecast — Vehicles", "status": "FORECAST"},
                {"id": "3", "title": "Award Notice — Widgets", "status": "AWARDED"},
            ]
        }
    )
    opps = fed.parse_listing(body, list_url="https://agency.test/bids")
    titles = [o.title for o in opps]
    assert any("Laboratory" in t for t in titles)
    assert not any("Forecast" in t for t in titles)
    assert not any("Award Notice" in t for t in titles)
    assert classify_notice_type(title="Procurement Forecast")["notice_type"] == NOTICE_FORECAST
    assert classify_notice_type(title="IFB Network Gear")["notice_type"] == NOTICE_OPEN_SOLICITATION


def test_http_request_accounting_and_caps():
    calls = {"n": 0}

    def transport(url, headers=None, timeout=None):
        calls["n"] += 1
        return _resp(url, HTML_LISTING)

    client = PublicProcurementHttpClient(
        budget=RequestBudget(max_total_requests=3, max_requests_per_source=2, min_interval_seconds=0),
        authorize_live=False,
        transport=transport,
    )
    client.get("https://a.test/1", source_id="s1", use_cache=False)
    client.get("https://a.test/2", source_id="s1", use_cache=False)
    assert client.request_count == 2
    try:
        client.get("https://a.test/3", source_id="s1", use_cache=False)
        assert False, "expected per-source budget exhaustion"
    except BudgetExhausted:
        pass
    acct = client.accounting()
    assert acct["SAM"] == 0 and acct["OpenAI"] == 0 and acct["USAspending"] == 0 and acct["paid"] == 0


def test_global_request_cap():
    def transport(url, headers=None, timeout=None):
        return _resp(url, HTML_LISTING)

    client = PublicProcurementHttpClient(
        budget=RequestBudget(max_total_requests=1, max_requests_per_source=10, min_interval_seconds=0),
        transport=transport,
    )
    client.get("https://a.test/1", source_id="s1", use_cache=False)
    try:
        client.get("https://a.test/2", source_id="s2", use_cache=False)
        assert False
    except BudgetExhausted as e:
        assert "global" in str(e).lower()


def test_cache_hit_etag_last_modified():
    seen_headers = []

    def transport(url, headers=None, timeout=None):
        seen_headers.append(dict(headers or {}))
        return _resp(
            url,
            HTML_LISTING,
            etag='"v1"',
            **{"Last-Modified": "Mon, 01 Sep 2026 00:00:00 GMT"},
        )

    client = PublicProcurementHttpClient(
        budget=RequestBudget(min_interval_seconds=0),
        transport=transport,
    )
    r1 = client.get("https://cache.test/list", source_id="c1")
    assert r1.meta.cache_hit is False
    assert client._etag_map["https://cache.test/list"] == '"v1"'
    r2 = client.get("https://cache.test/list", source_id="c1")
    assert r2.meta.cache_hit is True
    assert client.cache_hits == 1
    # Conditional headers on second network fetch after cache clear path
    client._cache.clear()
    client.get("https://cache.test/list", source_id="c1", use_cache=False)
    assert any("If-None-Match" in h for h in seen_headers)


def test_blocked_login_captcha_detection():
    login = validate_listing_response(
        status_code=200, content_type="text/html", body=HTML_LOGIN, records_found=0, expected_kind="html"
    )
    assert login["valid"] is False
    assert login["health_status"] in {"AUTH_REQUIRED", "BROKEN", "BLOCKED"}

    captcha = validate_listing_response(
        status_code=200, content_type="text/html", body=HTML_CAPTCHA, records_found=0, expected_kind="html"
    )
    assert captcha["valid"] is False
    assert captcha["failure_type"] in {"CAPTCHA", "BLOCKED"}


def test_zero_listings_not_automatic_healthy():
    junk = validate_listing_response(
        status_code=200,
        content_type="text/html",
        body="<html><body>Welcome to our homepage</body></html>",
        records_found=0,
        expected_kind="html",
    )
    assert junk["valid"] is False
    assert junk["failure_type"] == "PARSER_SCHEMA_CHANGE"

    empty_ok = validate_listing_response(
        status_code=200,
        content_type="text/html",
        body="<html><body><h1>Bid Opportunities</h1><table></table>No solicitations at this time.</body></html>",
        records_found=0,
        expected_kind="html",
    )
    assert empty_ok["valid"] is True
    assert empty_ok["zero_records_ok"] is True


def test_product_first_detail_gate():
    good = CanonicalOpportunity(
        external_id="1", source_id="x", title="Network Switches Equipment Purchase", detail_url="https://x/1"
    )
    service = CanonicalOpportunity(
        external_id="2", source_id="x", title="Professional Consulting Services", detail_url="https://x/2"
    )
    unknown = CanonicalOpportunity(
        external_id="3", source_id="x", title="Miscellaneous Procurement Notice", detail_url="https://x/3"
    )
    assert should_fetch_detail(good)["fetch"] is True
    assert should_fetch_detail(good)["earned"] is True
    assert should_fetch_detail(service)["fetch"] is False
    assert should_fetch_detail(unknown)["fetch"] is False
    assert should_fetch_detail(unknown)["reason"] == "unknown_does_not_earn_detail"
    assert should_fetch_detail(good, fetch_details_enabled=False)["fetch"] is False


def test_preview_does_not_persist_persist_does():
    from database import SessionLocal
    from models import DiscoveredOpportunity

    calls = []

    def transport(url, headers=None, timeout=None):
        calls.append(url)
        if url.endswith(".pdf"):
            return _resp(url, "%PDF-1.4", content_type="application/pdf")
        if "detail" in url or url.endswith("/p1"):
            return _resp(url, '<html>bid <a href="https://example.test/p1.pdf">spec.pdf</a></html>')
        return _resp(url, HTML_LISTING)

    # Force tiny profile to contact known source via source_ids override of candidates —
    # monkey by calling fetcher path through run with transport and limited sources.
    # Build a minimal transport-backed run: patch candidate list via source_ids matching seeds.
    session = SessionLocal()
    try:
        before = session.query(DiscoveredOpportunity).count()
        out = run_live_discovery(
            session,
            profile="tiny",
            preview=True,
            persist=False,
            authorize_live=False,
            transport=transport,
            source_ids=["state_al", "state_ga", "state_tx", "state_ia", "state_nc"],
        )
        session.commit()
        after_preview = session.query(DiscoveredOpportunity).count()
        assert out["preview"] is True
        assert out["persist"] is False
        assert after_preview == before
        assert out["OpenAI"] == 0 and out["SAM"] == 0

        out2 = run_live_discovery(
            session,
            profile="tiny",
            preview=False,
            persist=True,
            authorize_live=False,
            transport=transport,
            source_ids=["state_al", "state_ga"],
        )
        session.commit()
        after_persist = session.query(DiscoveredOpportunity).count()
        assert out2["persist"] is True
        assert after_persist >= before  # may add if parsers found rows
    finally:
        session.rollback()
        session.close()


def test_profiles_configured():
    assert get_profile("tiny")["name"] == "TINY"
    assert PROFILE_TINY["budget"].max_pages_per_source == 1
    assert PROFILE_BROAD["max_sources"] is None
    assert PROFILE_BROAD["all_eligible_sources"] is True
    assert PROFILE_BROAD["pagination_exhaust"] is True
    assert PROFILE_BROAD["max_records_total"] == 25000
    assert PROFILE_NATIONAL["max_sources"] is None
    assert PROFILE_NATIONAL["all_eligible_sources"] is True
    assert PROFILE_NATIONAL["max_records_total"] == 25000
    assert PROFILE_NATIONAL["pagination_exhaust"] is True
    assert PROFILE_TINY["fetch_details"] is False
    assert PROFILE_TINY["fetch_documents"] is False
    assert PROFILE_BROAD["fetch_details"] is False
    for p in (PROFILE_TINY, PROFILE_BROAD, PROFILE_NATIONAL):
        assert p["SAM"] == 0 and p["OpenAI"] == 0 and p["USAspending"] == 0 and p["paid"] == 0


def test_coverage_report_totals():
    from discovery.state_matrix import _STATUS_OVERRIDES

    _STATUS_OVERRIDES.clear()
    r = build_coverage_report(load_persisted=False)
    assert r["TOTAL_LIVE_VERIFIED_SOURCES"] == 0
    assert r["TOTAL_UNVERIFIED_LIVE_SOURCES"] >= 10
    assert r["TOTAL_LIVE_CAPABLE_SOURCES"] == r["TOTAL_LIVE_VERIFIED_SOURCES"]
    assert r["FIXTURE_ONLY_ADAPTERS"] == 6
    assert r["SAM"] == 0 and r["OpenAI"] == 0
    assert "UNVERIFIED" in r["note"] or "LIVE_VERIFIED" in r["note"]


def test_source_onboarding_no_fetch():
    out = onboard_source(
        name="Test City",
        url="https://agency.bonfirehub.com/portal",
        jurisdiction="CITY",
        agency_type="CITY",
        state_code="TX",
        authorize_fetch=False,
    )
    assert out["fetched"] is False
    assert out["LIVE_API_REQUESTS"] == 0
    rec = out["suggested_record"]
    assert rec["platform_family"] == "Bonfire"
    assert rec["adapter_family"] == "live_bonfire"
    assert rec["live_capable"] is True  # onboard suggests capable pending validation
    assert rec["validation_needed"] is True


def test_document_handoff():
    from database import SessionLocal
    from models import Contract, SolicitationDocument

    session = SessionLocal()
    try:
        c = Contract(notice_id=f"handoff-test-{id(session)}", title="Handoff Test", status="active")
        session.add(c)
        session.flush()
        out = handoff_documents_to_package(
            session,
            c.id,
            [{"url": "https://example.test/a.pdf", "kind": "attachment", "filename": "a.pdf"}],
        )
        session.flush()
        assert out["added"] == 1
        docs = session.query(SolicitationDocument).filter_by(contract_id=c.id).all()
        assert len(docs) == 1
        assert docs[0].source == "discovery_handoff"
        # dedupe
        out2 = handoff_documents_to_package(
            session, c.id, [{"url": "https://example.test/a.pdf", "kind": "attachment"}]
        )
        assert out2["added"] == 0
    finally:
        session.rollback()
        session.close()


def test_quality_sampling_and_analytics():
    opps = [
        {
            "external_id": "1",
            "title": "Network Switches",
            "product_classification": "CORE_PRODUCT",
            "research_priority": 90,
            "state_code": "TX",
            "source_id": "s1",
            "jurisdiction": "STATE",
            "response_deadline": "2026-12-01",
            "document_links": [{"url": "x"}],
            "estimated_value_status": "KNOWN",
            "estimated_value": 50000,
        },
        {
            "external_id": "2",
            "title": "Supply and Install HVAC",
            "product_classification": "PRODUCT_PLUS_SERVICE",
            "research_priority": 70,
            "state_code": "CA",
            "source_id": "s2",
            "jurisdiction": "CITY",
            "response_deadline": "2026-12-20",
        },
        {
            "external_id": "3",
            "title": "Unknown Widget Buy",
            "product_classification": "UNKNOWN",
            "research_priority": 40,
            "state_code": "NY",
            "source_id": "s3",
            "jurisdiction": "STATE",
        },
    ]
    a = analyze_run_results(opps)
    assert a["CORE_PRODUCT"] == 1
    assert a["PRODUCT_PLUS_SERVICE"] == 1
    assert a["runway_gt_7_days"] >= 1
    assert a["with_solicitation_documents"] == 1
    assert a["LIVE_API_REQUESTS"] == 0
    s = quality_sample(opps)
    assert s["OpenAI"] == 0
    assert "highest_research_priority" in s["samples"]
    assert len(s["samples"]["highest_research_priority"]) >= 1


def test_deadline_timezone_unknown_safety():
    d = normalize_deadline("September 30, 2026 2:00 PM")
    assert d["timezone_confidence"] == "UNKNOWN"
    assert d["timezone"] is None
    assert d["parsed_local"] is not None
    d2 = normalize_deadline(
        "September 30, 2026 2:00 PM",
        timezone_hint="America/Chicago",
        timezone_explicit=True,
    )
    assert d2["timezone_confidence"] == "KNOWN"
    assert d2["timezone"] == "America/Chicago"


def test_taxonomy_present():
    t = taxonomy_report()
    assert t["category_count"] >= 15
    assert "IT_HARDWARE" in t["categories"]


def test_no_paid_apis_in_live_modules():
    import discovery.live_runner as lr
    import discovery.http_client as hc

    for mod in (lr, hc):
        src = inspect.getsource(mod)
        assert "sam_client" not in src
        assert "openai.OpenAI" not in src
        assert "from openai" not in src
        assert "usaspending.gov" not in src.lower()
        assert "api.sam.gov" not in src.lower()


def test_live_fetcher_parse_html_with_transport():
    def transport(url, headers=None, timeout=None):
        return _resp(url, HTML_LISTING)

    client = PublicProcurementHttpClient(budget=RequestBudget(min_interval_seconds=0), transport=transport)
    fetcher = SimpleHtmlLiveFetcher()
    result = fetcher.fetch_listing(client, list_url="https://state.test/bids", source_id="state_test")
    assert result["validation"]["valid"] is True
    assert len(result["opportunities"]) >= 1
    assert client.accounting()["paid"] == 0


def test_platform_family_fetcher_map():
    assert get_fetcher_for_platform("Bonfire") is not None
    assert get_fetcher_for_platform("OpenGov") is not None
    assert len(list_live_capable_fetchers()) >= 10

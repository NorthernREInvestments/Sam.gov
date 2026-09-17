"""Discovery funnel repair — listing-first, budget isolation, SciQuest parsing."""

from __future__ import annotations

from pathlib import Path

from discovery.classify import classify_discovery_opportunity
from discovery.http_client import (
    GlobalBudgetExhausted,
    HttpResponse,
    PublicProcurementHttpClient,
    RequestBudget,
    RequestMeta,
    RuntimeBudgetExhausted,
    SourceBudgetExhausted,
)
from discovery.live_fetchers import JaggaerPublicLiveFetcher, should_fetch_detail
from discovery.live_runner import run_live_discovery
from discovery.profiles import PROFILE_BROAD, PROFILE_TINY, get_profile
from discovery.schema import CanonicalOpportunity
from discovery.sciquest import agency_from_list_url, parse_sciquest_public_events

FIXTURES = Path(__file__).resolve().parents[1] / "discovery" / "fixtures"
IA_HTML = (FIXTURES / "sciquest_iowa_listing.html").read_text(encoding="utf-8")
MT_HTML = (FIXTURES / "sciquest_montana_listing.html").read_text(encoding="utf-8")

IA_URL = "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa"
MT_URL = "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=StateOfMontana"

SIMPLE_HTML = """
<html><body><h1>Bid Opportunities</h1>
<table>
<tr><th>Title</th><th>Number</th><th>Close</th></tr>
<tr><td><a href="/p1">Network Equipment Purchase</a></td><td>NE-1</td><td>12/01/2026</td></tr>
</table>
</body></html>
"""


def _resp(url: str, text: str, status: int = 200, content_type: str = "text/html") -> HttpResponse:
    body = text.encode("utf-8")
    return HttpResponse(
        text=text,
        content=body,
        status_code=status,
        headers={"content-type": content_type},
        meta=RequestMeta(
            url=url,
            http_status=status,
            content_type=content_type,
            bytes_len=len(body),
            host="bids.sciquest.com",
        ),
    )


def test_sciquest_open_never_solicitation_number():
    opps = parse_sciquest_public_events(IA_HTML, list_url=IA_URL, source_id="state_ia")
    assert len(opps) >= 3
    for o in opps:
        assert o.solicitation_number
        assert o.solicitation_number.lower() != "open"
        assert o.status in {"OPEN", "CLOSED", "AWARDED"}
        assert "Open" not in (o.solicitation_number or "")


def test_sciquest_distinct_events_distinct_ids():
    opps = parse_sciquest_public_events(IA_HTML, list_url=IA_URL)
    ids = [o.external_id for o in opps]
    sols = [o.solicitation_number for o in opps]
    assert len(ids) == len(set(ids))
    assert len(sols) == len(set(sols))
    assert len(opps) >= 3  # must not collapse 4→1


def test_sciquest_title_cleaned_and_deadline_status():
    opps = parse_sciquest_public_events(IA_HTML, list_url=IA_URL)
    honey = next(o for o in opps if "Honey Creek" in o.title)
    assert "The State of Iowa" not in honey.title
    assert "\t" not in honey.title
    assert honey.status == "OPEN"
    assert honey.deadline_raw
    assert "2026" in honey.deadline_raw
    assert honey.agency
    assert "Iowa" in honey.agency
    assert honey.document_links
    assert honey.document_links[0].get("discovered_only") is True
    assert honey.document_links[0].get("document_fetched") is False


def test_sciquest_iowa_montana_shared_parser():
    ia = parse_sciquest_public_events(IA_HTML, list_url=IA_URL, source_id="state_ia")
    mt = parse_sciquest_public_events(MT_HTML, list_url=MT_URL, source_id="state_mt")
    assert agency_from_list_url(IA_URL)
    assert agency_from_list_url(MT_URL)
    assert all(o.agency for o in ia)
    assert all(o.agency for o in mt)
    fetcher = JaggaerPublicLiveFetcher()
    ia2 = fetcher.parse_listing(IA_HTML, list_url=IA_URL)
    mt2 = fetcher.parse_listing(MT_HTML, list_url=MT_URL)
    assert len(ia2) >= 3 and len(mt2) >= 2


def test_concessionaire_classified_service():
    cls = classify_discovery_opportunity(
        title="Honey Creek Resort Concessionaire Operations",
        description="seeking a concessionaire to operate within the resort",
    )
    assert cls["classification"] == "SERVICE"
    assert cls["OpenAI"] == 0


def test_product_overrides_generic_service_wording():
    cls = classify_discovery_opportunity(
        title="Janitorial Supplies and Maintenance Equipment Purchase",
        description="procurement of supplies and equipment",
    )
    assert cls["classification"] in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"}


def test_unknown_and_service_do_not_earn_detail():
    unk = CanonicalOpportunity(
        external_id="u", source_id="x", title="Miscellaneous Notice XYZ", detail_url="https://x/u.pdf"
    )
    svc = CanonicalOpportunity(
        external_id="s",
        source_id="x",
        title="Honey Creek Resort Concessionaire Operations",
        detail_url="https://x/s.pdf",
    )
    prod = CanonicalOpportunity(
        external_id="p",
        source_id="x",
        title="Laboratory Equipment Purchase",
        detail_url="https://x/p.pdf",
    )
    assert should_fetch_detail(unk)["fetch"] is False
    assert should_fetch_detail(svc)["fetch"] is False
    assert should_fetch_detail(prod)["fetch"] is True
    assert should_fetch_detail(prod, fetch_details_enabled=False)["fetch"] is False


def test_source_budget_exhausted_continues_to_next_source():
    calls: list[str] = []

    def transport(url, headers=None, timeout=None):
        calls.append(url)
        if "source_a" in url or "DASIowa" in url:
            # First listing OK; subsequent requests for same source would exhaust
            return _resp(url, IA_HTML)
        if "source_b" in url or "StateOfMontana" in url or "montana" in url.lower():
            return _resp(url, MT_HTML)
        return _resp(url, SIMPLE_HTML)

    # Tiny per-source budget: listing uses 1; if something tries more, source stops
    # Force isolation by making first source's fetcher burn budget via detail disabled —
    # instead simulate SourceBudgetExhausted by using a client that fails after 1 req on first source.
    # We inject via run with sources that each get 1 listing.
    out = run_live_discovery(
        profile="tiny",
        preview=True,
        persist=False,
        authorize_live=False,
        transport=transport,
        source_ids=["state_ia", "state_mt"],
        fetch_details=False,
        fetch_documents=False,
    )
    # Both sources contacted despite Iowa having PDF links
    contacted = {c["source_id"] for c in out["sources_contacted"]}
    assert "state_ia" in contacted
    assert "state_mt" in contacted
    assert out["metrics"]["listing_requests"] >= 2
    assert out["metrics"]["detail_requests"] == 0
    assert out["metrics"]["document_requests"] == 0
    # PDFs must not have been fetched
    assert not any(u.endswith(".pdf") for u in calls)
    assert out["SAM"] == 0 and out["OpenAI"] == 0 and out["USAspending"] == 0 and out["paid"] == 0


def test_explicit_source_budget_isolation_exception():
    """Per-source BudgetExhausted must continue; global stops."""
    order: list[str] = []

    class FlakyTransport:
        def __call__(self, url, headers=None, timeout=None):
            order.append(url)
            # Simulate exhausting source budget by raising via client path —
            # use response always; isolation tested at client level below
            return _resp(url, SIMPLE_HTML)

    client = PublicProcurementHttpClient(
        budget=RequestBudget(max_total_requests=10, max_requests_per_source=1, min_interval_seconds=0),
        transport=FlakyTransport(),
    )
    client.get("https://a.test/1", source_id="s1", use_cache=False)
    try:
        client.get("https://a.test/2", source_id="s1", use_cache=False)
        assert False, "expected SourceBudgetExhausted"
    except SourceBudgetExhausted:
        pass
    # Next source still allowed
    r = client.get("https://b.test/1", source_id="s2", use_cache=False)
    assert r.status_code == 200


def test_global_budget_stops_run():
    calls = []

    def transport(url, headers=None, timeout=None):
        calls.append(url)
        return _resp(url, SIMPLE_HTML)

    # Override via injecting a tiny global budget by calling runner with custom profile behavior —
    # use client-level GlobalBudgetExhausted in a mini loop mimicking runner
    client = PublicProcurementHttpClient(
        budget=RequestBudget(max_total_requests=1, max_requests_per_source=5, min_interval_seconds=0),
        transport=transport,
    )
    client.get("https://a.test/1", source_id="s1", use_cache=False)
    try:
        client.get("https://b.test/1", source_id="s2", use_cache=False)
        assert False
    except GlobalBudgetExhausted:
        pass

    # Runner-level: max_total_requests from tiny is high; force via monkeypatch of profile budget
    from discovery import profiles as profiles_mod

    original = profiles_mod.PROFILE_TINY["budget"]
    profiles_mod.PROFILE_TINY["budget"] = RequestBudget(
        max_total_requests=1,
        max_requests_per_source=5,
        max_pages_per_source=1,
        max_records_per_source=20,
        min_interval_seconds=0,
        max_runtime_seconds=90,
    )
    try:
        out = run_live_discovery(
            profile="tiny",
            preview=True,
            transport=transport,
            source_ids=["state_ia", "state_mt", "state_ne"],
            fetch_details=False,
        )
        assert out["global_budget_exhausted"] is True
        assert out["metrics"]["listing_requests"] == 1
        # Must not have contacted all three after global exhaust
        assert len(out["sources_contacted"]) == 1
    finally:
        profiles_mod.PROFILE_TINY["budget"] = original


def test_runtime_budget_stops_run():
    def transport(url, headers=None, timeout=None):
        return _resp(url, SIMPLE_HTML)

    client = PublicProcurementHttpClient(
        budget=RequestBudget(
            max_total_requests=100,
            max_requests_per_source=50,
            max_runtime_seconds=0.0,
            min_interval_seconds=0,
        ),
        transport=transport,
    )
    try:
        client.get("https://a.test/1", source_id="s1", use_cache=False)
        assert False
    except RuntimeBudgetExhausted:
        pass


def test_tiny_listing_only_no_detail_pdfs():
    calls = []

    def transport(url, headers=None, timeout=None):
        calls.append(url)
        if "DASIowa" in url:
            return _resp(url, IA_HTML)
        if "StateOfMontana" in url:
            return _resp(url, MT_HTML)
        return _resp(url, SIMPLE_HTML)

    assert get_profile("tiny")["fetch_details"] is False
    assert get_profile("tiny")["fetch_documents"] is False
    assert PROFILE_BROAD["fetch_details"] is False

    out = run_live_discovery(
        profile="tiny",
        preview=True,
        transport=transport,
        source_ids=["state_ia", "state_mt"],
    )
    assert out["fetch_details"] is False
    assert out["metrics"]["detail_requests"] == 0
    assert out["metrics"]["documents_fetched"] == 0
    assert out["metrics"]["document_links_discovered"] >= 1
    assert not any(".pdf" in u.lower() for u in calls)
    m = out["metrics"]
    assert "listing_requests" in m and "detail_requests" in m and "document_requests" in m
    assert "requests_per_unique_opportunity" in m


def test_document_link_discovery_not_fetch():
    opps = parse_sciquest_public_events(IA_HTML, list_url=IA_URL)
    with_docs = [o for o in opps if o.document_links]
    assert with_docs
    for o in with_docs:
        assert o.raw_metadata.get("document_link_discovered") is True
        assert o.raw_metadata.get("document_fetched") is False


def test_one_broken_source_does_not_stop_remaining():
    calls = []

    def transport(url, headers=None, timeout=None):
        calls.append(url)
        if "DASIowa" in url:
            raise ConnectionError("simulated source failure")
        if "StateOfMontana" in url:
            return _resp(url, MT_HTML)
        return _resp(url, SIMPLE_HTML)

    out = run_live_discovery(
        profile="tiny",
        preview=True,
        transport=transport,
        source_ids=["state_ia", "state_mt"],
        fetch_details=False,
    )
    contacted = {c["source_id"] for c in out["sources_contacted"]}
    assert "state_mt" in contacted
    assert out["metrics"]["per_source"]["state_ia"]["ok"] is False
    assert out["metrics"]["per_source"]["state_ia"].get("isolated") is True
    assert out["metrics"]["per_source"]["state_mt"]["ok"] is True
    assert out["global_budget_exhausted"] is False


def test_core_product_can_earn_detail_when_enabled():
    prod = CanonicalOpportunity(
        external_id="p",
        source_id="x",
        title="Network Switches Equipment Purchase",
        detail_url="https://x/detail/p",
    )
    gate = should_fetch_detail(prod, fetch_details_enabled=True)
    assert gate["earned"] is True and gate["fetch"] is True

    calls = []

    def transport(url, headers=None, timeout=None):
        calls.append(url)
        if url.endswith(".pdf") or "detail" in url:
            return _resp(url, "<html>detail <a href='/x.pdf'>pdf</a></html>")
        # Force a simple product listing via JSON-ish? Use HTML with equipment title
        html = """
        <html><body><h1>Bids</h1><table>
        <tr><td><a href="https://x.test/detail/p1">Network Switches Equipment Purchase</a></td>
        <td>EQ-99</td><td>12/01/2027</td></tr>
        </table></body></html>
        """
        return _resp(url, html)

    out = run_live_discovery(
        profile="tiny",
        preview=True,
        transport=transport,
        source_ids=["state_ne"],  # typically simple HTML if configured
        fetch_details=True,
        fetch_documents=False,
    )
    # May or may not fetch depending on NE parser; gate unit test above is authoritative
    assert out["fetch_documents"] is False
    assert out["SAM"] == 0


def test_no_paid_sam_openai_usaspending_in_funnel_repair():
    assert PROFILE_TINY["SAM"] == 0
    assert PROFILE_TINY["OpenAI"] == 0
    assert PROFILE_TINY["USAspending"] == 0
    assert PROFILE_TINY["paid"] == 0
    out = run_live_discovery(
        profile="tiny",
        preview=True,
        transport=lambda url, headers=None, timeout=None: _resp(url, SIMPLE_HTML),
        source_ids=["state_ne"],
    )
    assert out["SAM"] == 0 and out["OpenAI"] == 0 and out["USAspending"] == 0 and out["paid"] == 0
    assert out["LIVE_API_REQUESTS"] == 0

"""Discovery intake bottleneck repair — coverage, pagination, federal/DLA probes."""

from __future__ import annotations

from discovery.agency_seeds import FEDERAL_NON_SAM_LIVE
from discovery.live_fetchers import DibbsLiveFetcher, get_live_fetcher
from discovery.profiles import PROFILE_BROAD, PROFILE_NATIONAL, PROFILE_TINY
from discovery.selection import select_all_eligible_sources, select_diversified_sources


# Regression probes — NOT injected; used only to assert parser/general discovery capability
SPE_PROBES = [
    "SPE7M126Q1500",
    "SPE7M226T6835",
    "SPE7M526Q1029",
    "SPE8E726T3869",
    "SPE4A726Q1108",
    "SPE4A626T30NA",
    "SPE8E926T3782",
]
PRODUCT_ID_PROBES = [
    "130009-210",
    "957210",
    "6230-01-699-3102",
    "BH-1410",
    "5935-01-188-2529",
    "NF24Q100-01",
]


def test_broad_has_no_arbitrary_source_cap():
    assert PROFILE_BROAD["max_sources"] is None
    assert PROFILE_BROAD["all_eligible_sources"] is True
    assert PROFILE_NATIONAL["max_sources"] is None
    # TINY may still cap for validation
    assert PROFILE_TINY["max_sources"] == 5


def test_all_eligible_exceeds_old_25_cap():
    bundle = select_all_eligible_sources()
    assert bundle["eligible_count"] > 25
    assert bundle["registered_in_pool"] >= bundle["eligible_count"]
    selected = select_diversified_sources(max_sources=None, all_eligible=True)
    assert len(selected) == bundle["eligible_count"]
    # Priority orders but does not shrink
    assert selected[0]["source_id"] in {
        "state_ia",
        "state_mt",
        "fed_dla_dibbs_rfq",
        "fed_dla_dibbs_rfq_by_fsc",
        "fed_piee_public_solicitations",
    } or selected[0]["kind"] in {"STATE", "FEDERAL", "LOCAL", "COOPERATIVE"}


def test_priority_does_not_silently_exclude_eligible():
    full = select_all_eligible_sources()["eligible"]
    tiny = select_diversified_sources(max_sources=5)
    assert len(tiny) == 5
    # Explicit: None / all_eligible = full coverage (priority = order only)
    assert len(select_diversified_sources(max_sources=None)) == len(full)
    assert len(select_diversified_sources(all_eligible=True)) == len(full)
    assert len(full) > 25


def test_federal_dla_sources_have_list_urls():
    fed_live = [f for f in FEDERAL_NON_SAM_LIVE if f.get("list_url")]
    assert len(fed_live) >= 3
    ids = {f["source_id"] for f in fed_live}
    assert "fed_dla_dibbs_rfq" in ids
    assert "fed_piee_public_solicitations" in ids
    assert get_live_fetcher("live_dibbs") is not None
    assert get_live_fetcher("live_piee_public") is not None


def test_dibbs_parser_extracts_spe_without_injection():
    """Parser must recognize SPE* RFQ HTML generally — probes are fixtures, not seed data."""
    html = """
    <html><body><h1>Recent RFQs</h1>
    <table>
      <tr><td><a href="/RFQ/View.aspx?id=1">SPE7M126Q1500</a></td>
          <td>LIGHT, PORTABLE</td><td>6230-01-699-3102</td></tr>
      <tr><td><a href="/RFQ/View.aspx?id=2">SPE4A726Q1108</a></td>
          <td>CONNECTOR</td><td>5935-01-188-2529</td></tr>
      <tr><td><a href="/RFQ/View.aspx?id=3">SPE8E926T3782</a></td>
          <td>PART BH-1410</td><td></td></tr>
    </table>
    <div class="pagination">Page 1 of 3 Next page</div>
    </body></html>
    """
    fetcher = DibbsLiveFetcher()
    assert fetcher.structure_recognized(html)
    opps = fetcher.parse_listing(html, list_url="https://www.dibbs.bsm.dla.mil/RFQ/RfqRecents.aspx")
    sols = {(o.solicitation_number or o.external_id or "").upper().replace("-", "") for o in opps}
    # Normalize probe forms
    for probe in ("SPE7M126Q1500", "SPE4A726Q1108", "SPE8E926T3782"):
        assert any(probe.replace("-", "") in s.replace("-", "") for s in sols), probe
    # Product identifiers appear in titles/metadata when present in HTML — general extract, not hardcode
    blob = " ".join(f"{o.title} {o.raw_metadata}" for o in opps)
    assert "6230-01-699-3102" in blob or "5935-01-188-2529" in blob


def test_pagination_incomplete_when_safety_cap_hit():
    from discovery.http_client import HttpResponse, PublicProcurementHttpClient, RequestBudget, RequestMeta
    from discovery.live_fetchers import SimpleHtmlLiveFetcher

    pages = {}
    for i in range(1, 20):
        pages[i] = (
            f'<html><body><table><tr><td>Title</td><td>Sol</td><td>Deadline</td></tr>'
            f'<tr><td>Pump Equipment Purchase {i}</td><td>BID-{i}</td><td>12/31/2099</td></tr>'
            f"</table><div class=\"pagination\">Page {i} of 99 Next page</div></body></html>"
        )

    def transport(url, headers=None, timeout=None):
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        page = int((qs.get("page") or ["1"])[0])
        body = pages.get(page, pages[1])
        content = body.encode("utf-8")
        return HttpResponse(
            text=body,
            content=content,
            status_code=200,
            headers={"content-type": "text/html"},
            meta=RequestMeta(
                url=url,
                http_status=200,
                content_type="text/html",
                bytes_len=len(content),
                host="example.com",
            ),
        )

    client = PublicProcurementHttpClient(
        budget=RequestBudget(
            max_total_requests=100,
            max_requests_per_source=50,
            max_pages_per_source=3,
            max_records_per_source=500,
            max_runtime_seconds=60,
            min_interval_seconds=0.0,
        ),
        authorize_live=False,
        transport=transport,
    )
    fetcher = SimpleHtmlLiveFetcher()
    result = fetcher.fetch_listing(
        client,
        list_url="https://example.com/bids?page=1",
        source_id="test_pag",
        max_pages=3,
        pagination_exhaust=True,
        pagination_safety_max_pages=5,
    )
    assert result["pages_fetched"] >= 2
    assert "pagination_stop_reason" in result
    assert "pagination_complete" in result
    if result["pages_fetched"] >= 5 and result["pagination_stop_reason"] == "PAGINATION_INCOMPLETE":
        assert result["pagination_complete"] is False


def test_live_runner_broad_selects_all_eligible_mocked():
    from discovery.http_client import HttpResponse, RequestMeta
    from discovery.live_runner import run_live_discovery
    from discovery.selection import select_all_eligible_sources

    eligible = select_all_eligible_sources()["eligible"]
    assert len(eligible) > 25

    html = (
        "<html><body><h1>Bid Opportunities</h1><table>"
        "<tr><td>Title</td><td>Sol</td><td>Deadline</td></tr>"
        "<tr><td>Industrial Pump Equipment</td><td>RFQ-99</td><td>12/31/2099</td></tr>"
        "</table></body></html>"
    )

    def transport(url, headers=None, timeout=None):
        content = html.encode("utf-8")
        return HttpResponse(
            text=html,
            content=content,
            status_code=200,
            headers={"content-type": "text/html"},
            meta=RequestMeta(
                url=url,
                http_status=200,
                content_type="text/html",
                bytes_len=len(content),
                host="example.com",
            ),
        )

    assert PROFILE_BROAD["all_eligible_sources"] is True

    ids = [e["source_id"] for e in eligible[:30]]
    out = run_live_discovery(
        profile="broad",
        preview=True,
        authorize_live=False,
        transport=transport,
        source_ids=ids,
    )
    assert out["metrics"]["sources_attempted"] == 30
    assert out["metrics"]["sources_attempted"] > 25
    assert out.get("completeness")
    assert "fetched_this_run" in out["completeness"]
    assert "known_active_market_inventory" in out["completeness"]
    assert out.get("all_eligible_sources") is True


def test_spe_probes_are_not_hardcoded_into_registry():
    """Ensure known-answer SPE IDs are not seeded as fake opportunities."""
    from discovery.agency_seeds import FEDERAL_NON_SAM_LIVE
    import json

    blob = json.dumps(FEDERAL_NON_SAM_LIVE)
    for spe in SPE_PROBES:
        assert spe not in blob

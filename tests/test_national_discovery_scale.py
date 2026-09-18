"""National discovery scale — pagination path, coverage map, 25K fixture, BidNet network."""

from __future__ import annotations

from discovery.bidnet_network import all_bidnet_networks_enriched
from discovery.profiles import PROFILE_BROAD, PROFILE_NATIONAL
from discovery.selection import select_all_eligible_sources, _pool_map
from family_adapter_contract import (
    PAGINATION_PATH_PAGE,
    detect_pagination_model,
    next_page_url,
    normalize_bidnet_open_bids_url,
)
from national_procurement_coverage_map import (
    build_discovery_coverage_health,
    build_discovery_gap_queue,
    build_national_procurement_coverage_map,
    build_product_survivor_universe,
    build_source_yield_analytics,
)


def test_broad_national_caps_are_capacity_not_targets():
    assert PROFILE_BROAD["max_sources"] is None
    assert PROFILE_BROAD["max_records_total"] >= 25000
    assert PROFILE_BROAD["budget"].max_records_per_source >= 1000
    assert PROFILE_BROAD["budget"].max_pages_per_source >= 40
    assert PROFILE_BROAD["pagination_exhaust"] is True
    assert PROFILE_NATIONAL["max_records_total"] >= 25000
    assert PROFILE_NATIONAL["budget"].max_records_per_source >= 1000
    assert PROFILE_NATIONAL["pagination_safety_max_pages"] >= 60


def test_bidnet_path_pagination_detection_and_urls():
    html = """
    <html><body>
    <a href="/illinois/solicitations/open-bids/page1">1</a>
    <a href="/illinois/solicitations/open-bids/page2">Next page</a>
    <a href="/illinois/solicitations/open-bids/page16">16</a>
    </body></html>
    """
    base = "https://www.bidnetdirect.com/illinois/solicitations/open-bids"
    pag = detect_pagination_model(html, base)
    assert pag["model"] == PAGINATION_PATH_PAGE
    assert int(pag.get("max_page_hint") or 0) >= 16
    u2 = next_page_url(base, page=2, model=PAGINATION_PATH_PAGE)
    assert u2.endswith("/open-bids/page2")
    u3 = next_page_url(base, page=3, model=PAGINATION_PATH_PAGE)
    assert u3.endswith("/open-bids/page3")
    # Query ?page= must NOT be used for PATH_PAGE (silent duplicate trap)
    assert "?page=" not in (u2 or "")


def test_normalize_bidnet_open_bids_url():
    assert normalize_bidnet_open_bids_url("https://www.bidnetdirect.com/illinois") == (
        "https://www.bidnetdirect.com/illinois/solicitations/open-bids"
    )
    assert normalize_bidnet_open_bids_url(
        "https://www.bidnetdirect.com/florida/solicitations/open-bids/page2"
    ) == "https://www.bidnetdirect.com/florida/solicitations/open-bids"


def test_bidnet_network_in_eligible_pool():
    nets = all_bidnet_networks_enriched()
    assert len(nets) >= 50
    pool = _pool_map()
    assert "network_bidnet_illinois" in pool
    assert "open-bids" in pool["network_bidnet_illinois"]["list_url"]
    eligible = select_all_eligible_sources()["eligible_count"]
    assert eligible >= 120  # states + agencies + bidnet networks


def test_coverage_map_does_not_claim_statewide_from_network_only():
    per = {
        "network_bidnet_illinois": {"ok": True, "raw": 400},
        "agency_city_chicago_il": {"ok": True, "raw": 0, "source_stop_reason": "SHARED_LIST_URL"},
    }
    cmap = build_national_procurement_coverage_map(per_source=per)
    st = (cmap.get("geographic") or {}).get("states") or {}
    by = st.get("by_state") or {}
    assert by.get("IL") in {"LOCAL_ONLY", "PARTIAL_COVERAGE"}
    assert by.get("IL") != "STATEWIDE_SOURCE_VERIFIED"
    assert cmap.get("claim_percent_of_all_solicitations") is None


def test_coverage_map_statewide_when_state_portal_productive():
    per = {"state_ne": {"ok": True, "raw": 56}}
    cmap = build_national_procurement_coverage_map(per_source=per)
    by = ((cmap.get("geographic") or {}).get("states") or {}).get("by_state") or {}
    assert by.get("NE") == "STATEWIDE_SOURCE_VERIFIED"


def test_discovery_gap_queue_ranks_high_yield():
    gaps = build_discovery_gap_queue(
        per_source={"fed_dla_dibbs_rfq": {"ok": False, "source_stop_reason": "BOT_PROTECTED"}},
        limit=20,
    )
    assert gaps
    types = {g["gap_type"] for g in gaps}
    assert "STATE_WITHOUT_STATEWIDE_SOURCE" in types or "FEDERAL_COVERAGE_GAP" in types or "HIGH_VOLUME_PORTAL_BLOCKED" in types
    assert gaps[0]["expected_yield"] >= gaps[-1]["expected_yield"]


def test_product_survivor_universe_and_health():
    rows = [
        {"source_id": "a", "cheap_screen_class": "LIKELY_PRODUCT_RESALE"},
        {"source_id": "a", "cheap_screen_class": "CONSTRUCTION"},
        {"source_id": "b", "cheap_screen_class": "UNKNOWN"},
        {"source_id": "b", "cheap_screen_class": "MIXED"},
        {"source_id": "c", "product_classification": "CORE_PRODUCT"},
        {"source_id": "c", "product_classification": "PRODUCT_PLUS_SERVICE"},
    ]
    uni = build_product_survivor_universe(rows)
    assert uni["TOTAL_CURRENT_UNIQUE_DISCOVERED"] == 6
    assert uni["LIKELY_PRODUCT_RESALE"] == 2
    assert uni["PRODUCT_PLUS_MINOR_SERVICE"] == 1
    assert uni["CONSTRUCTION_DEFERRED"] == 1
    assert uni["UNKNOWN"] == 1
    health = build_discovery_coverage_health(funnel=uni)
    assert health["kind"] == "DISCOVERY_COVERAGE_HEALTH"
    assert health["national_percent_claim"] is None


def test_source_yield_analytics_sorts_by_volume():
    y = build_source_yield_analytics(
        per_source={
            "network_bidnet_illinois": {"ok": True, "raw": 400, "unique": 390},
            "state_ne": {"ok": True, "raw": 56, "unique": 56},
        }
    )
    assert y[0]["current_records"] >= y[1]["current_records"]


def test_bidnet_pagination_exhaustion_fixture():
    """Path pagination must accumulate beyond first-page 25-record trap."""
    from discovery.http_client import HttpResponse, PublicProcurementHttpClient, RequestBudget, RequestMeta
    from discovery.live_fetchers import BidNetLiveFetcher

    def page_html(n: int, total_pages: int = 5) -> str:
        rows = []
        for i in range(25):
            eid = 1000 + (n - 1) * 25 + i
            rows.append(
                f'<tr class="mets-table-row">'
                f'<td><a class="solicitation-link" href="/illinois/solicitations/statewide/{eid}/abstract">'
                f'Bid Item {eid} RFP-{eid}</a></td>'
                f'<td class="sol-closing-date"><span class="date-value">12/31/2099</span></td>'
                f'<td class="sol-region-item">Illinois Agency</td>'
                f"</tr>"
            )
        links = "".join(
            f'<a href="/illinois/solicitations/open-bids/page{p}">page {p}</a>'
            for p in range(1, total_pages + 1)
        )
        nxt = (
            f'<a href="/illinois/solicitations/open-bids/page{n + 1}">Next page</a>'
            if n < total_pages
            else ""
        )
        return f"<html><body>{''.join(rows)}{links}{nxt}</body></html>"

    def transport(url, headers=None, timeout=None):
        u = str(url)
        if "/page" in u:
            import re

            m = re.search(r"/page(\d+)", u)
            n = int(m.group(1)) if m else 1
        else:
            n = 1
        raw = page_html(n).encode("utf-8")
        return HttpResponse(
            text=raw.decode("utf-8"),
            content=raw,
            status_code=200,
            headers={},
            meta=RequestMeta(url=u, http_status=200, content_type="text/html", bytes_len=len(raw)),
        )

    client = PublicProcurementHttpClient(
        budget=RequestBudget(
            max_total_requests=50,
            max_requests_per_source=20,
            max_pages_per_source=10,
            max_records_per_source=500,
            max_runtime_seconds=30,
            min_interval_seconds=0.0,
        ),
        authorize_live=False,
        transport=transport,
    )
    result = BidNetLiveFetcher().fetch_listing(
        client,
        list_url="https://www.bidnetdirect.com/illinois/solicitations/open-bids",
        source_id="network_bidnet_illinois",
        max_pages=10,
        pagination_exhaust=True,
        pagination_safety_max_pages=20,
    )
    assert result["pages_fetched"] >= 3
    assert result["records_fetched"] >= 75  # not stuck at 25
    assert bool(result.get("beyond_page_1")) is True


def test_25k_scale_fixture_dedupe_and_backpressure():
    """Engineering scale: handle 25k candidates without deep research."""
    from m3_pipeline_store import M3PipelineStore
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "pipe.json"
    store = M3PipelineStore(path=tmp, durable=False)
    for i in range(25000):
        store.upsert_from_discovery(
            {
                "source_id": f"src_{i % 50}",
                "external_id": f"EXT-{i}",
                "title": f"Supply of widgets lot {i}",
                "solicitation_number": f"SOL-{i}",
                "status": "OPEN",
                "deadline_raw": "2099-12-31",
                "agency": f"Agency {i % 200}",
                "cheap_screen_class": "LIKELY_PRODUCT_RESALE" if i % 7 == 0 else "UNKNOWN",
            }
        )
    for i in range(2000):
        store.upsert_from_discovery(
            {
                "source_id": f"src_{i % 50}",
                "external_id": f"EXT-{i}-AMD",
                "title": f"Supply of widgets lot {i} AMENDMENT",
                "solicitation_number": f"SOL-{i}",
                "status": "OPEN",
                "deadline_raw": "2099-12-31",
                "agency": f"Agency {i % 200}",
                "notice_type": "AMENDMENT",
            }
        )
    rows = store.all()
    assert len(rows) >= 25000
    uni = build_product_survivor_universe(rows)
    assert uni["TOTAL_CURRENT_UNIQUE_DISCOVERED"] >= 25000
    survivors = uni["LIKELY_PRODUCT_RESALE"]
    assert survivors > 0
    assert len(rows) >= survivors


def test_shared_list_url_skip_in_runner():
    from discovery.http_client import HttpResponse, RequestMeta
    from discovery.live_runner import run_live_discovery

    html = (
        '<html><body><tr class="mets-table-row">'
        '<a class="solicitation-link" href="/illinois/solicitations/statewide/99/abstract">'
        "Industrial supplies RFP-99</a>"
        '<td class="sol-closing-date"><span class="date-value">12/31/2099</span></td>'
        "</tr>"
        '<a href="/illinois/solicitations/open-bids/page1">1</a>'
        "</body></html>"
    )
    hits = {"n": 0}

    def transport(url, headers=None, timeout=None):
        hits["n"] += 1
        raw = html.encode("utf-8")
        return HttpResponse(
            text=html,
            content=raw,
            status_code=200,
            headers={},
            meta=RequestMeta(url=str(url), http_status=200, content_type="text/html", bytes_len=len(raw)),
        )

    cands = [
        {
            "source_id": "network_bidnet_illinois",
            "name": "BidNet IL",
            "list_url": "https://www.bidnetdirect.com/illinois/solicitations/open-bids",
            "adapter_family": "live_bidnet",
            "platform_family": "BidNet",
            "kind": "NETWORK",
            "state_code": "IL",
        },
        {
            "source_id": "agency_city_chicago_il",
            "name": "Chicago",
            "list_url": "https://www.bidnetdirect.com/illinois/solicitations/open-bids",
            "adapter_family": "live_bidnet",
            "platform_family": "BidNet",
            "kind": "LOCAL",
            "state_code": "IL",
        },
    ]
    out = run_live_discovery(
        profile="tiny",
        authorize_live=False,
        transport=transport,
        candidates_override=cands,
        persist=False,
    )
    per = (out.get("metrics") or {}).get("per_source") or {}
    assert per.get("agency_city_chicago_il", {}).get("source_stop_reason") == "SHARED_LIST_URL"
    assert hits["n"] >= 1

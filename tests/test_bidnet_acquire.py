"""BidNet public acquisition — mocked transport only."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from m3.research import bidnet_acquire as ba

ROBOTS = """User-agent: *
Disallow: /private/
Disallow: /*?*page=
Crawl-delay: 5

User-agent: GPTBot
Disallow: /
"""


def _row(eid: int) -> str:
    return (f'<tr class="mets-table-row"><td><a href="/texas/solicitations/open-bids/x/{eid}?purchasingGroupId=1" '
            f'class="solicitation-link"><span class="rowTitle">Purchase of item number {eid}</span></a>'
            f'<span class="location">Texas</span><span class="closingDate">Closing <span class="dateValue">12/20/2026</span></span>'
            f'<span class="accessibility-hidden">{eid}</span></td></tr>')


def _page(n: int, per: int = 25, total: int = 60) -> str:
    start = (n - 1) * per
    rows = "".join(_row(1000000 + i) for i in range(start, min(start + per, total)))
    nxt = f'<a data-page-number="{n + 1}" data-href="/solicitations/open-bids/page{n + 1}">next</a>' if start + per < total else ""
    return f'<html><span class="simpleSolResultsNumResults">{total} results</span><table>{rows}</table>{nxt}</html>'


def _client(handler, sleeps):
    return ba.PoliteClient(transport=httpx.MockTransport(handler), sleep=sleeps.append)


def _router(pages: dict[str, httpx.Response]):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return pages.get(req.url.path) or httpx.Response(404)
    return handler, seen


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    s = SimpleNamespace(bidnet_public_enabled=True, bidnet_snapshot_root=lambda: tmp_path)
    monkeypatch.setattr(ba, "get_settings", lambda: s)
    return tmp_path


def test_robots_rules():
    rules = ba.parse_robots(ROBOTS, "M3ProcurementResearch")
    assert rules["crawl_delay"] == 5
    assert ba.robots_allows(rules, "https://www.bidnetdirect.com/solicitations/open-bids/page3")
    assert not ba.robots_allows(rules, "https://www.bidnetdirect.com/private/supplier/solicitations/search")
    assert not ba.robots_allows(rules, "https://www.bidnetdirect.com/solicitations/open-bids?page=3")


def test_disabled_by_default(tmp_path):
    assert ba.run_bidnet_public_enumeration(out_root=tmp_path)["error"] == "bidnet_public_disabled"


def test_full_enumeration_and_coverage(enabled):
    handler, seen = _router({"/solicitations/open-bids": httpx.Response(200, text=_page(1)),
                             "/solicitations/open-bids/page2": httpx.Response(200, text=_page(2)),
                             "/solicitations/open-bids/page3": httpx.Response(200, text=_page(3))})
    sleeps: list[float] = []
    res = ba.run_bidnet_public_enumeration(out_root=enabled, client=_client(handler, sleeps))
    assert res["complete"] and res["pages"] == 3
    cov = ba.finalize_bidnet_snapshot(out_root=enabled)
    assert cov["unique_records"] == 60 and cov["reported_total"] == 60 and cov["coverage_ratio"] == 1.0
    assert all(s >= 4.9 for s in sleeps if s)  # crawl delay respected between page requests
    assert not any(p.startswith("/private") for p in seen)


def test_stops_on_403_and_resumes(enabled):
    pages = {"/solicitations/open-bids": httpx.Response(200, text=_page(1)),
             "/solicitations/open-bids/page2": httpx.Response(403, text="<title>403 Forbidden</title>")}
    handler, _ = _router(pages)
    res = ba.run_bidnet_public_enumeration(out_root=enabled, client=_client(handler, []))
    assert res["stop_reason"] == "http_403" and res["pages"] == 1 and not res["complete"]
    pages["/solicitations/open-bids/page2"] = httpx.Response(200, text=_page(2))
    pages["/solicitations/open-bids/page3"] = httpx.Response(200, text=_page(3))
    handler, seen = _router(pages)
    res = ba.run_bidnet_public_enumeration(out_root=enabled, client=_client(handler, []))
    assert res["complete"] and res["pages"] == 3
    assert "/solicitations/open-bids" not in seen  # page 1 not refetched on resume


def test_incremental_stops_when_caught_up(enabled):
    pages = {"/solicitations/open-bids": httpx.Response(200, text=_page(1, total=125))}
    for n in range(2, 6):
        pages[f"/solicitations/open-bids/page{n}"] = httpx.Response(200, text=_page(n, total=125))
    handler, _ = _router(pages)
    assert ba.run_bidnet_public_enumeration(out_root=enabled, client=_client(handler, []))["pages"] == 5
    ba.finalize_bidnet_snapshot(out_root=enabled)
    handler, seen = _router(pages)
    res = ba.run_bidnet_public_enumeration(out_root=enabled, client=_client(handler, []), incremental=True)
    assert res["stop_reason"] == "caught_up_with_known_records" and res["pages"] == 3
    assert [p for p in seen if p != "/robots.txt"] == ["/solicitations/open-bids", "/solicitations/open-bids/page2",
                                                        "/solicitations/open-bids/page3"]
    assert ba.finalize_bidnet_snapshot(out_root=enabled)["unique_records"] == 125
    from m3.research import evidence_vault as ev
    page1 = ev.latest("bidnet", "listing:https://www.bidnetdirect.com/solicitations/open-bids", "listing_page",
                      root=enabled / "evidence_vault")
    assert page1["body"].decode("utf-8") == _page(1, total=125) and page1["status"] == 200


def test_challenge_page_stops(enabled):
    handler, _ = _router({"/solicitations/open-bids": httpx.Response(200, text="<title>Just a moment</title>")})
    res = ba.run_bidnet_public_enumeration(out_root=enabled, client=_client(handler, []))
    assert res["stop_reason"] == "challenge_page" and res["pages"] == 0


def test_pagination_repeat_stops(enabled):
    handler, _ = _router({"/solicitations/open-bids": httpx.Response(200, text=_page(1, total=100)),
                          "/solicitations/open-bids/page2": httpx.Response(200, text=_page(1, total=100))})
    res = ba.run_bidnet_public_enumeration(out_root=enabled, client=_client(handler, []))
    assert res["stop_reason"] == "pagination_repeat"


def test_short_titles_and_state_totals_parse():
    from m3.research.bidnet_parse import parse_search_results_html, reported_total_from_html

    row = ('<tr data-index="5" class="mets-table-row even"><td><a id="g_63" href="/public/supplier/solicitations/statewide/'
           '444245690760/abstract?purchasingGroupId=7&origin=1" class="solicitation-link mets-command-link">Fuel</a>'
           '<span class="sol-region-item">Arizona</span></td></tr>')
    rows = parse_search_results_html(row, list_url="https://www.bidnetdirect.com/arizona/solicitations/open-bids")
    assert [(r["provider_record_id"], r["title"]) for r in rows] == [("444245690760", "Fuel")]
    assert reported_total_from_html("<h2>243 Open Solicitations</h2>") == 243


def _state_page(slug: str, n: int, ids: list[int], last: bool, total: int = 0) -> str:
    rows = "".join(
        f'<tr class="mets-table-row odd"><td><a id="g_1" href="/public/supplier/solicitations/statewide/{i}/abstract?purchasingGroupId=1&origin=1" '
        f'class="solicitation-link mets-command-link">Purchase of widgets {i}</a><span class="sol-region-item">Texas</span>'
        f'<span class="sol-closing-date open"><span class="date-label">Closing</span><span class="date-value">12/16/2026</span></span>'
        f'</td></tr>' for i in ids)
    nxt = "" if last else f'<a data-page-number="{n + 1}" data-href="/{slug}/solicitations/open-bids/page{n + 1}">next</a>'
    return f"<html><h2>{total} Open Solicitations</h2><table>{rows}</table>{nxt}</html>"


def test_state_partitions_cover_beyond_national_cap(enabled, monkeypatch):
    tx1 = [444000000100 + i for i in range(25)]
    pages = {"/texas/solicitations/open-bids": httpx.Response(200, text=_state_page("texas", 1, tx1, False, 26)),
             "/texas/solicitations/open-bids/page2": httpx.Response(200, text=_state_page("texas", 2, [444000000003], True, 26)),
             "/utah/solicitations/open-bids": httpx.Response(200, text=_state_page("utah", 1, [tx1[0], 444000000009], True, 2))}
    handler, seen = _router(pages)
    monkeypatch.setattr(ba, "US_STATE_SLUGS", ("texas", "utah", "atlantis"))
    res = ba.run_bidnet_state_enumeration(out_root=enabled, states=("texas", "utah", "atlantis"), client=_client(handler, []))
    assert res["ok"] and res["states_complete"] == 2 and res["states_missing"] == ["atlantis"] and res["pages"] == 3
    cov = ba.finalize_bidnet_snapshot(out_root=enabled)
    # Listings walked but no provider total to reconcile against: not complete
    assert cov["unique_records"] == 27 and cov["state_listings_complete"] and not cov["enumeration_complete"]
    ck_p = enabled / "enumeration" / "checkpoint.json"
    for reported, complete in ((27, True), (30, False)):
        ck_p.write_text(json.dumps({"reported_total": reported, "complete": False, "completed_pages": []}), encoding="utf-8")
        assert ba.finalize_bidnet_snapshot(out_root=enabled)["enumeration_complete"] is complete
    snap = [json.loads(x) for x in (enabled / "snapshot.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(r["provider_record_id"] and r["deadline_raw"] == "12/16/2026" and r["location"] == "Texas" for r in snap)
    n = len(seen)
    again = ba.run_bidnet_state_enumeration(out_root=enabled, states=("texas", "utah", "atlantis"), client=_client(handler, []))
    assert again["pages"] == 3 and len(seen) == n + 1  # only robots.txt; finished states are not refetched


def test_state_run_stops_on_refusal(enabled):
    handler, _ = _router({"/texas/solicitations/open-bids": httpx.Response(429)})
    res = ba.run_bidnet_state_enumeration(out_root=enabled, states=("texas", "utah"), client=_client(handler, []))
    assert not res["ok"] and res["run_stop_reason"] == "http_429" and res["states_complete"] == 0


def test_private_urls_never_requested(enabled):
    handler, seen = _router({})
    c = _client(handler, [])
    with pytest.raises(ba.BidNetStop, match="out_of_scope"):
        c.get("https://www.bidnetdirect.com/private/solicitations/1/abstract")
    assert seen == ["/robots.txt"]


def test_abstract_fetch_resumable(enabled):
    html = ('<h1>Purchase of Fire Hose</h1><span class="mets-field-label">Closing Date</span>'
            '<div class="mets-field-body">12/20/2026</div>')
    handler, seen = _router({"/texas/solicitations/open-bids/x/1": httpx.Response(200, text=html)})
    recs = [{"provider_record_id": "1", "official_url": "https://www.bidnetdirect.com/texas/solicitations/open-bids/x/1"}]
    r1 = ba.fetch_public_abstracts(recs, out_root=enabled, client=_client(handler, []))
    r2 = ba.fetch_public_abstracts(recs, out_root=enabled, client=_client(handler, []))
    assert r1["fetched"] == 1 and r2["fetched"] == 0
    row = json.loads((enabled / "abstracts" / "abstracts.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["title"] == "Purchase of Fire Hose"

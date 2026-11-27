"""Free municipal/state portal adapters — list parse + accuracy fixtures."""

from __future__ import annotations

from m3.research.free_municipal_adapters import (
    _serviceish,
    discover_erie_ny_documents,
    parse_alaska_opn_listing_html,
    parse_cleveland_listing_html,
    parse_erie_ny_listing_html,
    parse_jefferson_tx_listing_html,
    parse_worcester_listing_html,
    search_alaska_opn,
    search_jefferson_tx,
)
from m3.research.package import detect_platform


ERIE_HTML = """
<table>
<tr><th>Type</th><th>Bid Number</th><th>Dept</th><th>Title</th><th>Desc</th><th>Due</th><th>Bid File</th><th>Addendum</th></tr>
<tr>
  <td>Commodity</td><td>260278-004</td><td>Parks</td><td>Golf Supplies</td><td>Golf Supplies</td><td>December 3, 2026</td>
  <td><a href="/purchasing/sites/www3.erie.gov.purchasing/files/bids/2026-11/260278-004-golf-supplies.pdf">Bid</a></td>
  <td></td>
</tr>
<tr>
  <td>Service</td><td>999-SVC</td><td>Facilities</td><td>Elevator Maintenance</td><td>Elevator Maintenance</td><td>January 1, 2027</td>
  <td><a href="/purchasing/sites/x/elevator.pdf">Bid</a></td>
  <td></td>
</tr>
</table>
"""

WORCESTER_HTML = """
<table>
<tr><th>Bid Number</th><th>Title</th><th>Due Date</th></tr>
<tr>
  <td><a href="/finance/purchasing-bids/bids/cr-8865-m7">CR-8865-M7</a></td>
  <td>Medical Gloves</td>
  <td>December 2, 2026</td>
</tr>
</table>
"""

CLEVELAND_HTML = """
<div>
  <p>File Number: 656 Description: Current Transformers Closing: December 10, 2026
  <a href="/sites/clevelandohio/files/fin/dps/bid/2026/656RFB2026.pdf">Bid Document</a>
  </p>
</div>
"""

JEFFERSON_HTML = """
<table>
<tr><th>Number</th><th>Title</th><th>Status</th><th>Docs</th></tr>
<tr>
  <td>26-079/MR</td>
  <td>Outboard Motors and Rigging Kits</td>
  <td>Bids Due December 2026</td>
  <td>
    <a href="/blobs/purchasing/nfb/BID/341.pdf">Bid</a>
    <a href="/blobs/purchasing/nfb/ADD/338.pdf">Addendum</a>
  </td>
</tr>
<tr>
  <td>26-100/XX</td>
  <td>Courthouse Reconstruction</td>
  <td>Bids Due January 2027</td>
  <td><a href="/blobs/purchasing/nfb/BID/999.pdf">Bid</a></td>
</tr>
</table>
"""

ALASKA_HTML = """
<a href="/OnlinePublicNotices/Notices/View.aspx?id=123">ITB 27-VSW-MYU-034 Materials for Flush Tank and Haul Systems</a>
<a href="/OnlinePublicNotices/Notices/View.aspx?id=456">Board Meeting Agenda</a>
<a href="/OnlinePublicNotices/Notices/View.aspx?id=789">RFP Construction Management Services Downtown</a>
"""


def test_detect_platform_municipal():
    assert detect_platform("https://www3.erie.gov/purchasing/retrieve-bids", "erie_ny") == "erie_ny"
    assert detect_platform("https://www.worcesterma.gov/finance/purchasing-bids/bids/cr-8865-m7", "worcester_ma") == "worcester_ma"
    assert detect_platform("https://www.clevelandohio.gov/x", "cleveland_oh") == "cleveland_oh"
    assert detect_platform("https://jeffersoncountytx.gov/Purchasing/NoticesForBid", "jefferson_tx") == "jefferson_tx"
    assert detect_platform(
        "https://aws.state.ak.us/OnlinePublicNotices/Notices/View.aspx?id=1", "alaska_opn"
    ) == "alaska_opn"


def test_parse_erie_listing():
    rows = parse_erie_ny_listing_html(ERIE_HTML)
    assert any(r["solicitation_number"] == "260278-004" for r in rows)
    golf = next(r for r in rows if r["solicitation_number"] == "260278-004")
    assert "golf-supplies.pdf" in golf["document_urls"][0]
    docs = discover_erie_ny_documents(golf["detail_url"], extra_urls=golf["document_urls"])
    assert docs and docs[0]["meta"]["platform"] == "erie_ny"


def test_parse_worcester_listing():
    rows = parse_worcester_listing_html(WORCESTER_HTML)
    assert rows[0]["solicitation_number"] == "CR-8865-M7"
    assert "cr-8865-m7" in rows[0]["detail_url"]


def test_parse_cleveland_listing():
    rows = parse_cleveland_listing_html(CLEVELAND_HTML)
    assert any(r["solicitation_number"] == "656" for r in rows)
    assert any("656RFB2026.pdf" in u for r in rows for u in r["document_urls"])


def test_jefferson_keeps_outboard_motors_accuracy_fixture(monkeypatch):
    """Outboard motors title looks product-ish — must stay in list for doc triage reject."""

    def _fake_fetch(url, timeout=45):  # noqa: ARG001
        return 200, "https://jeffersoncountytx.gov/Purchasing/NoticesForBid", JEFFERSON_HTML

    monkeypatch.setattr("m3.research.free_municipal_adapters.fetch_text", _fake_fetch)
    result = search_jefferson_tx({"source_key": "jefferson_tx", "config": {}}, limit=20)
    assert result["ok"]
    titles = [i.get("title") or "" for i in result["items"]]
    assert any("Outboard Motors" in t for t in titles)
    assert all("Reconstruction" not in t for t in titles)


def test_alaska_does_not_title_reject_materials_flush_tank(monkeypatch):
    """Materials/flush-tank ITB must not auto-skip on construction-ish wording in title path."""

    def _fake_fetch(url, timeout=40):  # noqa: ARG001
        if "View.aspx" in url:
            html = (
                "<h1>ITB 27-VSW-MYU-034 Materials for Flush Tank</h1>"
                '<a href="/OnlinePublicNotices/Notices/Attachment.aspx?id=165374">Solicitation</a>'
            )
            return 200, url, html
        return 200, "https://aws.state.ak.us/OnlinePublicNotices/Notices/Search.aspx?status=1", ALASKA_HTML

    monkeypatch.setattr("m3.research.free_municipal_adapters.fetch_text", _fake_fetch)
    rows = parse_alaska_opn_listing_html(ALASKA_HTML)
    assert any("Flush Tank" in r["title"] for r in rows)
    result = search_alaska_opn({"source_key": "alaska_opn", "config": {}}, limit=20, fetch_detail_docs=True)
    assert result["ok"]
    titles = " ".join(i.get("title") or "" for i in result["items"])
    assert "Flush Tank" in titles
    assert "Construction Management" not in titles
    # Title-level construction word alone is NOT enough to skip Alaska materials buys
    assert not _serviceish("Materials for Flush Tank and Haul Systems")


def test_free_source_specs_include_five():
    from m3.research.free_source_step_pipeline import FREE_SOURCE_SPECS

    for key in ("erie_ny", "worcester_ma", "cleveland_oh", "jefferson_tx", "alaska_opn"):
        assert key in FREE_SOURCE_SPECS
        assert callable(FREE_SOURCE_SPECS[key]["search"])
        assert callable(FREE_SOURCE_SPECS[key]["discover_docs"])

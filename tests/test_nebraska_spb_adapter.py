"""Nebraska State Purchasing Bureau free adapter — listing parse + doc discovery."""

from __future__ import annotations

from m3.research.adapters import parse_nebraska_spb_listing_html, search_nebraska_spb
from m3.research.package import detect_platform, discover_nebraska_spb_documents

SAMPLE_LIST = """
<html><body>
<h3>Current Bid Opportunities</h3>
<table>
<thead><tr>
<th>Posted</th><th>Description</th><th>Category - NIGP Code</th>
<th>Opening</th><th>Type</th><th>PCO/Buyer</th>
<th>Solicitation Number</th><th>Agency</th><th>Updated</th>
</tr></thead>
<tbody>
<tr>
  <td>11/23/26</td>
  <td><a href="/materiel/purchasing/127641 OR/127641 OR.html">Water Softener Salt</a></td>
  <td>77590 - Water Softener Salt</td>
  <td>01/5/27</td>
  <td>Invitation to Bid</td>
  <td>Brenda Sensibaugh and Vicki Collins</td>
  <td>127641 OR</td>
  <td>State Purchasing Bureau</td>
  <td>11/23/26</td>
</tr>
<tr>
  <td>10/16/26</td>
  <td><a href="/materiel/purchasing/127870 O5/127870 O5.html">InterRAI Assessment Evaluation</a></td>
  <td>95215 - Case Management</td>
  <td>12/09/26</td>
  <td>Request for Proposal</td>
  <td>Matthew Caddy</td>
  <td>127870 O5</td>
  <td>State Purchasing Bureau</td>
  <td>11/25/26</td>
</tr>
</tbody>
</table>
</body></html>
"""

SAMPLE_DETAIL = """
<html><body>
<h1>ITB 127641 OR</h1>
<p>PROJECT DESCRIPTION:</p>
<p>selecting a qualified Bidder to provide Water Softener Salt.</p>
<table>
<tr><td>Invitation to Bid:</td>
<td><a href="127641 OR_WATER SOFTENER SALT ITB DRAFT 11-18-26 BMS.docx">Word</a></td></tr>
<tr><td>Attachment A - Core List Bid Sheet:</td>
<td><a href="127641 OR Attachment A - Core List Bid Sheet11-18-26 bms.xlsx">Excel</a></td></tr>
<tr><td>Attachment B - Catalog List Bid Sheet:</td>
<td><a href="127641 OR Attachment B - Catalog List Bid Sheet.xlsx">Excel</a></td></tr>
</table>
</body></html>
"""


def test_parse_nebraska_spb_listing_rows():
    rows = parse_nebraska_spb_listing_html(SAMPLE_LIST)
    assert len(rows) == 2
    salt = next(r for r in rows if "127641" in (r.get("solicitation_number") or ""))
    assert salt["title"] == "Water Softener Salt"
    assert salt["bid_type"] == "Invitation to Bid"
    assert "127641 OR" in salt["detail_url"]


def test_nebraska_spb_document_discovery():
    base = "https://das.nebraska.gov/materiel/purchasing/127641%20OR/127641%20OR.html"
    docs = discover_nebraska_spb_documents(base, SAMPLE_DETAIL)
    assert len(docs) == 3
    names = " ".join(d["source_url"] for d in docs).lower()
    assert ".docx" in names
    assert ".xlsx" in names
    assert detect_platform(base, "nebraska_spb") == "nebraska_spb"


def test_search_nebraska_spb_live_water_softener_salt():
    """Live smoke — free listing; no NextBid credits."""
    source = {
        "source_key": "nebraska_spb",
        "config": {
            "list_url": "https://das.nebraska.gov/materiel/bid-opportunities.html",
            "geo_state": "NE",
        },
    }
    result = search_nebraska_spb(source, limit=40, fetch_detail_docs=True)
    assert result.get("ok") is True
    items = result.get("items") or []
    assert result.get("rows_examined", 0) > 0
    salt = [i for i in items if "127641" in str(i.get("solicitation_number") or "")]
    assert salt, f"Water Softener Salt ITB 127641 not in accepted items; accepted={len(items)}"
    one = salt[0]
    assert one["geo_state"] == "NE"
    assert one["documents_available"] == "yes"
    assert len(one.get("document_urls") or []) >= 3
    assert "salt" in (one.get("title") or "").lower()

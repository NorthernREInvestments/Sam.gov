"""TN Tech / Kent State / Tulsa / Columbus GA free adapters."""

from __future__ import annotations

from m3.research.free_municipal_adapters import (
    parse_columbus_ga_listing_html,
    parse_kent_listing_html,
    parse_tntech_listing_html,
    parse_tulsa_listing_html,
    search_tntech,
    search_tulsa_ok,
)
from m3.research.package import detect_platform


TNTECH_HTML = """
<table>
<tr><th>Due Date</th><th>Description</th><th>Purchasing Contact</th></tr>
<tr>
  <td>12/19/2026</td>
  <td><a href="/purchasing/pdf/bid_opportunities/RFQ_Electrochemical_Teaching_Lab_Equipment.pdf">Electrochemical Teaching Lab Equipment</a></td>
  <td>Tina</td>
</tr>
</table>
"""

KENT_HTML = """
<table>
<tr><th>DOCUMENT</th><th>DESCRIPTION</th><th>ISSUED</th><th>DUE DATE</th><th>CONTRACT MANAGER</th><th>SUBMIT</th></tr>
<tr>
  <td><a href="https://www-s3-live.kent.edu/s3fs-root/s3fs-public/file/RFP2054.pdf?VersionId=abc123">RFP #2054 (PDF)</a>
      <a href="https://www.kent.edu/node/1027724">Questions and Answers</a></td>
  <td>Selection of an S-band Ground Station Kit Vendor</td>
  <td>November 13, 2026</td>
  <td>December 7, 2026 4:30pm ET</td>
  <td>Danielle</td>
  <td><a href="https://dynamicforms.ngwebsolutions.com/Submit/Start/x?SSO=N">Respond</a></td>
</tr>
</table>
"""

TULSA_HTML = """
<table>
<tr><th>Bid # (Addendum)</th><th>Response Deadline</th><th>Description</th></tr>
<tr>
  <td><a href="/government/departments/finance/selling-to-the-city/bid-opportunities-and-results/ifb-27-710-commercial-grade-laundry-equipment/">IFB 27-710 (0)</a></td>
  <td>12/2/2026</td>
  <td>Commercial Grade Laundry Equipment</td>
</tr>
<tr>
  <td><a href="/government/departments/finance/selling-to-the-city/bid-opportunities-and-results/csp-tac-186g-multi-function-device-rental-services/">CSP TAC 186G (0)</a></td>
  <td>12/9/2026</td>
  <td>Multi-Function Device Rental Services</td>
</tr>
</table>
"""

COLUMBUS_HTML = """
<table>
<tr><th>Solicitation</th><th>Description</th><th>Due Date</th></tr>
<tr>
  <td>RFB No. 27-0001</td>
  <td>Leather Items Buyer: X</td>
  <td>December 30, 2026</td>
  <td></td>
</tr>
"""


def test_detect_platforms():
    assert detect_platform("https://www.tntech.edu/purchasing/bidopportunities.php", "tntech") == "tntech"
    assert detect_platform("https://www.kent.edu/procurement/bids", "kent_state") == "kent_state"
    assert detect_platform("https://www.cityoftulsa.org/media/1.pdf", "tulsa_ok") == "tulsa_ok"
    assert detect_platform("https://columbusga.gov/finance/bid-opportunities", "columbus_ga") == "columbus_ga"


def test_tntech_deadline_requires_confirmation(monkeypatch):
    def _fake(url, timeout=35):  # noqa: ARG001
        return 200, "https://www.tntech.edu/purchasing/bidopportunities.php", TNTECH_HTML

    monkeypatch.setattr("m3.research.free_municipal_adapters.fetch_text", _fake)
    rows = parse_tntech_listing_html(TNTECH_HTML)
    assert rows[0]["listing_deadline"] == "12/19/2026"
    result = search_tntech({"source_key": "tntech", "config": {}})
    assert result["ok"]
    item = result["items"][0]
    assert item["deadline_requires_document_confirmation"] is True
    assert "Electrochemical" in item["title"]
    assert any("RFQ_Electrochemical" in u for u in item["document_urls"])


def test_kent_preserves_version_and_skips_dynamicforms():
    rows = parse_kent_listing_html(KENT_HTML)
    assert rows[0]["solicitation_number"] == "RFP2054"
    assert "VersionId=abc123" in rows[0]["document_urls"][0]
    assert any("node/1027724" in u for u in rows[0]["document_urls"])
    assert all("dynamicforms" not in u for u in rows[0]["document_urls"])


def test_tulsa_skips_rental_services(monkeypatch):
    def _fake(url, timeout=40):  # noqa: ARG001
        if "laundry" in url:
            html = '<a href="/media/31988/ifb-27-710-commercial-grade-laundry-equipment.pdf">PDF</a> mandatory pre-bid conference'
            return 200, url, html
        return 200, "https://www.cityoftulsa.org/?t=current", TULSA_HTML

    monkeypatch.setattr("m3.research.free_municipal_adapters.fetch_text", _fake)
    rows = parse_tulsa_listing_html(TULSA_HTML)
    assert any("27-710" in r["solicitation_number"] for r in rows)
    result = search_tulsa_ok({"source_key": "tulsa_ok", "config": {}}, fetch_detail_docs=True)
    titles = [i.get("title") or "" for i in result["items"]]
    assert any("Laundry" in t for t in titles)
    assert all("Rental Services" not in t for t in titles)
    laundry = next(i for i in result["items"] if "Laundry" in (i.get("title") or ""))
    assert laundry.get("eligibility_restriction") == "mandatory_prebid_or_site_visit"


def test_columbus_flags_broken_addendum_links():
    html = """
    <table>
    <tr><th>Solicitation</th><th>Description</th><th>Due Date</th></tr>
    <tr>
      <td>RFB No. 27-0013</td>
      <td>Landscape Materials (Annual Contract) Addendum No. 1</td>
      <td>December 16, 2026</td>
    </tr>
    </table>
    """.replace(
        "</tr>\n    </table>",
        """<td>
        <a href="/Portals/finance/adam/Content/x/SolicitationDocument/RFB No. 27-0013 Landscape Materials.pdf">RFB</a>
        <a href="/desktopmodules/tosic_sexycontent/dist/ng-edit/Addendum No. 1">Addendum No. 1</a>
      </td></tr></table>""",
    )
    # rebuild properly
    html = """
    <table>
    <tr><th>Solicitation</th><th>Description</th><th>Due Date</th></tr>
    <tr>
      <td>RFB No. 27-0013</td>
      <td>Landscape Materials (Annual Contract) Addendum No. 1
        <a href="/Portals/finance/adam/Content/x/SolicitationDocument/RFB%20No.%2027-0013%20Landscape%20Materials.pdf">doc</a>
        <a href="/desktopmodules/tosic_sexycontent/dist/ng-edit/Addendum No. 1">Addendum No. 1</a>
      </td>
      <td>December 16, 2026</td>
    </tr>
    <tr>
      <td>RFB No. 27-0001</td>
      <td>Leather Items
        <a href="/Portals/finance/adam/Content/y/SolicitationDocument/RFB%2027-0001%20Leather%20Items%20.pdf">doc</a>
      </td>
      <td>December 30, 2026</td>
    </tr>
    </table>
    """
    rows = parse_columbus_ga_listing_html(html)
    landscape = next(r for r in rows if "27-0013" in r["solicitation_number"])
    assert landscape["broken_document_links"]
    leather = next(r for r in rows if "27-0001" in r["solicitation_number"])
    assert not leather["broken_document_links"]


def test_free_source_specs_include_four():
    from m3.research.free_source_step_pipeline import FREE_SOURCE_SPECS

    for key in ("tntech", "kent_state", "tulsa_ok", "columbus_ga"):
        assert key in FREE_SOURCE_SPECS

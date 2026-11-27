"""Pennsylvania eMarketplace + Louisiana LaPAC adapters."""

from __future__ import annotations

from m3.research.free_additional_adapters import (
    parse_lapac_segment_html,
    search_louisiana_lapac,
    search_pa_emarketplace,
)
from m3.research.package import detect_platform


LA_HTML = """
<table>
<tr><th>Bid Number</th><th>Description</th><th>Date Issued</th><th>Bid Open Date/Time</th><th>Help</th></tr>
<tr>
  <td><a href="/osp/lapac/agency/pdf/9089600.pdf">3000026848</a></td>
  <td>Pharma., Hygiene and Grooming supplies</td>
  <td>11/10/2026</td>
  <td>12/17/2026 10:00:00 AM CT</td>
  <td>107001</td>
</tr>
<tr>
  <td><a href="/osp/lapac/agency/pdf/9089601-00.pdf">3000026848-1</a></td>
  <td>Addendum NO. 01 - Extending Deadline</td>
  <td>11/25/2026</td>
</tr>
<tr>
  <td><a href="/osp/lapac/agency/pdf/9082400.pdf">3000026792</a></td>
  <td>*Mand Site*Waste Disposal Svcs -DOC/LCIW</td>
  <td>10/23/2026</td>
  <td>11/24/2026 10:00:00 AM CT</td>
  <td>x</td>
</tr>
</table>
"""


def test_detect_pa_la_platforms():
    assert detect_platform("https://www.emarketplace.state.pa.us/Search.aspx", "pa_emarketplace") == "pa_emarketplace"
    assert (
        detect_platform(
            "https://wwwcfprd.doa.louisiana.gov/osp/lapac/agency/pdf/8981200.pdf",
            "louisiana_lapac",
        )
        == "louisiana_lapac"
    )


def test_parse_lapac_groups_addenda():
    rows = parse_lapac_segment_html(
        LA_HTML, list_url="https://wwwcfprd.doa.louisiana.gov/osp/lapac/dspBid.cfm"
    )
    assert any(r["solicitation_number"] == "3000026848" for r in rows)
    pharma = next(r for r in rows if r["solicitation_number"] == "3000026848")
    assert len(pharma["document_urls"]) >= 2
    assert pharma.get("has_amendment") is True


def test_pa_search_filters_non_numeric_and_loads_docs(monkeypatch):
    rows = [
        {
            "Bid No": "COSTARS 39",
            "Bid Type": "RFI",
            "Title": "Voting Systems",
            "Description": "forum",
            "Agency": "DGS",
            "Bid Open Date": "12/31/9999",
            "Status": "Extended",
        },
        {
            "Bid No": "6100067016",
            "Bid Type": "IFB",
            "Title": "POLK CENTER COAL",
            "Description": "250 tons bituminous",
            "Agency": "Department of Corrections",
            "Bid Open Date": "12/11/2026 2:00:00 PM",
            "Status": "Open",
        },
    ]

    def _fake_csv(*, ad_type="10"):  # noqa: ARG001
        return "https://www.emarketplace.state.pa.us/Search.aspx", rows

    def _fake_docs(sid: str):
        assert sid == "6100067016"
        return (
            [
                "https://www.emarketplace.state.pa.us/FileDownload.aspx?file=6100067016/Solicitation_2.pdf"
            ],
            {"public_filedownload_count": 1, "login_referenced_links": 2, "package_incomplete": True},
        )

    monkeypatch.setattr("m3.research.free_additional_adapters._pa_search_csv", _fake_csv)
    monkeypatch.setattr("m3.research.free_additional_adapters._pa_detail_docs", _fake_docs)
    result = search_pa_emarketplace({"source_key": "pa_emarketplace", "config": {}}, limit=10)
    assert result["ok"]
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["solicitation_number"] == "6100067016"
    assert item["package_status_hint"] == "INCOMPLETE_OR_LOGIN_REFS"
    assert item["document_urls"]


def test_specs_include_pa_la():
    from m3.research.free_source_step_pipeline import FREE_SOURCE_SPECS

    assert "pa_emarketplace" in FREE_SOURCE_SPECS
    assert "louisiana_lapac" in FREE_SOURCE_SPECS

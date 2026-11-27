"""Batch adapters: Providence, San Juan, Pocatello, Indiana ZIP, etc."""

from __future__ import annotations

import io
import zipfile

from m3.research.free_additional_adapters import (
    apply_quantity_amendment_overrides,
    inventory_indiana_zip_bytes,
    parse_indiana_listing_html,
    parse_pocatello_listing_html,
    parse_providence_listing_html,
    parse_sanjuan_listing_html,
)
from m3.research.package import detect_platform


def test_detect_new_platforms():
    assert detect_platform("https://www.providenceri.gov/x", "providence_ri") == "providence_ri"
    assert detect_platform("https://www.sanjuan.edu/x", "sanjuan_usd") == "sanjuan_usd"
    assert detect_platform("https://www.sd25.us/x", "pocatello_sd25") == "pocatello_sd25"
    assert detect_platform("https://www.in.gov/idoa/proc/solicitations/files/1.zip", "indiana_idoa") == "indiana_idoa"
    assert detect_platform("https://evp.nc.gov/solicitations/", "nc_evp") == "nc_evp"


def test_providence_groups_addenda():
    html = """
    <a href="/wp-content/uploads/2026/11/PVD26-123-Public-Property-RFP-Narducci-Learning-Center-Temporary-Modular-Trailer-Units-MT55534-1.pdf">Narducci Learning Center, Temporary Modular Trailer Units</a>
    <a href="/wp-content/uploads/2026/11/Public-Property-Narducci-Learning-Center-Temporary-Modular-Trailer-Units-MT-55534-Addendum-1.pdf">Addendum 1</a>
    <a href="/wp-content/uploads/2026/11/PVD26-100-HHS-RFP-HOME-ARP-Technical-Assistance-MT-54829.pdf">Home-Arp Technical Assistance Services</a>
    """
    rows = parse_providence_listing_html(html)
    narducci = next(r for r in rows if "55534" in r["solicitation_number"])
    assert len(narducci["document_urls"]) == 2


def test_sanjuan_switch_equipment_and_erate():
    html = """
    <h3>RFP #27-108 Switch Equipment Erate</h3>
    <a href="/fs/resource-manager/view/71b98960-9145-4af1-8652-7c9db8ceba4b">RFP #27-108 Switch Equipment Proposal</a>
    <a href="/fs/resource-manager/view/95bf53ad-6bf0-4b54-9bb3-56a522253992">Switch Exhibit Pricing Sheet</a>
    <a href="/fs/resource-manager/view/f28b0572-0271-4f26-a0e4-b812d0e2adc5">E-rate #27-108 Questions and Answers</a>
    """
    rows = parse_sanjuan_listing_html(html)
    sw = next(r for r in rows if "27-108" in r["solicitation_number"])
    assert len(sw["document_urls"]) >= 2
    assert sw["erate"] is True


def test_pocatello_quantity_amendment_accuracy_fixture():
    """Custodial YY0001 must use amended 40 cases, not original 20,000."""
    amendment = """
    Item Number YY0001 Quantity Case Products per case 500
    The quantity for item YY0001 is hereby changed from 20,000 to 40.
    Quantity: 40
    """
    result = apply_quantity_amendment_overrides(
        item_number="YY0001",
        original_qty=20000,
        amendment_text=amendment,
    )
    assert result["amendment_applied"] is True
    assert result["effective_quantity"] == 40
    assert result["original_quantity"] == 20000


def test_pocatello_groups_custodial_package():
    html = """
    <a href="https://resources.finalsite.net/images/v1/d25k12idus/a/2026CustodialSuppliesInvitationtoBid.pdf">2026 Custodial Supplies, Invitation to Bid</a>
    <a href="https://resources.finalsite.net/images/v1/d25k12idus/b/Addendum2CustodialSupplies2026.pdf">Addendum 2,Custodial Supplies 2026</a>
    <a href="https://resources.finalsite.net/images/v1/d25k12idus/c/CompletedFY27BudgetCombinedRevExp.pdf">FY27 Budget</a>
    """
    rows = parse_pocatello_listing_html(html)
    assert any("Custodial" in r["title"] for r in rows)
    assert all("Budget" not in r["title"] for r in rows)


def test_indiana_zip_inventory_and_listing_parse():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("000610000088646/Negotiated Bid Main Document 27-88646.docx", b"PKDOC")
        zf.writestr("000610000088646/Att D - Cost Proposal Template (9).xlsx", b"PKXLS")
    members = inventory_indiana_zip_bytes(buf.getvalue())
    assert len(members) == 2
    html = """
    <table>
    <tr><th>Event Name</th><th>Agency</th><th>Event ID</th><th>Event Description</th><th>Response Due By</th><th>Contact</th></tr>
    <tr>
      <td>Pool and Water Chemicals Bid Documents</td>
      <td>Dept of Administration</td>
      <td>000610000088646</td>
      <td>Pool and Water Chemicals</td>
      <td>12/21/2026 3:00:00PM EST</td>
      <td>Buyer</td>
      <td><a href="/idoa/proc/solicitations/files/000610000088646.zip">Bid Documents</a></td>
    </tr>
    </table>
    """
    # parser expects 5+ cells before zip link in same row — rebuild with zip in row
    html = """
    <table>
    <tr><th>Event Name</th><th>Agency</th><th>Event ID</th><th>Event Description</th><th>Response Due By</th><th>Contact</th></tr>
    <tr>
      <td>Pool and Water Chemicals Bid Documents</td>
      <td>Dept of Administration</td>
      <td>000610000088646</td>
      <td>Pool and Water Chemicals solicitation</td>
      <td>12/21/2026 3:00:00PM EST</td>
      <td><a href="/idoa/proc/solicitations/files/000610000088646.zip">Bid Documents</a></td>
    </tr>
    </table>
    """
    rows = parse_indiana_listing_html(html)
    assert rows and rows[0]["solicitation_number"] == "000610000088646"
    assert rows[0]["document_urls"][0].endswith(".zip")


def test_free_specs_include_batch_and_hold_nc():
    from m3.research.free_source_step_pipeline import FREE_SOURCE_SPECS
    from m3.research.source_allowlist import owner_approved_entry

    for key in (
        "providence_ri",
        "sanjuan_usd",
        "pocatello_sd25",
        "danville_va",
        "papillion_plcs",
        "oceanview_ovsd",
        "msu_procurement",
        "indiana_idoa",
    ):
        assert key in FREE_SOURCE_SPECS
    # Delaware already wired; NC held off live
    assert "delaware_open_bids" in FREE_SOURCE_SPECS
    assert "nc_evp" not in FREE_SOURCE_SPECS
    entry = owner_approved_entry("nc_evp") or {}
    assert entry.get("approved") is True
    assert entry.get("live_acquisition") is False

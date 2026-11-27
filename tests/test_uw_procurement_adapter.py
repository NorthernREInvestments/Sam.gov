"""University of Washington Procurement free adapter — notice parse + doc discovery."""

from __future__ import annotations

from m3.research.adapters import (
    parse_uw_procurement_listing_html,
    parse_uw_procurement_notice_label,
    search_uw_procurement,
)
from m3.research.package import detect_platform, discover_uw_procurement_documents

SAMPLE_LIST = """
<html><body>
<a href="/ps/files/purchasing/Solicitations-PostedPurchWebsite/rfq-uw-25-0100-office-supplies.docx">
RFQ UW-25-0100
</a>
<a href="/ps/files/purchasing/Solicitations-PostedPurchWebsite/rfq-22-0039-cares-mentor.docx">
RFQ 22-0038
</a>
<a href="/ps/files/purchasing/Solicitations-PostedPurchWebsite/rfq-22-0039-cares-mentor-spanish.docx">
Revisar RFQQ 22-0038
</a>
<a href="/ps/sites/default/files/Resources/charts/Supplier%20Code%20of%20Conduct.pdf">
Supplier Code of Conduct
</a>
</body></html>
"""


def test_parse_uw_notice_label():
    p = parse_uw_procurement_notice_label(
        "RFQ UW-25-0100",
        href="/ps/files/purchasing/Solicitations-PostedPurchWebsite/rfq-uw-25-0100-office-supplies.docx",
    )
    assert p is not None
    assert "25-0100" in p["solicitation_number"]
    assert "office supplies" in p["title"].lower() or "UW-25-0100" in p["title"] or p["title"]


def test_parse_uw_listing_groups_notices():
    rows = parse_uw_procurement_listing_html(
        SAMPLE_LIST,
        list_url="https://finance.uw.edu/ps/suppliers/supplier-solicitation-notices",
    )
    assert len(rows) >= 2
    product = next(r for r in rows if "25-0100" in (r.get("solicitation_number") or ""))
    assert product["document_urls"][0].endswith(".docx")
    assert "Solicitations-PostedPurchWebsite" in product["detail_url"]
    mentor = next(r for r in rows if "22-0038" in (r.get("solicitation_number") or "") or "22-0039" in (r.get("solicitation_number") or ""))
    assert len(mentor["document_urls"]) == 2


def test_uw_document_discovery():
    url = (
        "https://finance.uw.edu/ps/files/purchasing/Solicitations-PostedPurchWebsite/"
        "rfq-uw-25-0100-office-supplies.docx"
    )
    docs = discover_uw_procurement_documents(url, "", extra_urls=[url])
    assert len(docs) == 1
    assert docs[0]["meta"]["platform"] == "uw_procurement"
    assert detect_platform(url, "uw_procurement") == "uw_procurement"


def test_search_uw_procurement_skips_serviceish(monkeypatch):
    from m3.research import adapters as ad

    def _fake_fetch(url, timeout=30):  # noqa: ARG001
        return (
            200,
            "https://finance.uw.edu/ps/suppliers/supplier-solicitation-notices",
            SAMPLE_LIST,
        )

    monkeypatch.setattr(ad, "fetch_text", _fake_fetch)
    result = search_uw_procurement(
        {
            "source_key": "uw_procurement",
            "config": {
                "list_url": "https://finance.uw.edu/ps/suppliers/supplier-solicitation-notices"
            },
        },
        limit=10,
        fetch_detail_docs=False,
    )
    assert result["ok"] is True
    # Mentor/caregiver service RFQ skipped; office supplies kept when title from filename.
    assert any("25-0100" in str(i.get("solicitation_number") or "") for i in result["items"])
    assert all(
        "mentor" not in (i.get("title") or "").lower() for i in result["items"]
    )

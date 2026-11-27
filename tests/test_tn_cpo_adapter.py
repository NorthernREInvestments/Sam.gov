"""Tennessee CPO free adapter — RFP package parse + doc discovery."""

from __future__ import annotations

from m3.research.adapters import parse_tn_cpo_listing_html, search_tn_cpo
from m3.research.package import detect_platform, discover_tn_cpo_documents

SAMPLE_LIST = """
<html><body>
<a href="/content/dam/tn/generalservices/documents/cpo/rfp-updates/40100-51900/RFP_40100-51900_Employee_Safety_Footwear_2.pdf">RFP 40100-51900</a>
<a href="/content/dam/tn/generalservices/documents/cpo/rfp-updates/40100-51900/Amendment_1%20_Schedule_Change.pdf">Amendment 1</a>
<a href="/content/dam/tn/generalservices/documents/cpo/rfp-updates/32901-31409/RFP_32901-31409_Inmate-Health_Services.pdf">RFP 32901-31409</a>
<a href="/content/dam/tn/generalservices/documents/cpo/rfp-updates/32901-31409/Attachment_2_Model_Performance_Bond.pdf">Attachment 2</a>
</body></html>
"""


def test_parse_tn_cpo_groups_packages():
    rows = parse_tn_cpo_listing_html(
        SAMPLE_LIST,
        list_url="https://www.tn.gov/generalservices/procurement/",
    )
    assert len(rows) == 2
    fw = next(r for r in rows if r["solicitation_number"] == "40100-51900")
    assert "Footwear" in fw["title"] or "FOOTWEAR" in fw["title"].upper()
    assert len(fw["document_urls"]) == 2


def test_tn_document_discovery():
    url = (
        "https://www.tn.gov/content/dam/tn/generalservices/documents/cpo/"
        "rfp-updates/40100-51900/RFP_40100-51900_Employee_Safety_Footwear_2.pdf"
    )
    docs = discover_tn_cpo_documents(
        url,
        "",
        extra_urls=[
            url,
            "https://www.tn.gov/content/dam/tn/generalservices/documents/cpo/rfp-updates/40100-51900/Amendment_1.pdf",
        ],
    )
    assert len(docs) == 2
    assert docs[0]["meta"]["platform"] == "tn_cpo"
    assert detect_platform(url, "tn_cpo") == "tn_cpo"


def test_search_tn_cpo_skips_serviceish(monkeypatch):
    from m3.research import adapters as ad

    def _fake_fetch(url, timeout=30):  # noqa: ARG001
        return 200, "https://www.tn.gov/rfp", SAMPLE_LIST

    monkeypatch.setattr(ad, "fetch_text", _fake_fetch)
    result = search_tn_cpo(
        {"source_key": "tn_cpo", "config": {"list_url": "https://www.tn.gov/rfp"}},
        limit=10,
        fetch_detail_docs=False,
    )
    assert result["ok"] is True
    titles = " ".join(i.get("title") or "" for i in result["items"]).upper()
    assert "FOOTWEAR" in titles
    assert "INMATE" not in titles

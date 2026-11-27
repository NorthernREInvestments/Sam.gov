"""New Jersey Purchase Bureau free adapter — notice PDF parse + doc discovery."""

from __future__ import annotations

from m3.research.adapters import (
    parse_nj_purchase_listing_html,
    parse_nj_purchase_notice_label,
    search_nj_purchase,
)
from m3.research.package import detect_platform, discover_nj_purchase_documents

SAMPLE_LIST = """
<html><body>
<a href="/treasury/purchase/specialnotices/NJDPP-26DPP01243-salt.pdf">
NOTICE OF BID SOLICITATION: 26DPP01243 - T0213-ROCK SALT FOR ROADWAYS, VARIOUS TYPES, BULK AND BAGGED
</a>
<a href="/treasury/purchase/specialnotices/NJDPP-26DPP01253-amend.pdf">
NOTICE OF BID SOLICITATION - BID AMENDMENT: 26DPP01253 - T1628 ENHANCED MOTOR VEHICLE INSPECTION MAINTENANCE
</a>
<a href="/treasury/purchase/pdf/doingbusinessinnj.pdf">Doing Business with the State of New Jersey</a>
</body></html>
"""


def test_parse_nj_notice_label():
    p = parse_nj_purchase_notice_label(
        "NOTICE OF BID SOLICITATION: 26DPP01243 - T0213-ROCK SALT FOR ROADWAYS, BULK"
    )
    assert p is not None
    assert p["solicitation_number"] == "26DPP01243"
    assert "ROCK SALT" in p["title"]
    assert p["is_amendment"] is False


def test_parse_nj_listing_groups_notices():
    rows = parse_nj_purchase_listing_html(
        SAMPLE_LIST, list_url="https://www.nj.gov/treasury/purchase/"
    )
    assert len(rows) == 2
    salt = next(r for r in rows if r["solicitation_number"] == "26DPP01243")
    assert "ROCK SALT" in salt["title"]
    assert salt["document_urls"][0].endswith(".pdf")
    assert "specialnotices" in salt["detail_url"]


def test_nj_document_discovery():
    url = "https://www.nj.gov/treasury/purchase/specialnotices/NJDPP-26DPP01243-salt.pdf"
    docs = discover_nj_purchase_documents(url, "", extra_urls=[url])
    assert len(docs) == 1
    assert docs[0]["meta"]["platform"] == "nj_purchase"
    assert detect_platform(url, "nj_purchase") == "nj_purchase"


def test_search_nj_purchase_parses_live_or_sample(monkeypatch):
    from m3.research import adapters as ad

    def _fake_fetch(url, timeout=30):  # noqa: ARG001
        return 200, "https://www.nj.gov/treasury/purchase/", SAMPLE_LIST

    monkeypatch.setattr(ad, "fetch_text", _fake_fetch)
    result = search_nj_purchase(
        {"source_key": "nj_purchase", "config": {"list_url": "https://www.nj.gov/treasury/purchase/"}},
        limit=10,
        fetch_detail_docs=False,
    )
    assert result["ok"] is True
    # Serviceish motor-vehicle inspection maintenance is skipped; salt kept.
    assert any("SALT" in (i.get("title") or "").upper() for i in result["items"])
    assert all("INSPECTION MAINTENANCE" not in (i.get("title") or "").upper() for i in result["items"])

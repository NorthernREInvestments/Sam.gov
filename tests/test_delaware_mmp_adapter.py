"""Delaware MMP free adapter — public bidcondocs discovery."""

from __future__ import annotations

from m3.research.adapters import search_delaware_mmp
from m3.research.package import detect_platform, discover_delaware_mmp_documents


def test_delaware_document_discovery_from_detail():
    url = "https://mmp.delaware.gov/Bids/Details/9398"
    docs = discover_delaware_mmp_documents(url)
    assert docs, "expected public GetBidDocumentList attachments"
    assert all(d.get("source_url") for d in docs)
    assert detect_platform(url, "delaware_open_bids") == "delaware_mmp"


def test_search_delaware_mmp_skips_serviceish(monkeypatch):
    from m3.research import adapters as ad

    sample = [
        {
            "contractnumber": "DOT2621-CRS_AGGSO",
            "contracttitle": "Coarse Aggregate South District",
            "agencycode": "DOT",
            "bidurl": {"url": "https://mmp.delaware.gov/Bids/Details/9398"},
            "deadlinedate": "2026-12-01T17:00:00.000",
            "opendate": "2026-11-01T00:00:00.000",
        },
        {
            "contractnumber": "BSD270003-DESIGNSERV",
            "contracttitle": "Design Services for SITE School",
            "agencycode": "BSD",
            "bidurl": {"url": "https://mmp.delaware.gov/Bids/Details/9393"},
        },
    ]

    def _fake_json(url, timeout=30):  # noqa: ARG001
        return 200, "https://data.delaware.gov/resource/2hnj-zwix.json", sample

    def _fake_docs(detail_url, html="", *, extra_urls=None):  # noqa: ARG001
        return [
            {
                "source_url": "https://bidcondocs.delaware.gov/DOT/DOT2621-CRS_AGGSO_itb.pdf",
                "original_filename": "DOT2621-CRS_AGGSO_itb.pdf",
                "document_title": "DOT2621-CRS_AGGSO_itb.pdf",
                "document_type": "attachment",
                "is_amendment": False,
                "meta": {"platform": "delaware_mmp"},
            }
        ]

    monkeypatch.setattr(ad, "fetch_json", _fake_json)
    monkeypatch.setattr(
        "m3.research.package.discover_delaware_mmp_documents",
        _fake_docs,
    )
    # search imports discover inside function — patch package symbol used at call time
    import m3.research.package as pkg

    monkeypatch.setattr(pkg, "discover_delaware_mmp_documents", _fake_docs)

    result = search_delaware_mmp(
        {
            "source_key": "delaware_open_bids",
            "config": {"dataset_url": "https://data.delaware.gov/resource/2hnj-zwix.json"},
        },
        limit=10,
        fetch_detail_docs=True,
    )
    assert result["ok"] is True
    assert any("Aggregate" in (i.get("title") or "") for i in result["items"])
    assert all("Design Services" not in (i.get("title") or "") for i in result["items"])
    agg = next(i for i in result["items"] if "Aggregate" in (i.get("title") or ""))
    assert agg["document_urls"]
    assert str(agg["official_url"]).endswith(".pdf")

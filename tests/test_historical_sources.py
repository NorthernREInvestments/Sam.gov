"""On-demand historical source adapters (mocked HTTP)."""

from __future__ import annotations

from m3.research.historical_sources import (
    bso_automation_status,
    missouri_awarded_status,
    pennsylvania_historical_status,
    search_louisiana_lapac_by_bidno,
    search_texas_esbd_awards,
    search_usac_erate_line_items,
)


class _Resp:
    def __init__(self, text: str = "", status_code: int = 200, json_data=None):
        self.text = text
        self.status_code = status_code
        self._json = json_data
        self.url = "https://example.test"

    def json(self):
        return self._json


def test_texas_esbd_awards_parses_po_blocks(monkeypatch):
    html = """
    <html><body>
    PO Number: HHSTX-1 Vendor fluff
    PO Amount: 12,345.67
    Agency/Texas SmartBuy Member Number: 529 : Agency Name
    Vendor Name: Acme Supply Co
    Award Date: 11/01/2025
    PO Number: EF999
    PO Amount: 100.00
    Agency/Texas SmartBuy Member Number: 696
    Vendor Name: Other Vendor
    Award Date: 10/01/2025
    </body></html>
    """

    import m3.research.historical_sources as hs

    class Sess:
        headers: dict = {}

        def get(self, *a, **k):
            return _Resp(html)

    monkeypatch.setattr(hs, "_session", lambda: Sess())
    result = search_texas_esbd_awards("medical", limit=5)
    assert result["ok"]
    assert len(result["items"]) >= 1
    assert result["items"][0]["po_number"]
    assert result["items"][0]["unit_price"] is None
    assert "PO amount" in result["items"][0]["match_reason"]


def test_usac_line_items_context_pricing(monkeypatch):
    import m3.research.historical_sources as hs

    data = [
        {
            "organization_name": "TEST SD",
            "state": "TX",
            "funding_year": "2020",
            "form_471_manufacturer_name": "Cisco Systems",
            "model_of_equipment": "AIR-AP2802I-B-K9",
            "form_471_product_name": "Access Point",
            "form_471_unit_name": "Each",
            "one_time_quantity": "18",
            "one_time_eligible_costs": "560.62",
            "total_one_time_cost": "560.62",
            "total_eligible_one_time_costs": "10091.16",
            "pre_discount_extended_eligible_line_item_costs": "10091.16",
            "funding_request_number": "1999",
        }
    ]

    class Sess:
        headers = {}

        def get(self, *a, **k):
            return _Resp(json_data=data, text="[]")

    monkeypatch.setattr(hs, "_session", lambda: Sess())
    # fix json path - Resp.json used
    result = search_usac_erate_line_items(manufacturer="Cisco", model="AIR-AP2802I-B-K9", limit=3)
    assert result["ok"]
    assert result["items"]
    item = result["items"][0]
    assert item["part_number"] == "AIR-AP2802I-B-K9"
    assert "funding" in item["match_reason"].lower() or "E-Rate" in item["match_reason"]
    assert item["relevance"] in {"MARKET_CONTEXT_ONLY", "QUALIFIED_COMPARABLE", "VERIFIED_COMPARABLE", "UNUSABLE"}


def test_louisiana_bidno_docs(monkeypatch):
    import m3.research.historical_sources as hs

    html = """
    <html><body>
    <a href="/osp/lapac/agency/pdf/9089600.pdf">3000026848</a>
    <a href="/osp/lapac/agency/pdf/9089601-00.pdf">addendum</a>
    </body></html>
    """

    class Sess:
        headers = {}

        def get(self, *a, **k):
            return _Resp(html)

    monkeypatch.setattr(hs, "_session", lambda: Sess())
    result = search_louisiana_lapac_by_bidno("3000026848")
    assert result["ok"]
    assert result["document_urls"]
    assert result["items"][0]["match_class"] == "NO_RELIABLE_MATCH"


def test_roadmap_status_helpers():
    assert bso_automation_status()["status"] == "partial"
    assert "Incapsula" in missouri_awarded_status()["blocked"][1] or "Incapsula" in str(
        missouri_awarded_status()
    )
    assert "Materials" in pennsylvania_historical_status()["ad_types"].values()

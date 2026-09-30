"""Phase L.2.9 maximum source coverage tests."""

from __future__ import annotations

from phase_l.board_records import PublicBoardPurchaseAdapter, OpenDataPurchaseAdapter
from phase_l.maximum_source_coverage import corroborate_price_leads
from phase_l.pricing_sources import parse_pdf_price_rows, parse_spreadsheet_prices, parse_council_award_text
from phase_l.product_page_resolution import EXACT_VERIFIED, STRONG_VERIFIED
from phase_l.progressive_funnel import FREIGHT_REQUIRED, stage3_economic_recon
from phase_l.recurring_buy import (
    aggregate_recurring_purchases,
    persist_known_product_economics,
    reverse_live_hunt,
)
from phase_l.source_inventory import SOURCE_INVENTORY, derive_status, health_dashboard
from phase_l.source_roles import (
    CORROBORATED_STRONG_PRICE,
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    PRIMARY_SOURCE_BLOCKED,
    STAGE3_NO_ROW_CAP,
    classify_evidence_roles,
)
from phase_l.state_contracts import STATE_CONTRACT_PORTALS, StateContractPriceAdapter


def test_omnia_and_state_inventory_present():
    families = {r["family"] for r in SOURCE_INVENTORY}
    assert "OMNIA" in families
    assert "Sourcewell" in families
    assert "NASPO" in families
    assert "state term contracts" in families
    assert "government board records" in families
    assert "open-data datasets" in families


def test_state_term_pricing_document_discovery():
    adapter = StateContractPriceAdapter()
    sid = {"model": "ToolCat UW56", "primary_mpn": "UW56", "manufacturer": "Bobcat"}
    urls = adapter.discovery_urls(sid, limit=4)
    assert urls
    assert any(u["state"] == "WA" for u in urls)
    html = '<html><a href="/docs/bobcat-uw56-price-schedule.pdf">Pricing</a></html>'
    docs = adapter.extract_pricing_document_links(html, base_url="https://des.wa.gov/contracts")
    assert docs and docs[0].endswith(".pdf")


def test_washington_des_style_portal_listed():
    assert any(p["state"] == "WA" and "DES" in p["name"] for p in STATE_CONTRACT_PORTALS)


def test_pdf_exact_mpn_price_extraction():
    text = "Line item P/N 1266M27P09 Valve Assembly unit price $1,250.00 awarded"
    sid = {"primary_mpn": "1266M27P09", "model": None}
    rows = parse_pdf_price_rows(text, search_id=sid, source_url="https://example.gov/a.pdf")
    assert rows
    assert rows[0].price == 1250.0


def test_xlsx_exact_model_pricing():
    # Minimal CSV-as-bytes path through spreadsheet parser
    data = b"Model,Price\nToolCat UW56,76900\nOther,10\n"
    sid = {"model": "ToolCat UW56", "primary_mpn": "UW56"}
    rows = parse_spreadsheet_prices(data, search_id=sid, source_url="https://x/price.csv", filename_hint="price.csv")
    assert any(r.price == 76900 for r in rows)


def test_board_packet_purchase_extraction():
    text = (
        "City Council Agenda Item 12: Approve purchase of Bobcat ToolCat UW56 "
        "from Dealer Co for $76,900. Resolution 2024-18."
    )
    sid = {"model": "ToolCat UW56", "manufacturer": "Bobcat", "primary_mpn": "UW56"}
    rows = parse_council_award_text(text, search_id=sid, source_url="https://city.gov/agenda.pdf")
    assert rows
    assert rows[0].price == 76900.0


def test_board_adapter_queries():
    q = PublicBoardPurchaseAdapter().discovery_queries(
        {"model": "F-150 Police Responder", "manufacturer": "Ford"},
        {"agency": "MC Sheriff"},
    )
    assert any("agenda" in x.lower() or "council" in x.lower() for x in q)


def test_open_data_adapter_family():
    assert OpenDataPurchaseAdapter().family == "open-data datasets"


def test_oem_static_price_book_role():
    roles = classify_evidence_roles(source_family="OEM", prefer_history=False)
    assert "CURRENT_COMMERCIAL_PRICE" in roles or "CURRENT_ACQUISITION_PRICE" in roles


def test_dealer_distributor_fallback_map():
    from phase_l.product_detail_resolution import alternate_seller_targets

    sid = {"primary_mpn": "UW56", "model": "ToolCat UW56"}
    alts = alternate_seller_targets(blocked_domain="bobcat.com", search_id=sid, family="EQUIPMENT")
    assert alts
    assert all(a["domain"] != "bobcat.com" for a in alts)


def test_primary_blocked_constant_not_price_not_available():
    assert PRIMARY_SOURCE_BLOCKED == "PRIMARY_SOURCE_BLOCKED"
    assert PRIMARY_SOURCE_BLOCKED != "PRICE_NOT_AVAILABLE"


def test_no_captcha_bypass_in_inventory_notes():
    # Soft check: inventory documents public access only
    assert all("captcha" not in str(r.get("access") or "").lower() for r in SOURCE_INVENTORY)


def test_strong_price_lead_corroboration():
    leads = [
        {"apparent_price": 76900, "seller": "dealer-a.com", "confidence": "HIGH"},
        {"apparent_price": 77100, "seller": "dealer-b.com", "source_url": "https://b", "confidence": "MEDIUM"},
    ]
    out = corroborate_price_leads(leads)
    assert any(L.get("evidence_state") == CORROBORATED_STRONG_PRICE for L in out)


def test_recurring_buy_creation():
    events = [
        {"manufacturer": "Bobcat", "model": "UW56", "buyer": "City A", "unit_price": 80000, "purchase_date": "2024-01-01"},
        {"manufacturer": "Bobcat", "model": "UW56", "buyer": "City B", "unit_price": 81000, "purchase_date": "2024-06-01"},
        {
            "manufacturer": "Bobcat",
            "model": "UW56",
            "buyer": "City A",
            "unit_price": 79000,
            "purchase_date": "2025-01-01",
            "acquisition_benchmark": 70000,
        },
    ]
    watches = aggregate_recurring_purchases(events)
    assert watches
    assert watches[0]["purchase_count"] >= 3


def test_reverse_live_hunt():
    watches = [
        {
            "product_key": "BOBCAT|UW56",
            "model_or_mpn": "UW56",
            "manufacturer": "Bobcat",
            "buyers": ["City A"],
            "status": "RECURRING_CANDIDATE",
            "median_government_price": 80000,
            "historical_margin_estimate": 10000,
        }
    ]
    live = [{"title": "Brand Name Bobcat ToolCat UW56 Utility Work Machine", "solicitation_id": "X1", "our_bid_access": "YES"}]
    matches = reverse_live_hunt(watches=watches, live_rows=live)
    assert matches
    assert matches[0]["solicitation"] == "X1"


def test_known_product_economics_persist():
    known: dict = {}
    rec = persist_known_product_economics(
        known, product_key="BOBCAT|UW56", hist_unit=80000, acq_unit=70000, solicitation="S1"
    )
    assert "KNOWN_PRODUCT" in rec["status"]
    assert known["products"]["BOBCAT|UW56"]["unit_spread"] == 10000


def test_all_stage3_no_caps():
    assert STAGE3_NO_ROW_CAP is True
    assert DEEP_RESEARCH_NO_FIXED_COUNT is True
    assert MANUAL_QUEUE_NO_FIXED_CAP is True


def test_freight_nonfatal_early():
    row = {"title": "Bobcat ToolCat UW56 heavy equipment", "our_bid_access": "YES"}
    stage2 = {
        "recon_identity_eligible": True,
        "market_research_eligible": True,
        "quantity": None,
        "commercial": {"model": "ToolCat UW56", "manufacturer": "Bobcat"},
        "identity_anchors": ["commercial_model"],
    }
    s3 = stage3_economic_recon(row, stage2=stage2)
    assert s3["pass"] is True


def test_strict_final_gate_unchanged():
    assert EXACT_VERIFIED == "EXACT_VERIFIED"
    assert STRONG_VERIFIED == "STRONG_VERIFIED"


def test_source_health_derive():
    assert derive_status({"attempts": 10, "blocks": 8, "successes": 0, "auth_required": 0, "restricted": 0, "parse_failures": 0}) == "BOT_BLOCKED"
    dash = health_dashboard({"sources": {}})
    assert len(dash) == len(SOURCE_INVENTORY)


def test_sitemap_style_product_path_ranking():
    from phase_l.product_detail_resolution import rank_product_links

    links = [
        {"url": "https://shop.example/search?q=UW56", "score": 90},
        {"url": "https://shop.example/products/toolcat-uw56", "score": 40},
    ]
    ranked = rank_product_links(links, search_id={"model": "ToolCat UW56", "primary_mpn": "UW56"})
    assert ranked
    assert "products/toolcat-uw56" in ranked[0]["url"]

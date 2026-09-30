"""Phase L.2.5 source expansion tests."""

from __future__ import annotations

from phase_l.convergence import (
    BOTH_FOUND_POSITIVE_SPREAD,
    COOPERATIVE_CONTRACT,
    OEM_MSRP,
    PRICE_ACCESS_CONDITIONAL,
    PRICE_ACCESS_NO,
    PRICE_ACCESS_YES,
    QUEUE_PROFITABLE_ANY,
    classify_price_access,
    join_history_and_price,
)
from phase_l.pricing_sources import (
    DEALER_ADVERTISED,
    HISTORICAL_GOV_PRICE,
    parse_content_auto,
    parse_council_award_text,
    parse_html_bid_tab,
    parse_pdf_price_rows,
    parse_spreadsheet_prices,
)
from phase_l.procurement_intel import (
    build_search_id,
    merge_history_from_procurement,
    merge_market_from_procurement,
    procurement_history_queries,
)
from phase_l.recurring_buy import (
    aggregate_buyer_watchlist,
    aggregate_recurring_purchases,
    prioritize_recurring,
)


SEARCH = {"manufacturer": "Bobcat", "model": "ToolCat UW56", "primary_mpn": None}


def test_state_bid_tab_parsing():
    html = """
    <table>
      <tr><th>Bidder</th><th>Model</th><th>Unit Price</th><th>Status</th></tr>
      <tr><td>Acme Fleet</td><td>ToolCat UW56</td><td>$78,500.00</td><td>Awarded</td></tr>
      <tr><td>Other Co</td><td>ToolCat UW56</td><td>$81,200.00</td><td>Not awarded</td></tr>
    </table>
    """
    recs = parse_html_bid_tab(html, search_id=SEARCH, source_url="https://city.example.gov/bidtab")
    assert recs
    assert recs[0].price == 78500.0 or recs[0].price == 81200.0
    assert HISTORICAL_GOV_PRICE in recs[0].roles
    assert recs[0].economics_eligible_as_history
    assert not recs[0].economics_eligible_as_acquisition


def test_local_award_table_and_council():
    html = """
    <tr><td>Ford F-150 Police Responder</td><td>qty 2</td><td>$56,150</td><td>awarded to Metro Ford</td></tr>
    """
    search = {"manufacturer": "Ford", "model": "F-150 Police Responder", "primary_mpn": None}
    tabs = parse_html_bid_tab(html, search_id=search, source_url="https://county.example.gov/awards")
    assert tabs and tabs[0].price == 56150.0

    council = """
    City Council Resolution 24-12: Approve purchase of one Bobcat ToolCat UW56
    from Northern Equipment for $79,900.00 as awarded under IFB 2024-08.
    """
    crecs = parse_council_award_text(council, search_id=SEARCH, source_url="https://city.example.gov/agenda.pdf")
    assert crecs
    assert crecs[0].price == 79900.0
    assert crecs[0].source_type == "BOARD_COUNCIL"


def test_pdf_exact_row_price_extraction():
    text = """
    Contract Price Schedule
    Item Model Description Unit Price
    12 ToolCat UW56 Utility Work Machine $72,400.00 EA
    13 OtherThing XYZ $1.00
    """
    recs = parse_pdf_price_rows(
        text, search_id=SEARCH, source_url="https://vendor.example.com/schedule.pdf", role_hint="CURRENT_ACQUISITION_PRICE"
    )
    assert recs
    assert recs[0].price == 72400.0
    # random other dollar on page without identity should not be sole hit
    assert all("UW56" in (r.evidence_text or "") or "ToolCat" in (r.evidence_text or "") for r in recs)


def test_spreadsheet_exact_row_price():
    csv_data = (
        "manufacturer,model,sku,msrp,contract price\n"
        "Bobcat,ToolCat UW56,UW56,85000,71000\n"
        "Bobcat,Other,OT1,100,90\n"
    ).encode("utf-8")
    recs = parse_spreadsheet_prices(
        csv_data, search_id=SEARCH, source_url="https://coop.example.com/prices.csv", filename_hint="prices.csv"
    )
    assert recs
    assert recs[0].price in {71000.0, 85000.0}
    assert recs[0].product_identity


def test_cooperative_contract_price_access():
    access = classify_price_access(
        price_type=COOPERATIVE_CONTRACT,
        url="https://www.sourcewell-mn.gov/contract/x",
        evidence_text="Sourcewell members only pricing",
    )
    assert access["price_access"] == PRICE_ACCESS_CONDITIONAL
    assert access["economics_eligible"] is False


def test_oem_msrp_and_dealer_advertised():
    oem_html = """
    <html><body><h1>Bobcat ToolCat UW56</h1>
    <p>MSRP $84,500</p><p>Configure your machine</p></body></html>
    """
    recs = parse_content_auto(
        url="https://www.bobcat.com/toolcat-uw56",
        content=oem_html,
        search_id=SEARCH,
        prefer_history=False,
    )
    assert recs
    assert recs[0].price is not None
    # dealer inventory
    dealer = """
    New Bobcat ToolCat UW56 in stock — advertised price $76,900 — new condition
    """
    drecs = parse_content_auto(
        url="https://dealer.example.com/inventory/toolcat",
        content=dealer,
        search_id=SEARCH,
        prefer_history=False,
    )
    assert drecs
    assert drecs[0].acquisition_price_type in {DEALER_ADVERTISED, OEM_MSRP, "PUBLIC_RETAIL", "PUBLIC_CATALOG"}


def test_government_only_price_rejected_as_acquisition():
    access = classify_price_access(
        price_type="PUBLIC_GOV_CHANNEL",
        evidence_text="Government only — federal agencies only",
    )
    assert access["price_access"] == PRICE_ACCESS_NO
    assert access["economics_eligible"] is False

    market = merge_market_from_procurement(
        {},
        {
            "attempted": True,
            "records": [
                {
                    "price": 50000,
                    "roles": ["GOVERNMENT_CHANNEL_PRICE"],
                    "economics_eligible_as_acquisition": False,
                    "price_access": PRICE_ACCESS_NO,
                    "acquisition_price_type": "STATE_TERM_CONTRACT",
                    "source_url": "https://state.example.gov/term",
                    "confidence": "HIGH",
                }
            ],
        },
    )
    assert market.get("public_retail_unit_price") is None
    assert market.get("economics_eligible") is False


def test_history_merge_from_bid_tab():
    hist = merge_history_from_procurement(
        {"historical_award_unit_price": None},
        {
            "records": [
                {
                    "price": 79000,
                    "roles": [HISTORICAL_GOV_PRICE],
                    "source_type": "BID_TAB_HTML",
                    "source_url": "https://city.example.gov/tab",
                    "confidence": "HIGH",
                }
            ]
        },
    )
    assert hist["historical_award_unit_price"] == 79000
    assert hist["history_confidence"] == "EXACT_MODEL"


def test_recurring_purchase_and_buyer_aggregation():
    events = [
        {"manufacturer": "Bobcat", "model": "ToolCat UW56", "buyer": "City A", "unit_price": 78000, "quantity": 1, "purchase_date": "2024-01-15"},
        {"manufacturer": "Bobcat", "model": "ToolCat UW56", "buyer": "City A", "unit_price": 79000, "quantity": 1, "purchase_date": "2024-06-15", "acquisition_benchmark": 70000},
        {"manufacturer": "Bobcat", "model": "ToolCat UW56", "buyer": "County B", "unit_price": 80000, "quantity": 2, "purchase_date": "2025-01-10", "acquisition_benchmark": 70000},
        {"manufacturer": "Ford", "model": "F-150 Police Responder", "buyer": "City A", "unit_price": 56000, "quantity": 2, "purchase_date": "2024-03-01"},
    ]
    watches = aggregate_recurring_purchases(events)
    assert any(w["purchase_count"] >= 3 for w in watches)
    pri = prioritize_recurring(watches)
    assert pri[0]["priority_score"] >= pri[-1]["priority_score"]
    buyers = aggregate_buyer_watchlist(events)
    assert any(b["buyer"] == "City A" and b["purchase_count"] >= 2 for b in buyers)


def test_positive_profit_queue_with_l25_sources():
    # Non-heavy CONUS item so freight does not block completed economics
    joined = join_history_and_price(
        row={"title": "Dell Latitude 5540 laptop", "place_of_performance": "Austin TX"},
        history={
            "historical_award_unit_price": 1800,
            "history_confidence": "EXACT_MODEL",
            "source_type": "BID_TAB_HTML",
            "history_research_state": "HISTORY_FOUND",
        },
        market={
            "public_retail_unit_price": 1200,
            "economics_eligible": True,
            "public_retail_source": "https://www.dell.com/latitude-5540",
            "l22_confidence": "STRONG_VERIFIED",
            "acquisition_price_type": OEM_MSRP,
        },
        commercial={"configuration_completeness": "COMPLETE"},
        quantity=10,
    )
    assert joined["convergence_state"] == BOTH_FOUND_POSITIVE_SPREAD
    assert joined["economics_completed"]
    assert joined["expected_net_profit"] > 0
    assert joined["queue"] in {QUEUE_PROFITABLE_ANY, "PROFITABLE_GE_10K", "PROMISING_UNIT_ECONOMICS"}


def test_procurement_history_queries_include_gov_pdf():
    sid = build_search_id({"model": "ToolCat UW56"}, {"manufacturer": "Bobcat", "model": "ToolCat UW56"}, {})
    qs = procurement_history_queries(sid, {"agency": "City of Austin", "pop_state": "TX"})
    assert any("award" in q for q in qs)
    assert any("site:.gov" in q for q in qs)
    assert any("filetype:pdf" in q for q in qs)

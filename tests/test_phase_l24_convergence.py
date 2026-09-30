"""Phase L.2.4 price/history convergence tests."""

from __future__ import annotations

from phase_l.convergence import (
    ACCESS_BLOCKED,
    BOTH_FOUND_NEGATIVE_SPREAD,
    BOTH_FOUND_POSITIVE_SPREAD,
    COOPERATIVE_CONTRACT,
    CURRENT_PRICE_ONLY,
    EXACT_PRODUCT_EXACT_UNIT,
    FREIGHT_UNRESOLVED,
    HISTORY_ONLY,
    HISTORY_REJECTED,
    NEITHER_FOUND,
    OEM_MSRP,
    PRICE_ACCESS_CONDITIONAL,
    PRICE_ACCESS_NO,
    PRICE_ACCESS_YES,
    PUBLIC_RETAIL,
    QUEUE_PROFITABLE_10K,
    QUEUE_PROFITABLE_ANY,
    QUEUE_PROMISING_UNIT,
    STRONG_COMPARABLE,
    WEAK_COMPARABLE,
    classify_acquisition_price_type,
    classify_freight_screen,
    classify_price_access,
    expanded_history_keywords,
    history_economics_ok,
    history_query_approaches,
    join_history_and_price,
    map_history_confidence,
    select_usable_acquisition,
)


def test_expanded_history_keywords_include_model():
    keys = expanded_history_keywords(
        {"manufacturer": "Bobcat", "model": "ToolCat UW56"},
        {"title": "Brand Name Bobcat ToolCat UW56"},
        {"manufacturer": "Bobcat", "model": "ToolCat UW56"},
    )
    assert any("UW56" in k or "ToolCat" in k for k in keys)
    assert len(history_query_approaches(keys)) >= 3


def test_history_mpn_and_model_keywords():
    keys = expanded_history_keywords({"mpn": "1274M99P01", "nsn": "5930-01-215-4689"})
    assert "1274M99P01" in keys or "5930-01-215-4689" in keys


def test_map_history_confidence_and_reject_weak():
    assert map_history_confidence({"historical_award_unit_price": 100, "history_confidence": "EXACT_RECENT"}) == EXACT_PRODUCT_EXACT_UNIT
    assert map_history_confidence({"historical_award_unit_price": 100, "history_confidence": "STRONG_COMPARABLE"}) == STRONG_COMPARABLE
    assert map_history_confidence({"historical_award_unit_price": 100, "history_confidence": "WEAK_COMPARABLE"}) == WEAK_COMPARABLE
    assert map_history_confidence({"historical_award_unit_price": None}) == HISTORY_REJECTED
    assert history_economics_ok(EXACT_PRODUCT_EXACT_UNIT)
    assert history_economics_ok(STRONG_COMPARABLE)
    assert not history_economics_ok(WEAK_COMPARABLE)


def test_oem_msrp_and_dealer_types():
    assert classify_acquisition_price_type(url="https://www.dell.com/p", seller_type="MANUFACTURER", evidence_text="MSRP $1200") == OEM_MSRP
    assert classify_acquisition_price_type(url="https://dealer.example/bobcat", seller_type="EQUIPMENT_DEALER") == "DEALER_ADVERTISED"
    assert classify_acquisition_price_type(url="https://www.grainger.com/product/x", seller_type="ESTABLISHED_DISTRIBUTOR") == "AUTHORIZED_DISTRIBUTOR"


def test_cooperative_price_access_gate():
    ptype = classify_acquisition_price_type(url="https://www.sourcewell-mn.gov/contract/x", evidence_text="members only pricing")
    assert ptype == COOPERATIVE_CONTRACT
    access = classify_price_access(price_type=ptype, url="https://www.sourcewell-mn.gov/x", evidence_text="cooperative members only")
    assert access["price_access"] == PRICE_ACCESS_CONDITIONAL
    assert access["economics_eligible"] is False


def test_government_only_price_not_used():
    access = classify_price_access(
        price_type="PUBLIC_GOV_CHANNEL",
        evidence_text="Government only pricing for federal agencies only",
    )
    assert access["price_access"] == PRICE_ACCESS_NO
    assert access["economics_eligible"] is False


def test_public_retail_usable():
    access = classify_price_access(price_type=PUBLIC_RETAIL, url="https://www.cdw.com/product/x")
    assert access["price_access"] == PRICE_ACCESS_YES
    assert access["economics_eligible"] is True


def test_select_usable_skips_inaccessible():
    market = {
        "public_retail_unit_price": 50000,
        "economics_eligible": True,
        "public_retail_source": "https://www.sourcewell-mn.gov/contract/ford",
        "evidence": [
            {
                "price": 50000,
                "source_url": "https://www.sourcewell-mn.gov/contract/ford",
                "economics_eligible": True,
                "confidence": "STRONG_VERIFIED",
                "evidence_text": "Sourcewell members only",
                "seller_type": "GOVERNMENT_COOPERATIVE_CATALOG",
            }
        ],
    }
    sel = select_usable_acquisition(market)
    # Cooperative members-only should not be economics-usable
    assert sel is not None
    assert sel.get("economics_eligible") is False or sel.get("public_retail_unit_price") is None


def test_join_both_positive_and_negative():
    pos = join_history_and_price(
        row={"title": "Dell laptop"},
        history={"historical_award_unit_price": 1000, "history_confidence": "EXACT_RECENT", "history_research_state": "HISTORY_FOUND"},
        market={
            "public_retail_unit_price": 700,
            "economics_eligible": True,
            "public_retail_source": "https://www.cdw.com/product/dell-x",
            "verified_evidence": [
                {
                    "price": 700,
                    "source_url": "https://www.cdw.com/product/dell-x",
                    "economics_eligible": True,
                    "confidence": "EXACT_VERIFIED",
                    "seller_type": "RETAILER",
                }
            ],
        },
        quantity=10,
    )
    assert pos["convergence_state"] == BOTH_FOUND_POSITIVE_SPREAD
    assert pos["unit_economics"]["unit_raw_spread"] == 300.0
    assert pos["ready_to_bid"] is False

    neg = join_history_and_price(
        row={"title": "widget"},
        history={"historical_award_unit_price": 100, "history_confidence": "EXACT_RECENT", "history_research_state": "HISTORY_FOUND"},
        market={
            "public_retail_unit_price": 200,
            "economics_eligible": True,
            "public_retail_source": "https://www.cdw.com/product/w",
            "verified_evidence": [
                {"price": 200, "source_url": "https://www.cdw.com/product/w", "economics_eligible": True, "confidence": "EXACT_VERIFIED", "seller_type": "RETAILER"}
            ],
        },
        quantity=5,
    )
    assert neg["convergence_state"] == BOTH_FOUND_NEGATIVE_SPREAD


def test_join_history_only_retail_only_neither():
    h = join_history_and_price(
        row={"title": "x"},
        history={"historical_award_unit_price": 50, "history_confidence": "EXACT_RECENT", "history_research_state": "HISTORY_FOUND"},
        market={"attempted": True},
        quantity=1,
    )
    assert h["convergence_state"] == HISTORY_ONLY
    r = join_history_and_price(
        row={"title": "y"},
        history={"attempted": True},
        market={
            "public_retail_unit_price": 80,
            "economics_eligible": True,
            "public_retail_source": "https://www.cdw.com/p",
            "verified_evidence": [
                {"price": 80, "source_url": "https://www.cdw.com/p", "economics_eligible": True, "confidence": "EXACT_VERIFIED", "seller_type": "RETAILER"}
            ],
        },
    )
    assert r["convergence_state"] == CURRENT_PRICE_ONLY
    n = join_history_and_price(row={"title": "z"}, history={}, market={})
    assert n["convergence_state"] == NEITHER_FOUND


def test_positive_unit_spread_quantity_unknown_queue():
    j = join_history_and_price(
        row={"title": "Ford F-150 Police Responder CONUS"},
        history={"historical_award_unit_price": 60000, "history_confidence": "EXACT_RECENT", "history_research_state": "HISTORY_FOUND"},
        market={
            "public_retail_unit_price": 56000,
            "economics_eligible": True,
            "public_retail_source": "https://www.cdw.com/nope",
            "verified_evidence": [
                {
                    "price": 56000,
                    "source_url": "https://dealer.example/ford-f150-police-responder",
                    "economics_eligible": True,
                    "confidence": "STRONG_VERIFIED",
                    "seller_type": "EQUIPMENT_DEALER",
                }
            ],
        },
        quantity=None,
    )
    assert j["unit_economics"]["unit_raw_spread"] == 4000.0
    assert j["queue"] == QUEUE_PROMISING_UNIT
    assert j["economics_completed"] is False


def test_freight_alaska_heavy_unresolved():
    fr = classify_freight_screen(
        {"title": "Bobcat ToolCat UW56", "place_of_performance": "Anchorage, Alaska"}
    )
    assert fr["destination_class"] == "ALASKA"
    assert fr["freight_unresolved"] is True
    assert FREIGHT_UNRESOLVED in fr["freight_status"] or fr["note"] == "FREIGHT_REQUIRED_BEFORE_FINAL_PASS"


def test_freight_conus_parcel_ok():
    fr = classify_freight_screen({"title": "ASUS VG27AQ Monitor", "place_of_performance": "Austin, TX"})
    assert fr["destination_class"] == "CONUS"
    # monitors not heavy → OK
    assert fr["freight_unresolved"] is False


def test_profitable_queues_no_ready_to_bid():
    # Simulate completed positive small profit via join with qty + CONUS light item
    j = join_history_and_price(
        row={"title": "ASUS VG27AQ Monitor", "place_of_performance": "Dallas TX"},
        history={"historical_award_unit_price": 400, "history_confidence": "EXACT_RECENT", "history_research_state": "HISTORY_FOUND"},
        market={
            "public_retail_unit_price": 300,
            "economics_eligible": True,
            "public_retail_source": "https://www.newegg.com/asus-vg27aq",
            "verified_evidence": [
                {
                    "price": 300,
                    "source_url": "https://www.newegg.com/asus-vg27aq",
                    "economics_eligible": True,
                    "confidence": "EXACT_VERIFIED",
                    "seller_type": "RETAILER",
                }
            ],
        },
        quantity=20,
    )
    assert j["ready_to_bid"] is False
    if j.get("economics_completed") and j.get("expected_net_profit") and j["expected_net_profit"] > 0:
        if j["expected_net_profit"] >= 10000:
            assert j["queue"] == QUEUE_PROFITABLE_10K
        else:
            assert j["queue"] == QUEUE_PROFITABLE_ANY

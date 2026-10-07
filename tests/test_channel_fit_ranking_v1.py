"""Channel fit + MSRP-first quote priority — acceptance + Truckee Donner regression."""

from __future__ import annotations

from channel_fit.coverage import compute_public_basket, public_price_coverage_class
from channel_fit.engine import BUILD, score_opportunity_channel_fit
from channel_fit.registry import classify_bidder_type, role_for_company


def test_build_target():
    assert BUILD.startswith("2026")
    assert "channel-fit" in BUILD


def _priced_line(ext: float, public_unit: float, qty: float = 1.0, grade: str = "A_EXACT_CURRENT_PUBLIC"):
    return {
        "line_kind": "product",
        "extended_value": ext,
        "quantity": qty,
        "retail": {"unit_price": public_unit, "source": "public"},
        "price_match_grade": grade,
    }


def test_truckee_donner_waterworks_pass():
    """Regression: Truckee Donner water materials → D_CHANNEL_DOMINATED / PASS."""
    opp = {
        "stable_key": "truckee-donner-water-materials",
        "title": "Truckee Donner Public Utility District Water Materials Contract",
        "buyer": "Truckee Donner PUD",
        "government_value": 180000,
        "deadline": "2026-12-15",
        "days_remaining": 60,
        "package_state": "PACKAGE_ACQUIRED_BIDNET",
        "eligibility": "ELIGIBILITY_CLEAR",
        "historical_bidders": [
            {"name": "Ferguson Waterworks", "bid_amount": 175000, "winner": True},
            {"name": "Western Nevada Supply", "bid_amount": 182000, "winner": False},
            {"name": "Pace Supply", "bid_amount": 190000, "winner": False},
            {"name": "Sierra Mountain Pipe Supply", "bid_amount": 195000, "winner": False},
        ],
        # Public/list far above historical gov win — channel discounts required
        "line_items": [
            _priced_line(45000, 90, qty=500),
            _priced_line(45000, 90, qty=500),
            _priced_line(45000, 90, qty=500),
            _priced_line(45000, 90, qty=500),
        ],
        # Make public basket >> gov: qty*unit = 500*90*4 = 180000 wait that's equal
        # Force public above gov by higher units
    }
    # Rebuild lines so public basket is materially above government value
    opp["line_items"] = [
        _priced_line(45000, 120, qty=500),  # public ext 60000 each
        _priced_line(45000, 120, qty=500),
        _priced_line(45000, 120, qty=500),
        _priced_line(45000, 120, qty=500),
    ]
    # public = 4 * 500 * 120 = 240000 > gov 180000
    scored = score_opportunity_channel_fit(opp)
    assert scored["CHANNEL_COMPETITION_CLASS"] == "D_CHANNEL_DOMINATED"
    assert scored["CHANNEL_DOMINANCE_SCORE"] >= 81
    assert scored["PRE_QUOTE_DECISION"] == "PASS"
    assert scored["bidder_structure"]["DISTRIBUTOR_BIDDER_PERCENT"] == 100.0
    # Ferguson on waterworks = competitor
    roles = {b["bidder_name"]: b["role"] for b in scored["bidder_structure"]["historical_bidders"]}
    assert roles["Ferguson Waterworks"] == "LIKELY_COMPETITOR"


def test_mro_grainger_fastenal_c_or_d():
    opp = {
        "stable_key": "mro-pure",
        "title": "City MRO Industrial Supply Contract Fasteners Bearings",
        "government_value": 50000,
        "days_remaining": 20,
        "historical_bidders": [
            {"name": "Grainger", "winner": True},
            {"name": "Fastenal", "winner": False},
            {"name": "MSC Industrial", "winner": False},
        ],
        "line_items": [
            _priced_line(20000, 45, qty=400),
            _priced_line(20000, 45, qty=400),
            _priced_line(10000, 45, qty=200),
        ],
    }
    scored = score_opportunity_channel_fit(opp)
    assert scored["CHANNEL_COMPETITION_CLASS"] in {"C_DISTRIBUTOR_ADVANTAGED", "D_CHANNEL_DOMINATED"}


def test_mixed_office_tools_ppe_not_auto_killed():
    """Grainger carrying some items does not auto-kill a mixed basket."""
    opp = {
        "stable_key": "mixed-office-ppe",
        "title": "Office Supplies Tools and PPE Resale Package",
        "government_value": 100000,
        "days_remaining": 25,
        "package_state": "PACKAGE_ACQUIRED_BIDNET",
        "eligibility": "ELIGIBILITY_CLEAR",
        "supplier_breadth": 4,
        "financing_plausible": True,
        "historical_bidders": [
            {"name": "Acme Office Solutions", "winner": True},
            {"name": "Pacific Safety Reseller", "winner": False},
            {"name": "Grainger", "winner": False},  # present but not dominant
            {"name": "Valley Trading Co", "winner": False},
        ],
        "line_items": [
            _priced_line(25000, 18, qty=1000),
            _priced_line(25000, 18, qty=1000),
            _priced_line(25000, 18, qty=1000),
            _priced_line(25000, 18, qty=1000),
        ],
    }
    # public = 4*1000*18 = 72000 < gov 100000 → headroom
    scored = score_opportunity_channel_fit(opp)
    assert scored["CHANNEL_COMPETITION_CLASS"] != "D_CHANNEL_DOMINATED"
    assert scored["PRE_QUOTE_DECISION"] != "PASS" or "channel" not in str(scored.get("decision_reasons"))


def test_single_brand_oem_high_dominance():
    opp = {
        "stable_key": "oem-heavy",
        "title": "Authorized Dealer Only OEM Equipment Parts",
        "single_brand_concentration": 0.95,
        "government_value": 80000,
        "historical_bidders": [
            {"name": "Graybar", "winner": True},
            {"name": "Wesco", "winner": False},
        ],
        "line_items": [_priced_line(80000, 100, qty=800)],
    }
    scored = score_opportunity_channel_fit(opp)
    assert scored["CHANNEL_DOMINANCE_SCORE"] >= 41


def test_fragmented_multi_brand_lower_dominance():
    opp = {
        "stable_key": "fragmented",
        "title": "Multi Brand Facilities Supplies Across Categories",
        "single_brand_concentration": 0.15,
        "government_value": 90000,
        "days_remaining": 30,
        "historical_bidders": [
            {"name": "Summit Facilities Reseller", "winner": True},
            {"name": "Northstar Integrator LLC", "winner": False},
            {"name": "Bay Area Supply Trading", "winner": False},
        ],
        "line_items": [
            _priced_line(30000, 20, qty=1000),
            _priced_line(30000, 20, qty=1000),
            _priced_line(30000, 20, qty=1000),
        ],
        "supplier_breadth": 5,
        "package_state": "PACKAGE_ACQUIRED_BIDNET",
        "financing_plausible": True,
    }
    scored = score_opportunity_channel_fit(opp)
    assert scored["CHANNEL_DOMINANCE_SCORE"] <= 40
    assert scored["CHANNEL_COMPETITION_CLASS"] in {"A_RESELLER_FRIENDLY", "B_MIXED_CHANNEL", "UNKNOWN"}


def test_coverage_13_percent_insufficient():
    lines = [_priced_line(1000, 10, qty=10)] + [
        {"line_kind": "product", "extended_value": 1000, "quantity": 10} for _ in range(7)
    ]
    # 1 priced of ~8 by value if weights equal → 12.5%
    basket = compute_public_basket(lines, government_value=10000)
    assert basket["PUBLIC_PRICE_COVERAGE_CLASS"] == "INSUFFICIENT"
    assert public_price_coverage_class(13.0) == "INSUFFICIENT"


def test_coverage_80_usable():
    assert public_price_coverage_class(80.0) == "USABLE"


def test_coverage_95_strong():
    assert public_price_coverage_class(95.0) == "STRONG"


def test_public_below_gov_call_today():
    opp = {
        "stable_key": "good-headroom",
        "title": "Mixed Office Tools PPE Multi Brand Facilities Package",
        "government_value": 120000,
        "days_remaining": 20,
        "package_state": "PACKAGE_ACQUIRED_BIDNET",
        "eligibility": "ELIGIBILITY_CLEAR",
        "supplier_breadth": 4,
        "financing_plausible": True,
        "historical_bidders": [
            {"name": "Summit Facilities Reseller", "winner": True},
            {"name": "Northstar Integrator LLC", "winner": False},
            {"name": "Bay Area Supply Trading", "winner": False},
        ],
        "line_items": [
            _priced_line(30000, 20, qty=1000),
            _priced_line(30000, 20, qty=1000),
            _priced_line(30000, 20, qty=1000),
            _priced_line(30000, 20, qty=1000),
        ],
    }
    # public = 80000, gov = 120000 → ~33% headroom, coverage complete
    scored = score_opportunity_channel_fit(opp)
    assert scored["PUBLIC_PRICE_COVERAGE_CLASS"] in {"USABLE", "STRONG", "COMPLETE"}
    assert (scored["VISIBLE_HEADROOM"] or 0) > 0
    assert scored["CHANNEL_COMPETITION_CLASS"] in {"A_RESELLER_FRIENDLY", "B_MIXED_CHANNEL"}
    assert scored["PRE_QUOTE_DECISION"] in {"CALL_TODAY", "QUOTE_IF_CAPACITY"}


def test_public_above_gov_pass():
    opp = {
        "stable_key": "no-headroom",
        "title": "Mixed Office Tools Package",
        "government_value": 50000,
        "days_remaining": 20,
        "historical_bidders": [
            {"name": "Summit Facilities Reseller", "winner": True},
        ],
        "line_items": [
            _priced_line(25000, 40, qty=1000),
            _priced_line(25000, 40, qty=1000),
        ],
    }
    # public = 80000 > gov 50000
    scored = score_opportunity_channel_fit(opp)
    assert scored["PRE_QUOTE_DECISION"] == "PASS"


def test_supplier_vs_competitor_context():
    assert classify_bidder_type("Ferguson Waterworks", vertical="WATERWORKS") == "DISTRIBUTOR"
    assert (
        role_for_company("Ferguson", vertical="WATERWORKS", channel_class="D_CHANNEL_DOMINATED")
        == "LIKELY_COMPETITOR"
    )
    assert (
        role_for_company("Ferguson", vertical=None, channel_class="A_RESELLER_FRIENDLY")
        == "OUR_SUPPLIER"
    )


def test_weak_comparable_excluded_from_coverage():
    lines = [
        {
            "line_kind": "product",
            "extended_value": 5000,
            "quantity": 10,
            "retail": {"unit_price": 50, "source": "weak comparable"},
            "price_match_grade": "F_WEAK_COMPARABLE",
        },
        {
            "line_kind": "product",
            "extended_value": 5000,
            "quantity": 10,
            "retail": {"unit_price": 40, "source": "public"},
            "price_match_grade": "A_EXACT_CURRENT_PUBLIC",
        },
    ]
    basket = compute_public_basket(lines, government_value=12000)
    # Only second line counts → 50% value coverage → WEAK
    assert basket["PUBLIC_BASKET_VALUE_COVERAGE"] == 50.0
    assert basket["PUBLIC_PRICE_COVERAGE_CLASS"] == "WEAK"

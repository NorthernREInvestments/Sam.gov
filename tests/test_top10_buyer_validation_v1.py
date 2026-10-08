"""Top-10 buyer validation unit tests."""

from buyer_intelligence.validate_top10 import BUILD, normalize_buyer, cash_recalc


def test_build():
    assert "top10-buyer-validation" in BUILD


def test_normalize_gometro_high():
    n = normalize_buyer("Go-Metro", family="AUTO_PARTS")
    assert n["BUYER_NORMALIZATION_CONFIDENCE"] == "HIGH"
    assert n["STATE"] == "OH"


def test_normalize_california_low():
    n = normalize_buyer("California", family="MRO")
    assert n["BUYER_NORMALIZATION_CONFIDENCE"] == "LOW"


def test_cash_uses_order_totals_not_tiny_lines():
    purchases = [
        {"TOTAL_AMOUNT": 7610.0},
        {"TOTAL_AMOUNT": 15087.0},
        {"TOTAL_AMOUNT": 5393.0},
    ]
    cash = cash_recalc(purchases, family="AUTO_PARTS")
    assert cash["MEDIAN_ORDER_TOTAL"] > 1000
    assert cash["CASH_RISK"] in {"LOW", "MEDIUM"}

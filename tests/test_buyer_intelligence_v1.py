"""Buyer Intelligence + Micro-Purchase V1 — unit tests (no network)."""

from __future__ import annotations

from buyer_intelligence.engine import (
    BUILD,
    amount_band,
    classify_family,
    detect_pcard,
    sourcing_simplicity,
)


def test_build_tag():
    assert BUILD.startswith("20261007-m3-buyer-intelligence")


def test_classify_family_mro_auto():
    assert classify_family("SCREW HEX FLANGE exhaust manifold") == "AUTO_PARTS"
    assert classify_family("janitorial cleaning supplies mop") == "JANITORIAL_SUPPLIES"
    assert classify_family("nitrile gloves PPE") == "PPE"


def test_amount_bands():
    assert amount_band(3000) == "0-5K"
    assert amount_band(12000) == "5K-15K"
    assert amount_band(20000) == "15K-25K"
    assert amount_band(40000) == "25K-50K"


def test_pcard_and_sourcing():
    yes, ev, fast = detect_pcard("Small purchase via government purchase card RFQ")
    assert yes == "YES"
    assert ev
    assert fast == "YES"
    score = sourcing_simplicity(
        {
            "text": "Cummins filter MPN 123",
            "product_family": "AUTO_PARTS",
            "mpn": "123",
            "RECURRING_VENDOR_COUNT": 3,
        }
    )
    assert score >= 70

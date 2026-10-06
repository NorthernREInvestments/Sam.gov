"""Unit tests for price coverage 80 (no live network required)."""

from __future__ import annotations

from price_coverage_80.corpus import seed_items
from price_coverage_80.guards import check_condition, check_uom_pack, is_credible_seller
from price_coverage_80.models import BUILD
from price_coverage_80.scoring import score_accuracy


def test_build_tag():
    assert BUILD == "20261004-m3-price-coverage-80-v1"


def test_app_build():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_seed_corpus_size():
    items = seed_items()
    assert len(items) >= 80
    easy = [i for i in items if i.get("easy")]
    assert len(easy) >= 25


def test_condition_fail_closed():
    r = check_condition(expected="NEW", found="REMANUFACTURED", new_assumed=True)
    assert r["valid"] is False


def test_uom_pack_mismatch():
    r = check_uom_pack(expected_uom="EA", expected_pack=1, candidate_uom="BX", candidate_pack=10)
    assert r["valid"] is False


def test_noncredible_seller():
    assert is_credible_seller("advancedtruckparts.com", 1.0) is False
    assert is_credible_seller("grainger.com", 12.50) is True


def test_accuracy_wrong_price():
    item = {
        "known_public_price": 100.0,
        "price_tolerance_pct": 0.3,
        "expected_condition": "NEW",
        "known_public_seller": "grainger.com",
    }
    found = {"usable": True, "unit_price": 5.0, "condition": "NEW", "seller": "grainger.com"}
    acc = score_accuracy(item, found)
    assert acc["correct"] is False

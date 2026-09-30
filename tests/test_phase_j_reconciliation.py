"""Phase J reconciliation — identity, false-match guard, UOM, recency."""

from __future__ import annotations

from phase_j.history_reconciliation import (
    COMPARABLE,
    HIGHLY_COMPARABLE,
    MATCH_CONFIRMED,
    MATCH_REJECTED,
    NOT_COMPARABLE,
    RECENCY_AGED,
    RECENCY_CURRENT,
    UNIT_PRICE_NORMALIZED,
    UNIT_PRICE_UNUSABLE,
    classify_recency,
    history_search_keywords,
    reconcile_award,
    reconcile_awards,
    unit_price_from_award,
)
from phase_j.product_identity import (
    LEVEL_EXACT,
    LEVEL_UNKNOWN,
    build_product_identity,
    normalize_nomenclature,
)


def test_exact_nsn_identity():
    ident = build_product_identity({"title": "PARTS KIT NSN: 4320-01-086-6793"})
    assert ident["nsn"] == "4320-01-086-6793"
    assert ident["identity_level"] == LEVEL_EXACT
    assert ident["identity_confidence"] == "EXACT_CONFIRMED"


def test_mpn_manufacturer_cage_exact():
    ident = build_product_identity(
        {
            "title": "ENGINE",
            "description": "MFR CAGE: 61080 MFR PART NUMBER: 1D81C-24VOLT",
        }
    )
    assert ident["mpn"]
    assert ident["cage"] == "61080"
    assert ident["identity_level"] == LEVEL_EXACT


def test_truncated_title_unknown_without_nsn():
    ident = build_product_identity({"title": "25--WHEEL ASSEMBLY,PNEUMAT"})
    assert ident["identity_level"] == LEVEL_UNKNOWN
    assert normalize_nomenclature("25--WHEEL ASSEMBLY,PNEUMAT").startswith("WHEEL")


def test_same_noun_wrong_nsn_rejected():
    current = build_product_identity({"title": "WHEEL NSN: 2530-01-111-2222"})
    award = {
        "description": "WHEEL ASSEMBLY NSN: 2530-01-999-8888",
        "award_amount": 1000,
        "days_ago": 100,
    }
    r = reconcile_award(current, award)
    assert r["final_reconciliation_result"] == MATCH_REJECTED
    assert r["comparability"] == NOT_COMPARABLE
    assert r["drives_economics"] is False


def test_exact_nsn_history_match_confirmed():
    current = build_product_identity({"title": "ENGINE DIESEL NSN: 2815-01-536-9262"})
    award = {
        "description": "NOUN: ENGINE, DIESEL NSN: 2815-01-536-9262 P/N: 1D81C QTY: 38",
        "award_amount": 318014.4,
        "days_ago": 400,
        "award_id": "X1",
    }
    r = reconcile_award(current, award)
    assert r["final_reconciliation_result"] == MATCH_CONFIRMED
    assert r["comparability"] == HIGHLY_COMPARABLE
    assert r["unit_price"]["unit_price_confidence"] == UNIT_PRICE_NORMALIZED
    assert r["drives_economics"] is True


def test_wheel_noun_without_nsn_not_in_search_keywords():
    ident = build_product_identity({"title": "Wheel Assembly, Pneumatic Tire"})
    assert history_search_keywords(ident) == []


def test_stale_exact_history_is_reference_only():
    current = build_product_identity({"title": "NSN: 4320-01-086-6793"})
    award = {
        "description": "NSN: 4320-01-086-6793 QTY: 2",
        "award_amount": 100000,
        "days_ago": 4000,
    }
    r = reconcile_award(current, award)
    assert r["recency"] == "STALE"
    assert r["drives_economics"] is False
    assert r["reference_only"] is True


def test_aged_exact_nsn_can_be_comparable():
    assert classify_recency(2200) == RECENCY_AGED
    current = build_product_identity({"title": "NSN: 2815-01-536-9262"})
    award = {
        "description": "ENGINE DIESEL NSN: 2815-01-536-9262 QTY: 38",
        "award_amount": 318014.4,
        "days_ago": 2219,
    }
    r = reconcile_award(current, award)
    assert r["recency"] == RECENCY_AGED
    assert r["comparability"] == COMPARABLE
    assert r["drives_economics"] is True


def test_pack_vs_each_unit_price():
    award = {"description": "NSN: 1234-01-234-5678 QTY: 10 EA", "award_amount": 1000.0, "days_ago": 100}
    up = unit_price_from_award(award)
    assert up["derived_unit_price"] == 100.0
    assert up["unit_price_confidence"] == UNIT_PRICE_NORMALIZED


def test_uom_mismatch_visible():
    current = build_product_identity({"title": "NSN: 1234-01-234-5678"})
    award = {
        "description": "NSN: 1234-01-234-5678 QTY: 5 BX",
        "award_amount": 500,
        "days_ago": 100,
    }
    r = reconcile_award(current, award, solicitation_uom="EA")
    assert r["uom_comparison"] == "mismatch"


def test_multiple_awards_median_lot():
    current = build_product_identity({"title": "NSN: 2815-01-536-9262"})
    awards = [
        {"description": "NSN: 2815-01-536-9262", "award_amount": 100000, "days_ago": 200},
        {"description": "NSN: 2815-01-536-9262", "award_amount": 200000, "days_ago": 300},
        {"description": "NSN: 2815-01-536-9262", "award_amount": 300000, "days_ago": 400},
    ]
    set_r = reconcile_awards(current, awards)
    assert set_r["history_class"] in {"STRONG_HISTORY", "MODERATE_HISTORY"}
    assert set_r["revenue"] == 200000.0
    assert set_r["usable_count"] == 3


def test_outlier_not_silently_removed():
    current = build_product_identity({"title": "NSN: 2815-01-536-9262"})
    awards = [
        {"description": "NSN: 2815-01-536-9262", "award_amount": 100, "days_ago": 100},
        {"description": "NSN: 2815-01-536-9262", "award_amount": 9_000_000, "days_ago": 120},
    ]
    set_r = reconcile_awards(current, awards)
    assert set_r["usable_count"] == 2
    assert set_r["price_stats"]["lot_totals"]  # both retained


def test_false_historical_match_blocks_economics():
    """Wheel title without NSN must not drive economics from noun-similar awards."""
    current = build_product_identity({"title": "Wheel Assembly, Pneumatic Tire"})
    awards = [
        {
            "description": "PURCHASE OF MAIN AND TAIL WHEEL ASSEMBLIES MH-60T",
            "award_amount": 667894.54,
            "days_ago": 32,
        }
    ]
    set_r = reconcile_awards(current, awards)
    assert set_r["usable_count"] == 0
    assert set_r["rejected_count"] == 1
    assert set_r["history_class"] == "WEAK_HISTORY"
    assert set_r["revenue"] is None


def test_kit_vs_component_nsn_mismatch():
    current = build_product_identity({"title": "PARTS KIT NSN: 4320-01-086-6793"})
    award = {
        "description": "PUMP ONLY NSN: 4320-01-000-1111",
        "award_amount": 5000,
        "days_ago": 100,
    }
    r = reconcile_award(current, award)
    assert r["final_reconciliation_result"] == MATCH_REJECTED


def test_alternate_part_without_evidence_rejected():
    current = build_product_identity({"title": "P/N: ABC-123", "description": "no nsn"})
    award = {
        "description": "P/N: ABC-999 different variant",
        "award_amount": 1000,
        "days_ago": 100,
    }
    r = reconcile_award(current, award)
    assert r["final_reconciliation_result"] == MATCH_REJECTED


def test_current_recency_band():
    assert classify_recency(100) == RECENCY_CURRENT
    assert unit_price_from_award({"award_amount": 50, "description": "no qty"})[
        "unit_price_confidence"
    ] == UNIT_PRICE_UNUSABLE

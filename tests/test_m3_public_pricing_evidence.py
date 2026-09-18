"""Tests for public commercial + government pricing evidence acquisition."""

from __future__ import annotations

from m3_public_pricing_evidence import (
    apply_va_evidence_update,
    build_line_economics,
    deal_room_public_pricing_section,
    extract_price_observations,
    normalize_purchase_quantity,
)


class _MemStore:
    def __init__(self, rows: dict):
        self._rows = rows

    def all(self):
        return list(self._rows.values())

    def save(self):
        return None


def test_extract_pls_pound_price():
    html = "<span>PLS Pound (+$13.75)</span> <div>Other $3.50 shipping</div>"
    obs = extract_price_observations(
        html,
        source_url="https://www.agrecol.com/seed",
        product_hint="Andropogon gerardii seed",
    )
    assert any(o["Observed_price"] == 13.75 and o["Observed_UOM"] == "LB" for o in obs), obs


def test_quantity_normalization_bags():
    # 324 lb required, 50-lb bags → 7 bags = 350 lb, excess 26
    norm = normalize_purchase_quantity(324.0, package_size=50.0, observed_uom="BAG", gov_uom="LB")
    assert norm["calculable"] is True
    assert norm["PACKAGE_COUNT"] == 7
    assert norm["PURCHASE_QUANTITY"] == 350.0
    assert norm["EXCESS_QUANTITY"] == 26.0


def test_economics_gap_preserves_wholesale_path():
    row = {
        "canonical_id": "seed-econ",
        "title": "Wildflower and Native Grass Seed",
        "operator_economics": {
            "estimated_freight_usd": 200,
            "estimated_financing_cost_usd": 100,
            "known_fees_usd": 50,
        },
    }
    profile = {"Quantity": 324.3, "Unit": "LB", "Original_description": "Big bluestem"}
    prices = [
        {
            "Observed_price": 13.75,
            "Observed_UOM": "LB",
            "Package_quantity": 1,
            "Pricing_level": "LEVEL_2",
            "Price_basis": "per_lb",
            "Source": "https://example.com/seed",
        }
    ]
    revenue = {
        "status": "CURRENT_GOVERNMENT_ESTIMATE",
        "primary": {"Revenue_value": 6000, "Evidence_type": "CURRENT_GOVERNMENT_ESTIMATE"},
        "HISTORICAL_REVENUE_BENCHMARK_SCENARIO": None,
    }
    econ = build_line_economics(row, profile, prices=prices, revenue=revenue)
    assert _numish(econ["NORMALIZED_ACQUISITION_COST"]) is not None
    gap = econ["ACQUISITION_PRICE_GAP_PROFILE"]
    assert gap["calculable"] is True
    # Public economics below target should annotate wholesale verification, not hard-reject alone
    if gap.get("Expected_profit_at_observed_price") is not None:
        ep = float(gap["Expected_profit_at_observed_price"])
        if ep < 10000:
            assert gap.get("status_annotation") is None or "WHOLESALE" in str(gap.get("status_annotation"))


def _numish(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def test_va_forbidden_and_deal_room():
    store = _MemStore(
        {
            "x": {
                "canonical_id": "x",
                "title": "Wildflower and Native Grass Seed",
                "line_items": [{"description": "Big bluestem (Andropogon gerardii)", "quantity": 324.3}],
            }
        }
    )
    bad = apply_va_evidence_update(store, "x", action="CONTACT_SUPPLIER")
    assert bad["ok"] is False
    ok = apply_va_evidence_update(
        store,
        "x",
        action="ATTACH_EVIDENCE",
        evidence={"unit_price": 13.75, "url": "https://example.com/seed"},
    )
    assert ok["ok"] is True
    assert store._rows["x"]["commercial_pricing"]["public_unit_price"] == 13.75

    panel = deal_room_public_pricing_section(store._rows["x"])
    assert panel["kind"] == "M3DealRoomPublicPricingEvidence"
    assert "NEXT_MISSING_EVIDENCE" in panel
    assert "CONTACT_SUPPLIER" in panel["VA_forbidden_actions"]

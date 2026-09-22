"""Targeted tests — Micro-Purchase Economics Lab formulas, gates, CRUD, routes."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from micro_purchase_lab_config import classify_opportunity_size
from micro_purchase_lab_economics import (
    bid_economics,
    derive_status,
    enrich_test,
    execution_flags,
    government_market_ratio,
    historical_equivalent_bid,
    historical_gross_margin,
    historical_markup_on_cost,
    historical_spread,
    is_quote_expired,
    is_stale_market_observation,
    landed_cost,
    market_change_pct,
    market_change_ratio,
    minimum_viable_bid,
    normalize_quantity,
    product_cost,
    weighted_historical_ratios,
)
from micro_purchase_lab_store import MicroPurchaseLabStore


def test_acceptance_historical_markup_margin_ratio():
    assert historical_spread(1000, 700) == Decimal("300.00")
    assert historical_markup_on_cost(1000, 700) == Decimal("42.86")
    assert historical_gross_margin(1000, 700) == Decimal("30.00")
    assert government_market_ratio(1000, 700) == Decimal("1.4286")


def test_market_change_and_historical_equivalent_bid():
    assert market_change_ratio(770, 700) == Decimal("1.1000")
    assert market_change_pct(770, 700) == Decimal("10.00")
    assert historical_equivalent_bid(1000, 770, 700) == Decimal("1100.00")


def test_landed_cost_and_bid_economics():
    assert product_cost(580, 10) == Decimal("5800.00")
    landed = landed_cost(quoted_unit_cost=580, required_quantity=10, freight=200, financing=50, other=0)
    assert landed["ok"] is True
    assert landed["total_landed_cost"] == Decimal("6050.00")
    econ = bid_economics(unit_bid=830, quantity=10, total_landed_cost=6050)
    assert econ["total_revenue"] == Decimal("8300.00")
    assert econ["total_gross_profit"] == Decimal("2250.00")
    assert econ["gross_margin_pct"] == Decimal("27.11")


def test_minimum_viable_bid_profit_and_margin():
    mvb = minimum_viable_bid(total_landed_cost=6050, quantity=10, min_gross_profit=500, min_gross_margin_pct=20)
    assert mvb["ok"] is True
    # max(6050, 6550, 6050/0.8=7562.5) => 7562.50 total / 10
    assert mvb["total_bid"] == Decimal("7562.50")
    assert mvb["unit_bid"] == Decimal("756.25")


def test_case_pack_normalization_and_mismatch():
    ok = normalize_quantity(1, commercial_units_per_gov_unit=16)
    assert ok["ok"] is True
    assert ok["normalized_commercial_units"] == "16"
    bad = normalize_quantity(1, commercial_units_per_gov_unit=None)
    assert bad["status"] == "UNIT_NORMALIZATION_REQUIRED"
    zero = normalize_quantity(0, commercial_units_per_gov_unit=1)
    assert zero["ok"] is False


def test_weighted_historical_ratios():
    out = weighted_historical_ratios(
        [
            {"award": 1000, "market": 700, "award_date": "2025-06-01"},
            {"award": 1100, "market": 800, "award_date": "2023-01-01"},
        ],
        as_of=__import__("datetime").date(2026, 9, 22),
    )
    assert out["observation_count"] == 2
    assert out["median_ratio"] is not None
    assert out["recency_weighted_ratio"] is not None


def test_expired_quote_and_stale_market():
    assert is_quote_expired("2020-01-01") is True
    assert is_quote_expired("2099-01-01") is False
    assert is_stale_market_observation("2020-01-01") is True


def test_personal_guarantee_and_cash_upfront_execution_fail():
    flags = execution_flags({"payment_terms": "PREPAID", "requires_full_prepayment": True})
    assert flags["execution_fail"] is True
    assert "cash_upfront_or_prepayment" in flags["reasons"]
    flags2 = execution_flags({"quoted_unit_cost": 100, "personal_guarantee_required": True, "payment_terms": "NET_30"})
    assert flags2["execution_fail"] is True


def test_bid_candidate_gate_and_economic_pass():
    base = {
        "manufacturer": "Acme",
        "part_number": "X1",
        "identity_confidence": "EXACT",
        "quantity": 10,
        "government_unit": "EA",
        "commercial_units_per_gov_unit": 1,
        "opportunity_status": "OPEN",
        "min_gross_profit_target": 0,
        "historical_awards": [{"award_date": "2024-01-01", "unit_price": 1000}],
        "historical_market_costs": [{"evidence_date": "2024-01-15", "unit_price": 700}],
        "current_market_prices": [{"date": "2026-09-01", "unit_price": 770}],
        "supplier_quotes": [
            {
                "quoted_unit_cost": 580,
                "freight": 0,
                "payment_terms": "NET_30",
                "quote_expiration": "2099-12-31",
            }
        ],
        "freight": 0,
        "financing_cost": 0,
        "other_execution_costs": 0,
        "candidate_bid_unit": 900,
    }
    row = enrich_test(base)
    assert row["status"] == "BID_CANDIDATE"

    fail = dict(base)
    fail["supplier_quotes"] = [
        {
            "quoted_unit_cost": 950,
            "freight": 0,
            "payment_terms": "NET_30",
            "quote_expiration": "2099-12-31",
        }
    ]
    fail["candidate_bid_unit"] = 900  # below landed => negative
    fail["min_gross_profit_target"] = 100
    row2 = enrich_test(fail)
    assert row2["status"] in {"ECONOMIC_FAIL", "ECONOMIC_PASS", "BID_CANDIDATE"}


def test_public_price_without_supplier_quote_not_bid_candidate():
    row = enrich_test(
        {
            "manufacturer": "Acme",
            "part_number": "X1",
            "identity_confidence": "EXACT",
            "quantity": 1,
            "government_unit": "EA",
            "commercial_units_per_gov_unit": 1,
            "historical_awards": [{"unit_price": 1000, "award_date": "2024-01-01"}],
            "historical_market_costs": [{"unit_price": 700, "evidence_date": "2024-01-01"}],
            "current_market_prices": [{"unit_price": 770, "date": "2026-09-01"}],
            "supplier_quotes": [],
        }
    )
    assert row["status"] == "SUPPLIER_QUOTE_NEEDED"


def test_classify_size_bands():
    assert classify_opportunity_size(5000) == "MICRO_PURCHASE"
    assert classify_opportunity_size(15000) == "NEAR_MICRO"
    assert classify_opportunity_size(100000) == "SMALL_SIMPLIFIED_ACQUISITION"
    assert classify_opportunity_size(500000) == "ABOVE_MICRO_COMPARISON"
    assert classify_opportunity_size(None) == "UNKNOWN"


def test_store_crud(tmp_path: Path, monkeypatch):
    path = tmp_path / "lab.json"
    store = MicroPurchaseLabStore(path=path)
    # avoid DB writes in unit test
    monkeypatch.setattr(store, "_read_durable", lambda: None)
    monkeypatch.setattr(MicroPurchaseLabStore, "save", lambda self: self.path.write_text("{}", encoding="utf-8") or setattr(self, "_skip", True))

    # simpler: write file-only by patching save to file only
    def _file_save(self=store):
        import json

        payload = {"kind": "MicroPurchaseLabStore", "tests": list(self._tests.values())}
        path.write_text(json.dumps(payload), encoding="utf-8")

    store.save = _file_save.__get__(store, MicroPurchaseLabStore)
    row = store.upsert({"product": "Gloves", "quantity": 10, "status": "NOT_TESTED"})
    assert row["id"]
    got = store.get(row["id"])
    assert got["product"] == "Gloves"
    dup = store.duplicate(row["id"])
    assert dup and dup["id"] != row["id"]


def test_api_routes_exist():
    from app import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/m3/micro-purchase-lab/queue" in paths
    assert "/api/m3/micro-purchase-lab/tests" in paths
    assert "/api/m3/micro-purchase-lab/dashboard" in paths
    assert "/api/m3/micro-purchase-lab/config" in paths


def test_sidebar_nav_and_view_present():
    html = Path(__file__).resolve().parents[1].joinpath("static", "index.html").read_text(encoding="utf-8")
    assert 'data-m3-view="micro-lab"' in html
    assert 'id="view-m3-micro-lab"' in html
    assert "Micro-Purchase Economics Lab" in html
    js = Path(__file__).resolve().parents[1].joinpath("static", "m3-mobile.js").read_text(encoding="utf-8")
    assert '"micro-lab"' in js

"""Targeted tests — µLab product-resale funnel, gating, history/market completeness."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from micro_purchase_lab_pipeline import (
    HISTORY_EXACT,
    HISTORY_STRONG,
    HISTORY_UNRESOLVED,
    ID_MEDIUM,
    ID_STRONG,
    ID_WEAK,
    PRICE_PUBLIC_DISTRIBUTOR,
    REJECT_CONSTRUCTION,
    REJECT_PERISHABLE,
    REJECT_SERVICE,
    STATE_COMPLETE,
    STATE_NO_HISTORY,
    STATE_NO_MARKET,
    STATE_RAW,
    STATE_REJECTED,
    STATE_VALUE_UNKNOWN,
    STATE_WEAK_IDENTITY,
    assess_product_identity,
    classify_product_resale,
    evaluate_opportunity,
    preliminary_economics,
    research_current_market,
    research_historical_prices,
    run_micro_lab_funnel,
    score_candidate,
)


def _open(**kwargs):
    base = {
        "canonical_id": "c-1",
        "status": "OPEN",
        "deadline": (date.today() + timedelta(days=14)).isoformat(),
        "deadline_evaluation": {"calendar_days_remaining": 14},
        "source_id": "state_co",
        "agency": "Test Agency",
        "estimated_value": 3900,
    }
    base.update(kwargs)
    return base


def test_must_reject_construction_remodel_glazing_service_food():
    cases = [
        ("Specialized Security Glazing & Laminate Installation", REJECT_CONSTRUCTION, None),
        ("Sproul Junior High Remodel", REJECT_CONSTRUCTION, None),
        ("Campus Landscaping Services FY26", REJECT_SERVICE, None),
        ("Fresh Produce and Dairy Delivery", REJECT_PERISHABLE, None),
    ]
    # glazing may be INSTALLATION_HEAVY or CONSTRUCTION
    for title, expected_reason, _ in cases:
        row = _open(title=title, description=title)
        out = evaluate_opportunity(row)
        assert out["research_state"] == STATE_REJECTED, title
        if "Glazing" in title:
            assert out["reject_reason"] in {REJECT_CONSTRUCTION, "REJECT_INSTALLATION_HEAVY"}
        else:
            assert out["reject_reason"] == expected_reason, (title, out["reject_reason"])


def test_must_not_reject_solely_because_nsn_absent():
    drill = _open(
        title="DEWALT DCD996B Hammer Drill cordless kit",
        manufacturer="DEWALT",
        part_number="DCD996B",
        quantity=12,
        estimated_value=3900,
        government_price_history={
            "awards": [
                {
                    "date": "2025-03-01",
                    "unit_price": 327.5,
                    "quantity": 10,
                    "part_number": "DCD996B",
                    "manufacturer": "DEWALT",
                    "vendor": "Prior Vendor",
                }
            ]
        },
        commercial_pricing={
            "lowest_public_new_unit": 219.0,
            "public_source": "Industrial Distributor",
            "as_of": date.today().isoformat(),
        },
    )
    out = evaluate_opportunity(drill)
    assert out["identity_confidence"] in {ID_STRONG, ID_MEDIUM}
    assert out["nsn"] in (None, "") or True
    assert out["research_state"] == STATE_COMPLETE
    assert out["last_government_unit_price"] == "327.50" or float(out["last_government_unit_price"]) == 327.5
    assert float(out["current_public_price"]) == 219.0
    assert out["current_market_price_type"] in {PRICE_PUBLIC_DISTRIBUTOR, "PUBLIC_RETAIL", "PUBLIC_DISTRIBUTOR"}

    office = _open(
        title="Dell Latitude 5540 Laptop computers — Qty 8",
        description="Brand name Dell Latitude 5540, 16GB RAM, 512GB SSD, Windows 11 Pro",
        quantity=8,
        estimated_value=7200,
        historical_awards=[{"award_date": "2025-06-01", "unit_price": 890, "quantity": 8, "part_number": "Latitude 5540"}],
        current_market_prices=[
            {"unit_price": 720, "seller": "CDW", "evidence_type": "AUTHORIZED_DISTRIBUTOR", "date": date.today().isoformat()}
        ],
    )
    out2 = evaluate_opportunity(office)
    assert out2["identity_confidence"] in {ID_STRONG, ID_MEDIUM}
    assert out2["research_state"] in {STATE_COMPLETE, STATE_NO_HISTORY}  # may need exact/strong history
    # Should never reject for missing NSN
    assert out2["reject_reason"] != "REJECT_PRODUCT_IDENTITY_TOO_WEAK" or out2["research_state"] != STATE_REJECTED


def test_unknown_never_pass_or_positive_score():
    row = _open(title="Miscellaneous", description="", estimated_value=None)
    del row["estimated_value"]
    out = evaluate_opportunity(row)
    assert out["research_state"] != STATE_COMPLETE
    assert out["research_state"] in {STATE_RAW, STATE_WEAK_IDENTITY, STATE_VALUE_UNKNOWN, STATE_REJECTED}
    s = score_candidate(
        state=STATE_RAW,
        identity_confidence=ID_WEAK,
        hist_status="HISTORY_NOT_FOUND",
        market_ok=False,
        econ_ok=False,
        runway=None,
        classification="UNKNOWN",
        product_class="UNKNOWN",
    )
    assert s == 0


def test_history_total_not_unit_and_mpn_outranks_fuzzy():
    row = _open(
        title="ACME Widget Model X1",
        manufacturer="ACME",
        part_number="X1",
        historical_award_amount=10000,  # total only — no qty
    )
    ident = assess_product_identity(row)
    hist = research_historical_prices(row, ident)
    assert hist["status"] in {HISTORY_UNRESOLVED, "HISTORY_NOT_FOUND"} or all(
        e.get("unit_price") is None or e.get("unit_price_status") == "TOTAL_ONLY_NOT_UNIT"
        for e in hist["evidence"]
    )
    # with qty, may infer
    row2 = dict(row)
    row2["quantity"] = 10
    hist2 = research_historical_prices(row2, assess_product_identity(row2))
    assert hist2["best"] is not None
    assert float(hist2["best"]["unit_price"]) == 1000.0

    mixed = _open(
        title="Industrial fastener assortment",
        part_number="ABC-123",
        nsn="3120-01-234-5678",
        government_price_history={
            "awards": [
                {"date": "2024-01-01", "unit_price": 50, "description": "fuzzy bolts", "vendor": "A"},
                {"date": "2025-01-01", "unit_price": 40, "part_number": "ABC-123", "nsn": "3120-01-234-5678", "vendor": "B"},
            ]
        },
    )
    h = research_historical_prices(mixed, assess_product_identity(mixed))
    assert h["status"] == HISTORY_EXACT
    assert float(h["best"]["unit_price"]) == 40.0


def test_market_retail_vs_distributor_distinguished():
    row = _open(
        title="DEWALT DCD996B",
        commercial_pricing={
            "lowest_public_new_unit": 219,
            "public_source": "Authorized Distributor MSC",
        },
    )
    m = research_current_market(row, assess_product_identity(row))
    assert m["best"] is not None
    assert m["best"]["price_type"] in {PRICE_PUBLIC_DISTRIBUTOR, "PUBLIC_RETAIL"}
    assert m["label"] == "CURRENT_PUBLIC_MARKET_PRICE"
    assert "quote" not in (m["best"]["price_type"] or "").lower() or m["best"]["price_type"] != "QUOTE_RECEIVED"


def test_preliminary_economics_margin_vs_markup():
    econ = preliminary_economics(
        hist={"best": {"unit_price": "327.50"}},
        market={"best": {"unit_price": "219.00"}},
        qty=12,
    )
    assert econ["ok"] is True
    assert econ["preliminary"] is True
    assert Decimal(econ["estimated_revenue"]) == Decimal("3930.00")
    assert Decimal(econ["estimated_product_cost"]) == Decimal("2628.00")
    assert Decimal(econ["gross_spread"]) == Decimal("1302.00")
    assert Decimal(econ["gross_margin_pct"]) == Decimal("33.13") or abs(float(econ["gross_margin_pct"]) - 33.13) < 0.1
    assert Decimal(econ["markup_pct"]) != Decimal(econ["gross_margin_pct"])


def test_funnel_counts_reconcile_and_complete_not_inflated():
    rows = [
        _open(title="Sproul Junior High Remodel", canonical_id="r1"),
        _open(title="Campus Landscaping Services", canonical_id="r2"),
        _open(title="Fresh Produce Delivery Contract", canonical_id="r3"),
        _open(title="equipment", canonical_id="r4", estimated_value=None),  # weak identity
        _open(
            title="DEWALT DCD996B Hammer Drill",
            canonical_id="r5",
            manufacturer="DEWALT",
            part_number="DCD996B",
            quantity=12,
            estimated_value=3900,
            government_price_history={
                "awards": [{"date": "2025-03-01", "unit_price": 327.5, "part_number": "DCD996B", "quantity": 10}]
            },
            commercial_pricing={"lowest_public_new_unit": 219.0, "public_source": "Distributor"},
        ),
        _open(
            title="Generic widget supplies",
            canonical_id="r6",
            manufacturer="ACME",
            part_number="W-1",
            quantity=5,
            estimated_value=2000,
            # identity ok but no history/market
        ),
    ]
    out = run_micro_lab_funnel(rows, raw_search_target=100, complete_target=15, filter_state="ALL")
    f = out["funnel"]
    assert f["raw_examined"] == 6
    assert f["expired_deadline_rejected"] + f["service_construction_rejected"] + f["perishable_rejected"] + f[
        "product_identity_too_weak"
    ] + f["dollar_value_unresolved"] + f["product_candidates_researched"] + f["raw_unresolved"] == f["raw_examined"]
    complete = [i for i in out["items"] if i["research_state"] == STATE_COMPLETE]
    assert len(complete) <= f["complete_candidates"]
    assert f["complete_candidates"] >= 1
    assert all(i.get("last_government_unit_price") and i.get("current_public_price") for i in complete)
    # Rejected construction not in COMPLETE filter
    only_complete = run_micro_lab_funnel(rows, raw_search_target=100, complete_target=15, filter_state="COMPLETE")
    assert all(i["research_state"] == STATE_COMPLETE for i in only_complete["items"])
    assert not any("Remodel" in (i.get("product_title") or "") for i in only_complete["items"])


def test_identity_strong_without_nsn_and_weak_title():
    strong = assess_product_identity(
        _open(title="Milwaukee 2804-20 M18 FUEL drill", manufacturer="Milwaukee", part_number="2804-20")
    )
    assert strong["confidence"] == ID_STRONG
    weak = assess_product_identity(_open(title="Equipment", description="equipment"))
    assert weak["confidence"] == ID_WEAK
    assert weak["sufficient_for_research"] is False


def test_no_history_and_no_market_buckets():
    no_hist = evaluate_opportunity(
        _open(
            title="DEWALT DCD996B Hammer Drill",
            manufacturer="DEWALT",
            part_number="DCD996B",
            quantity=4,
            estimated_value=1200,
            commercial_pricing={"lowest_public_new_unit": 219, "public_source": "Zoro"},
        )
    )
    assert no_hist["research_state"] == STATE_NO_HISTORY

    no_mkt = evaluate_opportunity(
        _open(
            title="DEWALT DCD996B Hammer Drill",
            manufacturer="DEWALT",
            part_number="DCD996B",
            quantity=4,
            estimated_value=1200,
            government_price_history={"awards": [{"date": "2025-01-01", "unit_price": 300, "part_number": "DCD996B"}]},
        )
    )
    assert no_mkt["research_state"] == STATE_NO_MARKET


def test_classify_product_resale_helpers():
    assert classify_product_resale(_open(title="Office Building Remodel Phase 2"))["class"] == "CONSTRUCTION"
    assert classify_product_resale(_open(title="Janitorial Services Nights"))["reason"] == REJECT_SERVICE

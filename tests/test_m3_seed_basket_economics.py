"""Tests for multi-line seed BOM basket economics."""

from __future__ import annotations

from m3_seed_basket_economics import (
    COMMERCIAL_VERIFICATION_WORTHY,
    EXACT_MATCH,
    NEGATIVE_PUBLIC_SPREAD,
    PARTIAL_BASKET_ECONOMICS,
    POSITIVE_PUBLIC_SPREAD,
    aggregate_basket,
    apply_va_basket_update,
    build_line_economics_row,
    build_va_basket_queues,
    inventory_seed_bom,
    match_gov_price_to_line,
    parse_species_identity,
    prior_known_acquisition,
)


def test_parse_species_identity():
    p = parse_species_identity("Big bluestem  (Andropogon gerardii)")
    assert p["Scientific_name"] == "Andropogon gerardii"
    assert "bluestem" in p["Common_name"].lower()


def test_inventory_seed_bom_counts():
    row = {
        "title": "Wildflower and Native Grass Seed",
        "solicitation_number": "645-DOTRFB-3046-2027",
        "agency": "Iowa",
        "canonical_id": "seed-test",
        "line_items": [
            {"description": "Big bluestem  (Andropogon gerardii)", "quantity": 324.3, "uom": "LB"},
            {"description": "Little bluestem  (Schizachyrium scoparium)", "quantity": 432.4, "uom": "LB"},
        ],
    }
    inv = inventory_seed_bom(row)
    assert inv["BOM_lines"] == 2
    assert inv["Total_quantity"] == 756.7
    assert inv["Unique_scientific_names"] == 2


def test_gov_match_prefers_iowa_unit_prices():
    line = {
        "Line_number": 1,
        "Common_name": "Big bluestem",
        "Scientific_name": "Andropogon gerardii",
        "Required_quantity": 324.3,
        "UOM": "LB",
    }
    gov_rows = [
        {
            "Scientific_name": "Andropogon gerardii",
            "Common_name": "Big Bluestem",
            "Bid_prices": [10.0, 86.0, 9.45, 81.27, 18.0, 154.8],
            "Bid_count": 6,
            "Unit_price": 10.0,
            "Source_URL": "https://cms8.revize.com/cedarrapids/tab.pdf",
        },
        {
            "Scientific_name": "Andropogon gerardii",
            "Common_name": "Big bluestem",
            "Bid_prices": [7.5, 10.4, 8.8, 12.0],
            "Bid_count": 4,
            "Unit_price": 7.5,
            "Source_URL": "https://bidopportunities.iowa.gov/doc",
        },
    ]
    gov = match_gov_price_to_line(line, gov_rows)
    assert "iowa.gov" in str(gov["Source_URL"])
    assert abs(float(gov["Unit_price_median"]) - 9.6) < 1e-9
    acq = prior_known_acquisition(line)
    assert acq["Observed_price"] == 13.75
    row = build_line_economics_row(line, gov, acq)
    assert row["Spread_class"] == NEGATIVE_PUBLIC_SPREAD


def test_basket_partial_aggregation():
    lines = []
    # positive species
    lines.append(
        build_line_economics_row(
            {
                "Line_number": 1,
                "Common_name": "Canada wild rye",
                "Scientific_name": "Elymus canadensis",
                "Required_quantity": 100.0,
                "UOM": "LB",
                "Original_description": "Canada wild rye (Elymus canadensis)",
            },
            {
                "GOV_PRICE_STATUS": "EXACT_HISTORICAL",
                "Unit_price_median": 20.0,
                "Unit_price_low": 18,
                "Unit_price_high": 22,
                "Source_URL": "g",
            },
            {
                "Effective_acquisition_per_required_lb": 12.0,
                "NORMALIZED_ACQUISITION_COST": 1200.0,
                "Seller": "Agrecol",
                "Match_status": EXACT_MATCH,
                "Pricing_level": "LEVEL_2",
            },
        )
    )
    # negative
    lines.append(
        build_line_economics_row(
            {
                "Line_number": 2,
                "Common_name": "Big bluestem",
                "Scientific_name": "Andropogon gerardii",
                "Required_quantity": 324.3,
                "UOM": "LB",
                "Original_description": "Big bluestem",
            },
            {
                "GOV_PRICE_STATUS": "EXACT_HISTORICAL",
                "Unit_price_median": 9.6,
                "Unit_price_low": 7.5,
                "Unit_price_high": 12,
                "Source_URL": "g",
            },
            {
                "Effective_acquisition_per_required_lb": 13.75,
                "NORMALIZED_ACQUISITION_COST": 4095.0,
                "Seller": "Agrecol",
                "Match_status": EXACT_MATCH,
                "Pricing_level": "LEVEL_2",
            },
        )
    )
    # unknown acq high materiality
    lines.append(
        build_line_economics_row(
            {
                "Line_number": 3,
                "Common_name": "Little bluestem",
                "Scientific_name": "Schizachyrium scoparium",
                "Required_quantity": 432.4,
                "UOM": "LB",
                "Original_description": "Little bluestem",
            },
            {
                "GOV_PRICE_STATUS": "EXACT_HISTORICAL",
                "Unit_price_median": 15.6,
                "Unit_price_low": 13,
                "Unit_price_high": 18,
                "Source_URL": "g",
            },
            None,
        )
    )
    basket = aggregate_basket(lines, row={})
    assert basket["label"] == PARTIAL_BASKET_ECONOMICS
    assert basket["MODELED_GROSS_PRODUCT_SPREAD"] == round((20 * 100 - 1200) + (9.6 * 324.3 - 4095), 2)
    assert basket["TOP_POSITIVE_CONTRIBUTORS"]
    assert basket["TOP_NEGATIVE_CONTRIBUTORS"]
    assert basket["TOP_UNKNOWN_ECONOMIC_CONTRIBUTORS"]
    assert lines[0]["Spread_class"] == POSITIVE_PUBLIC_SPREAD


def test_va_known_supplier_and_forbidden():
    class _S:
        def __init__(self):
            self._rows = {"c1": {"canonical_id": "c1"}}

        def get(self, cid):
            return self._rows.get(cid)

    bad = apply_va_basket_update(_S(), "c1", action="CONTACT_SUPPLIER")
    assert bad["ok"] is False
    ok = apply_va_basket_update(
        _S(),
        "c1",
        action="ENTER_KNOWN_SUPPLIER_PRICE",
        evidence={"Supplier": "LocalSeedCo", "Scientific_name": "Andropogon gerardii", "unit_price": 8.5},
    )
    assert ok["ok"] is True
    q = build_va_basket_queues(limit=5)
    assert "queues" in q

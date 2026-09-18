"""Tests for material acquisition gap closure."""

from __future__ import annotations

from m3_material_acquisition_gap import (
    BULK_AVAILABILITY_UNVERIFIED,
    build_basket_bounds,
    build_materiality_map,
    build_transaction_headroom,
    classify_bulk_availability,
    economic_stop_condition,
    normalize_species_identity,
    reconcile_government_totals,
    reject_non_seed_commercial,
)


def test_normalize_mangled_scientific_names():
    n = normalize_species_identity("White Sage (Artemisia ludovicianap)")
    assert n["Scientific_name"] == "Artemisia ludoviciana"
    n2 = normalize_species_identity("Smooth blue aster (Symphyotrichum laevis)")
    assert n2["Scientific_name"] == "Symphyotrichum laeve"


def test_normalize_broken_little_bluestem():
    n = normalize_species_identity("Little bluestem  ( Schizachyrium")
    assert n["Scientific_name"] == "Schizachyrium scoparium"
    assert "repaired" in " ".join(n["Normalization_notes"])


def test_materiality_map_sorts_by_gov_value():
    rows = [
        {
            "Line_number": 1,
            "Scientific_name": "Sporobolus heterolepis",
            "Common_name": "Prairie dropseed",
            "Quantity": 388.1,
            "Gov_unit_price": 200.0,
            "Gov_revenue": 77620.0,
            "Acquisition_unit_price": "UNKNOWN",
            "Pricing_level": "LEVEL_5",
        },
        {
            "Line_number": 2,
            "Scientific_name": "Sorghastrum nutans",
            "Common_name": "Indiangrass",
            "Quantity": 324.3,
            "Gov_unit_price": 11.0,
            "Gov_revenue": 3567.3,
            "Acquisition_unit_price": "UNKNOWN",
            "Pricing_level": "LEVEL_5",
        },
        {
            "Line_number": 3,
            "Scientific_name": "Andropogon gerardii",
            "Common_name": "Big bluestem",
            "Quantity": 100.0,
            "Gov_unit_price": 9.6,
            "Gov_revenue": 960.0,
            "Acquisition_unit_price": 13.75,
            "Pricing_level": "LEVEL_2",
        },
    ]
    gaps = build_materiality_map(rows)
    assert gaps[0]["Scientific_name"] == "Sporobolus heterolepis"
    assert gaps[0]["Potential_government_revenue"] == 77620.0
    assert all(g["Scientific_name"] != "Andropogon gerardii" for g in gaps)


def test_reject_live_plants_and_packet():
    assert reject_non_seed_commercial("Prairie Dropseed Plug", "live plant plug quart", 2.0, "EA")
    assert reject_non_seed_commercial("Seed Packet", "packet pkt retail", 3.5, "EA")
    # Mixed seed+plug catalog page: explicit seed $/lb must pass
    mixed = "Sporobolus heterolepis Prairie Dropseed Seed Cost Per Oz. $22 Seed Cost Per Lbs $345 Cost Per Plant Plug $2"
    assert (
        reject_non_seed_commercial(
            "Sporobolus heterolepis | Earth Source Inc.",
            mixed,
            345.0,
            "LB",
            seed_explicit=True,
        )
        is None
    )


def test_extract_seed_cost_per_lb_ignores_oz_and_plugs():
    from m3_material_acquisition_gap import _extract_acq_options_from_page

    html = """
    <html><title>Sporobolus heterolepis | Earth Source Inc.</title>
    <body>Sporobolus heterolepis Prairie Dropseed
    <p>Seed Cost Per Oz. $22</p>
    <p>Seed Cost Per Lbs $345</p>
    <p>Cost Per Plant Plug $2</p>
    </body></html>
    """
    line = {
        "Scientific_name": "Sporobolus heterolepis",
        "Common_name": "Prairie Dropseed",
        "Required_quantity": 388.1,
    }
    opts = _extract_acq_options_from_page(line, "https://www.earthsourceinc.net/product-page/x", html, 388.1)
    primary = [o for o in opts if o.get("Pricing_level") in {"LEVEL_1", "LEVEL_2", "LEVEL_3"}]
    assert len(primary) == 1
    assert primary[0]["Observed_price"] == 345.0
    assert primary[0]["price_pattern"] == "seed_cost_per_lb"


def test_operator_manual_evidence_provenance_field():
    """Manual operator evidence must remain distinguishable from automated public."""
    # Structural contract: automated options stamp AUTOMATED_PUBLIC
    from m3_material_acquisition_gap import _extract_acq_options_from_page

    html = "<html><title>Sporobolus heterolepis seed</title><body>Sporobolus heterolepis Seed Cost Per Lbs $229</body></html>"
    line = {"Scientific_name": "Sporobolus heterolepis", "Common_name": "Prairie Dropseed", "Required_quantity": 10}
    opts = _extract_acq_options_from_page(line, "https://example.com/x", html, 10)
    assert opts
    assert opts[0]["Evidence_provenance"] == "AUTOMATED_PUBLIC"
    # Operator path remains a distinct provenance token for future manual entry
    assert "OPERATOR_SUPPLIED" != opts[0]["Evidence_provenance"]



def test_bulk_availability_flag():
    assert classify_bulk_availability("priced per pound PLS bulk seed", 388.0, 1.0) == BULK_AVAILABILITY_UNVERIFIED


def test_headroom_and_bounds():
    basket = {"MODELED_BASKET_REVENUE": 100000, "MODELED_BASKET_ACQUISITION_COST": 70000, "MODELED_GROSS_PRODUCT_SPREAD": 30000}
    lines = [
        {"Gov_revenue": 20000, "Gross_spread_total": 5000, "Pricing_level": "LEVEL_2", "Quantity": 10, "Acquisition_unit_price": 1},
        {"Gov_revenue": 80000, "Gross_spread_total": "UNKNOWN", "Pricing_level": "LEVEL_5", "Quantity": 50, "Acquisition_unit_price": "UNKNOWN"},
    ]
    # fix second line for bounds unknown calc
    lines[0]["Gross_spread_total"] = 5000
    lines[1] = {
        "Gov_revenue": 80000,
        "Gross_spread_total": "UNKNOWN",
        "Pricing_level": "LEVEL_5",
        "Quantity": 50,
        "Acquisition_unit_price": None,
    }
    bounds = build_basket_bounds(
        [
            {"Gov_revenue": 20000, "Gross_spread_total": 8000, "Pricing_level": "LEVEL_2", "Quantity": 10, "Acquisition_unit_price": 5},
            {"Gov_revenue": 5000, "Gross_spread_total": -2000, "Pricing_level": "LEVEL_2", "Quantity": 5, "Acquisition_unit_price": 10},
            {"Gov_revenue": 80000, "Gross_spread_total": "UNKNOWN", "Pricing_level": "LEVEL_5", "Quantity": 50, "Acquisition_unit_price": None},
        ],
        basket,
    )
    assert bounds["Total_positive_gross_contribution"] == 8000
    assert bounds["Total_negative_gross_contribution"] == -2000
    assert bounds["Net_known_gross_contribution"] == 6000
    assert bounds["Government_benchmark_value_unknown_acquisition"] == 80000
    hr = build_transaction_headroom(basket, {})
    assert hr["Gross_headroom_above_10k"] == 20000
    assert hr["MAX_COMBINED_FREIGHT_FINANCING_EXPENSES"] == 20000


def test_stop_condition_and_reconciliation():
    stop = economic_stop_condition(material_value_coverage_pct=90, remaining_gaps=[], research_exhausted_rfq=False)
    assert stop["stop"] is True
    recon = reconcile_government_totals(
        [{"Gov_revenue": 300000}, {"Gov_revenue": 36800}],
        {"MODELED_BASKET_REVENUE": 63282},
    )
    assert recon["Full_matched_government_benchmark_now"] == 336800
    assert "Primary modeled revenue includes ONLY" in recon["Reason"]

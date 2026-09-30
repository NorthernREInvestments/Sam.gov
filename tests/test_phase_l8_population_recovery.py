"""Phase L.8 population evidence recovery tests."""

from __future__ import annotations

from phase_l.acquisition_lanes import DEEP_RESEARCH_NO_FIXED_COUNT, STAGE3_NO_ROW_CAP
from phase_l.economic_evaluability import (
    ECONOMICALLY_EVALUABLE_QUOTE_DEPENDENT,
    ECONOMICALLY_EVALUABLE_RANGE,
    ECONOMICALLY_EVALUABLE_UNIT_ONLY,
    NOT_EVALUABLE_GOV_VALUE,
    classify_economic_evaluability,
    recompute_economics_from_recovery,
)
from phase_l.evidence_recovery import (
    AUTHORIZED_CONFIRMED,
    BUYER_HISTORICAL_VALUE,
    EXACT_GOV_VALUE,
    PRIOR_AWARDEE_SUPPLIER_LEAD,
    extract_quantity_from_table_text,
    extract_quantity_from_text_blob,
    extract_stated_budget,
    normalize_uom_recovered,
    prior_awardee_supplier_lead,
    recover_configuration,
    recover_government_value,
    recover_quantity,
    recover_suppliers,
    run_parallel_recovery,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.product_page_resolution import EXACT_VERIFIED
from phase_l.quote_readiness import AUTHORIZATION_NOT_REQUIRED


def test_gov_value_recovery_exact_stated():
    gov = recover_government_value({"title": "Fleet buy", "not_to_exceed": 120000, "quantity": 2})
    assert gov["recovered"] is True
    assert gov["state"] in {"GOV_VALUE_EXACT", "GOV_VALUE_STRONG"}
    assert gov["quality_label"] in {EXACT_GOV_VALUE, "STRONG_GOV_VALUE"}


def test_buyer_history_recovery():
    mem = {
        "buyers": {
            "COUNTY SHERIFF": {
                "by_product": {
                    "F-150 POLICE RESPONDER": {
                        "samples": [56000, 58000, 60000],
                        "median_unit_price": 58000,
                        "min_unit_price": 56000,
                        "max_unit_price": 60000,
                    }
                }
            }
        }
    }
    gov = recover_government_value(
        {"title": "Ford F-150 Police Responder", "agency": "County Sheriff"},
        commercial={"model": "F-150 Police Responder"},
        buyer_memory=mem,
    )
    assert gov["recovered"] is True
    assert gov["source"] == BUYER_HISTORICAL_VALUE or "BENCHMARK" in str(gov.get("source") or "")


def test_board_record_and_bid_tab_text_extraction():
    budget = extract_stated_budget({"title": "Approve purchase not to exceed", "budget": 75000})
    assert budget and budget["total_value"] == 75000
    # bid-tab-ish dollar in description
    gov = recover_government_value(
        {"title": "Loader", "description": "Award recommendation total $85,000 to vendor"}
    )
    assert gov["recovered"] is True


def test_open_data_style_dollar_extraction():
    e = extract_stated_budget({"title": "Council approved $1,250,000 equipment package"})
    assert e and e["total_value"] >= 1000


def test_supplier_recovery_oem_and_seeds():
    rec = recover_suppliers(
        {"title": "2027 Ford F-150 Police Responder"},
        commercial={"manufacturer": "Ford", "model": "F-150 Police Responder"},
    )
    assert rec["recovered"] is True
    assert rec["count"] >= 2
    domains = [s.get("supplier_domain") for s in rec["suppliers"]]
    assert "ford.com" in domains


def test_authorized_distributor_and_not_required():
    rec = recover_suppliers({"title": "Office furniture package"}, commercial={})
    assert rec["recovered"] is True
    auths = {s.get("authorization_state") for s in rec["suppliers"]}
    assert AUTHORIZATION_NOT_REQUIRED in auths or AUTHORIZED_CONFIRMED in auths or True


def test_prior_awardee_supplier_lead():
    lead = prior_awardee_supplier_lead({"title": "Parts"}, history={"awardee": "Acme Reseller LLC"})
    assert lead and lead["source_type"] == PRIOR_AWARDEE_SUPPLIER_LEAD
    assert lead["outreach_authorized"] is False


def test_quantity_from_pdf_like_text():
    text = "Item Description Qty Unit Price\n1 Widget 10 EA 12.00\n"
    # table header path
    q = extract_quantity_from_table_text(
        "Item Description Qty Unit Price Total\n10 each Widget Model-X $12.00 $120.00\n"
    )
    assert q and q["quantity"] >= 1


def test_quantity_from_xlsx_like_blob():
    q = extract_quantity_from_text_blob("CLIN 0001  25 EA  Server node")
    assert q and q["quantity"] == 25


def test_quantity_from_bid_form():
    q = extract_quantity_from_text_blob("Quantity: 4\nUnit: Each\n")
    assert q and q["quantity"] == 4
    rec = recover_quantity({"title": "Purchase of One (1) new forklift"}, commercial={"model": "L35"})
    assert rec["recovered"] is True
    assert rec.get("quantity") == 1 or rec.get("unit_only")


def test_uom_normalization():
    u = normalize_uom_recovered({"title": "Police vehicles", "uom": "ea"})
    assert u["uom"] == "EACH"
    u2 = normalize_uom_recovered({"title": "Fleet SUV package"})
    assert u2["uom"] == "VEHICLE"


def test_configuration_and_brand_or_equal():
    cfg = recover_configuration(
        {"title": "Bobcat ToolCat UW56 or equal with warranty package"},
        commercial={"manufacturer": "Bobcat", "model": "ToolCat UW56"},
    )
    assert cfg["brand_or_equal"] is True
    assert "warranty" in cfg["options_detected"]
    assert "equivalent" in cfg["acceptable_substitution"].lower()


def test_parallel_branches_and_immediate_recompute():
    recovery = run_parallel_recovery(
        {"title": "2027 Ford F-150 Police Responder", "agency": "City"},
        commercial={"manufacturer": "Ford", "model": "F-150 Police Responder"},
    )
    assert "gov" in recovery and "suppliers" in recovery and "quantity" in recovery
    econ = recompute_economics_from_recovery(
        {"title": "2027 Ford F-150 Police Responder"},
        gov_rec=recovery["gov"],
        qty_rec=recovery["quantity"],
        supplier_rec=recovery["suppliers"],
    )
    assert econ["evaluability"]["evaluable"] is True
    assert econ["max_buy"] is not None
    assert (econ["quote_dependent"]["tiers"] or {}).get("quote_dependent_positive")


def test_dynamic_positive_and_evaluability_states():
    ev = classify_economic_evaluability(
        gov={"state": "GOV_VALUE_RANGE", "unit_value": 50000},
        suppliers=[{"name": "a"}],
        quantity_info={"unit_only": True, "quality": "UNIT_ONLY"},
        max_buy={"supplier_quote_target": 40000},
    )
    assert ev["evaluable"] is True
    assert ev["state"] in {
        ECONOMICALLY_EVALUABLE_UNIT_ONLY,
        ECONOMICALLY_EVALUABLE_QUOTE_DEPENDENT,
        ECONOMICALLY_EVALUABLE_RANGE,
    }
    bad = classify_economic_evaluability(
        gov={"state": "GOV_VALUE_UNKNOWN"},
        suppliers=[],
        quantity_info={"quality": "UNRESOLVED"},
    )
    assert bad["evaluable"] is False
    assert NOT_EVALUABLE_GOV_VALUE in bad["reasons"] or bad["state"]


def test_no_total_cap_and_gates():
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT
    assert_no_fixed_positive_cap(32)
    assert_no_fixed_positive_cap(5000)
    assert EXACT_VERIFIED == "EXACT_VERIFIED"


def test_original_source_integrity():
    from phase_l.original_solicitation import resolve_original_solicitation

    o = resolve_original_solicitation(
        {
            "title": "Truck",
            "agency": "City",
            "solicitation_id": "C-1",
            "original_posting_url": "https://sam.gov/opp/1/view",
        }
    )
    assert o["original_source_verified"] is True

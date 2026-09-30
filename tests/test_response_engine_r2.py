"""R2 unit tests — CLIN, UOM, technical, pricing, firewall, amendment propagation."""

from __future__ import annotations

from decimal import Decimal

from response_engine.acquisition_cost import build_acquisition_cost_breakdown, line_economics
from response_engine.clins import extract_line_items_from_project, new_line_item
from response_engine.financing import evaluate_execution_gates, financing_cost_of
from response_engine.firewall import firewall_report, ingest_supplier_quote_as_internal
from response_engine.logistics import classify_freight, evaluate_delivery, packaging_cost_state
from response_engine.models import new_response_project
from response_engine.pricing import (
    bid_price_for_target_margin,
    bid_price_for_target_profit,
    calculate_max_buy_decimal,
    compute_profit,
    max_acquisition_for_bid,
)
from response_engine.product_offer import evaluate_exact_match, new_offered_product
from response_engine.r2_constants import (
    PASS_VERIFIED,
    TECHNICAL_FAIL,
    UOM_CONVERSION_BLOCKED,
    VERIFIED,
)
from response_engine.r2_service import apply_quantity_amendment_to_r2, run_r2_analysis
from response_engine.store import save_project
from response_engine.supplier_evidence import (
    attach_supplier_quote,
    build_supplier_questions_for_gaps,
    sanitize_supplier_facing_payload,
)
from response_engine.technical_compliance import evaluate_line_technical_compliance
from response_engine.uom import convert_quantity


def test_uom_ea_passthrough():
    r = convert_quantity(buyer_qty=120, buyer_uom="EA")
    assert r["ok"] and r["normalized_quantity"] == "120" and r["normalized_uom"] == "EA"


def test_uom_case_with_pack():
    r = convert_quantity(buyer_qty=10, buyer_uom="CASE", pack_size=12)
    assert r["ok"] and r["normalized_quantity"] == "120"


def test_uom_case_unknown_pack_blocks():
    r = convert_quantity(buyer_qty=10, buyer_uom="CASE")
    assert not r["ok"] and r["block"] == UOM_CONVERSION_BLOCKED


def test_uom_dozen():
    r = convert_quantity(buyer_qty=2, buyer_uom="DOZEN")
    assert r["ok"] and r["normalized_quantity"] == "24"


def test_uom_ft_roll_unknown_blocks():
    r = convert_quantity(buyer_qty=100, buyer_uom="FT", supplier_uom="ROLL")
    assert not r["ok"] and r["block"] == UOM_CONVERSION_BLOCKED


def test_exact_part_match_and_mismatch():
    assert evaluate_exact_match(required_mpn="ABC-123", offered_mpn="ABC-123", product_mode="EXACT_PART_NUMBER") == "EXACT_MATCH"
    assert evaluate_exact_match(required_mpn="ABC-123", offered_mpn="ABC-124", product_mode="EXACT_PART_NUMBER") == "MISMATCH"


def test_technical_fail_on_part_mismatch():
    line = new_line_item(response_project_id="RP-x", clin="0001", required_mpn="ABC-123", product_mode="EXACT_PART_NUMBER")
    line["required_mpn"] = "ABC-123"
    line["product_mode"] = "EXACT_PART_NUMBER"
    offered = new_offered_product(response_project_id="RP-x", line_item_id=line["line_item_id"], mpn="ABC-124", product_mode="EXACT_PART_NUMBER")
    items = evaluate_line_technical_compliance(line=line, offered=offered, requirements=[])
    assert any(i["compliance_status"] == TECHNICAL_FAIL for i in items)


def test_generic_equal_rejected():
    line = new_line_item(response_project_id="RP-x", clin="0001", product_mode="BRAND_NAME_OR_EQUAL")
    offered = new_offered_product(
        response_project_id="RP-x",
        line_item_id=line["line_item_id"],
        description="meets all requirements",
        product_mode="BRAND_NAME_OR_EQUAL",
    )
    reqs = [{"requirement_id": "r1", "requirement_category": "BRAND_OR_EQUAL", "requirement_text": "Brand Name or Equal Widget"}]
    items = evaluate_line_technical_compliance(line=line, offered=offered, requirements=reqs)
    assert any(i["compliance_status"] == TECHNICAL_FAIL and "generic" in (i.get("notes") or "") for i in items)


def test_delivery_fail_and_pass():
    assert evaluate_delivery(buyer_days_aro=30, supplier_days_aro=45)["delivery_status"] == TECHNICAL_FAIL
    assert evaluate_delivery(buyer_days_aro=30, supplier_days_aro=20)["delivery_status"] == PASS_VERIFIED


def test_margin_markup_and_target_formulas():
    p = compute_profit(bid_revenue="100", total_execution_cost="80", evidence_quality="SCENARIO")
    assert p["expected_profit"] == "20.00"
    assert Decimal(p["margin_pct"]) == Decimal("20.00")
    assert Decimal(p["markup_pct"]) == Decimal("25.00")
    tp = bid_price_for_target_profit(total_execution_cost="80", target_profit="10000")
    assert tp["bid_price"] == "10080.00"
    tm = bid_price_for_target_margin(total_execution_cost="80", target_margin="0.20")
    assert tm["bid_price"] == "100.00"


def test_max_buy_decimal_matches_l6_algebra():
    # revenue 10000, freight 0, rate 5%, profit 0 → max = 10000/1.05
    mb = calculate_max_buy_decimal(revenue="10000", financing_rate="0.05", freight=0)
    assert mb["ok"]
    assert mb["thresholds"]["BREAK_EVEN_MAX_BUY"] == "9523.81"
    assert mb["namespace"] == "INTERNAL_ONLY"


def test_max_acquisition_internal_only():
    r = max_acquisition_for_bid(bid_price="100000", target_profit="10000", financing_rate="0.05")
    assert r["must_not_reveal_to_supplier"] is True
    assert r["ok"]


def test_financing_and_execution_fail():
    f = financing_cost_of(amount="1000", rate="0.05")
    assert f["financing_cost"] == "50.00"
    g = evaluate_execution_gates({"terms": "personal guarantee and prepayment required"})
    assert g["execution_status"] == "EXECUTION_FAIL"


def test_freight_unknown_blocks():
    fr = classify_freight(included=False)
    assert fr["blocks_verified_profit"] is True
    pack = packaging_cost_state(military_packaging_required=True)
    assert pack["blocks_verified_profit"] is True


def test_acquisition_breakdown_no_silent_zero():
    bd = build_acquisition_cost_breakdown(product_subtotal="100", product_evidence=VERIFIED)
    # freight unknown → not verified
    assert bd["verified_acquisition"] is False or bd["blocked_components"]


def test_line_economics_verified_when_complete():
    e = line_economics(
        quantity=10,
        unit_acquisition="50",
        unit_bid="80",
        freight_total="0",
        product_evidence=VERIFIED,
        freight_evidence=VERIFIED,
        packaging_evidence="NOT_APPLICABLE",
    )
    assert e["ok"]
    assert e["profit"]["expected_profit"] is not None


def test_firewall_quote_conditions_and_max_buy():
    p = new_response_project(canonical_opportunity_id="r2-fw-1")
    ingest_supplier_quote_as_internal(p, {"supplier": "A", "unit_price": 10, "terms": "pricing subject to change"})
    p.setdefault("evidence_namespaces", {}).setdefault("GOVERNMENT_SUBMISSION_CONTENT", []).append(
        {"payload": {"description": "widget"}}
    )
    report = firewall_report(p)
    assert report["clean"] is True
    leaked = sanitize_supplier_facing_payload({"max_buy": 72000, "unit_price": 10, "historical_government_price": 95000})
    assert "max_buy" not in leaked and "historical_government_price" not in leaked
    assert leaked.get("unit_price") == 10


def test_supplier_questions_no_internal_leak():
    qs = build_supplier_questions_for_gaps(["freight", "coo", "pack_size"])
    blob = " ".join(qs).lower()
    assert "max-buy" not in blob and "historical" not in blob and "margin" not in blob


def test_r2_analysis_and_amendment_qty_invalidates_quote(tmp_path, monkeypatch):
    import response_engine.store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "rp")
    monkeypatch.setattr(store, "INDEX_PATH", store.STORE_DIR / "index.json")
    store.ensure_store()
    p = new_response_project(canonical_opportunity_id="r2-amd-1", title="Widget RFQ")
    p["documents"] = [
        {
            "document_id": "D1",
            "controlling_status": "CONTROLLING",
            "text": "CLIN 0001 Qty 10 EA Brand Name Only part ABC-123. Acknowledge the amendment.",
            "tables": {"line_items": [{"clin": "0001", "quantity": 10, "uom": "EA", "description": "Widget", "sheet": "Pricing", "row": 12}]},
        }
    ]
    p["product_mode"] = "EXACT_PART_NUMBER"
    p["requirements"] = []
    save_project(p)
    run_r2_analysis(p)
    assert p.get("line_items")
    assert p["line_items"][0]["quantity"] == "10"
    lid = p["line_items"][0]["line_item_id"]
    attach_supplier_quote(p, {"supplier": "Acme", "unit_price": "40.00", "quote_type": "WRITTEN_QUOTE", "quantity": 10}, line_item_id=lid)
    run_r2_analysis(p)
    apply_quantity_amendment_to_r2(p, line_item_id=lid, new_qty="20")
    assert p["line_items"][0]["quantity"] == "20"
    q = p["supplier_quotes"][0]
    assert q.get("quantity_mismatch") is True
    assert q.get("supports_verified_acquisition") is False


def test_clin_extraction_from_table():
    p = new_response_project(canonical_opportunity_id="r2-clin-1")
    p["documents"] = [
        {
            "document_id": "D1",
            "controlling_status": "CONTROLLING",
            "text": "",
            "tables": {
                "line_items": [
                    {"clin": "0001", "quantity": 5, "uom": "EA", "description": "A", "sheet": "Cost", "row": 18, "unit_price_cell": "D18"},
                    {"clin": "0002", "quantity": 3, "uom": "EA", "description": "B", "sheet": "Cost", "row": 19},
                ]
            },
        }
    ]
    lines = extract_line_items_from_project(p)
    assert len(lines) == 2
    assert lines[0]["provenance"].get("sheet") == "Cost"
    assert lines[0]["template_map"]["unit_price_cell"] == "D18"

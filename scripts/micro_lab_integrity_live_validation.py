"""Controlled live/fixture validation for µLab integrity — not the 1k–2k experiment.

Demonstrates integrity gates with fixtures + any available local DIBBS/history shapes.
No fabricated COMPLETE. Network optional; reports access constraints when absent.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from micro_purchase_lab_integrity import (
    evaluate_integrity,
    extract_clins,
    normalize_commercial_to_base,
    compute_max_acquisition_cost,
    assess_actionability,
    assess_approved_source,
    classify_procurement_path,
)
from micro_purchase_lab_pipeline import evaluate_opportunity, STATE_COMPLETE
from micro_purchase_lab_source_router import route_plan_for_opportunity, NEED_DIBBS_RFQ, route_evidence


def _base(**kw):
    row = {
        "canonical_id": "live-val-1",
        "status": "OPEN",
        "deadline": (date.today() + timedelta(days=21)).isoformat(),
        "deadline_evaluation": {"calendar_days_remaining": 21},
        "source_id": "state_co",
        "agency": "Validation Agency",
        "estimated_value": 4500,
        "detail_url": "https://example.gov/sol/val",
    }
    row.update(kw)
    return row


def main() -> dict:
    results = []

    # 1) DIBBS-style product
    dibbs = _base(
        source_id="dla_dibbs",
        is_dla=True,
        solicitation_number="SPE7M1-26-T-9999",
        title="WASHER, FLAT",
        nsn="5310-00-999-8888",
        part_number="MS15795-808",
        manufacturer="Approved Mfr",
        approved_cages=["1A2B3"],
        approved_source_required=True,
        quantity=100,
        unit_of_issue="EA",
        description="CLIN 0001 Qty 100 EA. FOB DESTINATION. Inspection DESTINATION. Delivery 45 days ARO. MIL-STD-2073 packaging. PID required.",
        line_items=[{"clin": "0001", "quantity": 100, "uom": "EA", "destination": "Richmond", "fob": "DESTINATION", "inspection": "DESTINATION"}],
        government_price_history={
            "awards": [
                {"date": "2024-11-01", "unit_price": 1.25, "quantity": 100, "nsn": "5310-00-999-8888", "vendor": "Prior Inc"}
            ]
        },
        commercial_pricing={"lowest_public_new_unit": 0.85, "public_source": "Industrial Dist", "as_of": date.today().isoformat()},
    )
    out1 = evaluate_opportunity(dibbs)
    qty1 = extract_clins(dibbs)
    results.append(
        {
            "case": "1_dibbs_style_product",
            "research_state": out1["research_state"],
            "nsn_resolved": bool(out1.get("nsn")),
            "quantity_confidence": qty1.get("overall_quantity_confidence"),
            "history_ok": bool(out1.get("last_government_unit_price")),
            "packaging_flagged": "SPECIAL_PACKAGING_REQUIRED" in ((out1.get("execution") or {}).get("flags") or []),
            "path": (out1.get("procurement_path") or {}).get("path"),
            "complete": out1["research_state"] == STATE_COMPLETE,
            "blockers": out1.get("complete_blockers"),
        }
    )

    # 2) Pack/UOM ambiguity prevents COMPLETE
    pack_amb = _base(
        title="SafeHand SH-NIT-100 Nitrile Exam Gloves industrial supply",
        manufacturer="SafeHand",
        part_number="SH-NIT-100",
        quantity=1,
        unit_of_issue="CASE",
        estimated_value=2000,
        government_price_history={"awards": [{"date": "2025-01-01", "unit_price": 40, "quantity": 1, "part_number": "SH-NIT-100"}]},
        commercial_pricing={"lowest_public_new_unit": 28, "public_source": "Retail", "as_of": date.today().isoformat()},
    )
    out2 = evaluate_opportunity(pack_amb)
    results.append(
        {
            "case": "2_pack_uom_ambiguity",
            "research_state": out2["research_state"],
            "complete": out2["research_state"] == STATE_COMPLETE,
            "blockers": out2.get("complete_blockers"),
            "reason": out2.get("unresolved_reason"),
            "qty_confidence": (out2.get("quantity_integrity") or {}).get("overall_quantity_confidence"),
        }
    )

    # 3) Variant mismatch
    variant = _base(
        title="DEWALT DCD996B Hammer Drill cordless bare tool",
        manufacturer="DEWALT",
        part_number="DCD996B",
        compared_part_number="DCD996P2",
        quantity=5,
        unit_of_issue="EA",
        estimated_value=2000,
        government_price_history={"awards": [{"date": "2025-01-01", "unit_price": 300, "quantity": 5, "part_number": "DCD996B"}]},
        commercial_pricing={"lowest_public_new_unit": 220, "public_source": "Dist", "as_of": date.today().isoformat()},
    )
    out3 = evaluate_opportunity(variant)
    results.append(
        {
            "case": "3_variant_mismatch",
            "research_state": out3["research_state"],
            "complete": out3["research_state"] == STATE_COMPLETE,
            "identity_state": (out3.get("variant_identity") or {}).get("identity_state"),
            "blockers": out3.get("complete_blockers"),
            "reason": out3.get("unresolved_reason"),
        }
    )

    # 4) Aggregator without authoritative resolve
    agg = _base(
        source_id="bidnet_demo",
        source_family="AGGREGATOR",
        title="HON HON-2091 Task Chair office furniture product",
        manufacturer="HON",
        part_number="HON-2091",
        quantity=20,
        unit_of_issue="EA",
        estimated_value=3500,
        government_price_history={"awards": [{"date": "2025-01-01", "unit_price": 150, "quantity": 20, "part_number": "HON-2091"}]},
        commercial_pricing={"lowest_public_new_unit": 110, "public_source": "Dist", "as_of": date.today().isoformat()},
    )
    act4 = assess_actionability(agg)
    out4 = evaluate_opportunity(agg)
    results.append(
        {
            "case": "4_aggregator_not_actionable",
            "actionability": act4.get("state"),
            "complete": out4["research_state"] == STATE_COMPLETE,
            "blockers": out4.get("complete_blockers"),
        }
    )

    # 5) History total vs unit price
    hist_total = evaluate_integrity(
        _base(title="Widget", part_number="W-1", quantity=10),
        pipeline_partial={
            "historical_evidence": [
                {"unit_price": None, "unit_price_status": "TOTAL_ONLY_NOT_UNIT", "total_amount": 5000},
                {"unit_price": "12.00", "quantity": 10},
            ],
            "last_government_unit_price": "12.00",
            "current_public_price": "9.00",
        },
    )
    results.append(
        {
            "case": "5_history_total_vs_unit",
            "range_sample_count": (hist_total.get("historical_range") or {}).get("sample_count"),
            "uses_only_verified_unit": (hist_total.get("historical_range") or {}).get("sample_count") == 1,
            "recent_median": (hist_total.get("historical_range") or {}).get("recent_median"),
        }
    )

    # 6) Commercial pack normalization
    norm = normalize_commercial_to_base(unit_price=99.0, pack_quantity=11, seller_uom="CASE", government_base_uom="EA")
    results.append({"case": "6_commercial_uom_normalize", "ok": norm["ok"], "normalized_unit_price": norm.get("normalized_unit_price")})

    # 7) Max supplier cost
    mac = compute_max_acquisition_cost(historical_unit_price="312.40", quantity=10, required_gross_margin_pct=25, known_freight=50)
    results.append(
        {
            "case": "7_max_supplier_cost",
            "ok": mac["ok"],
            "max_landed_unit_cost": mac.get("max_landed_unit_cost"),
            "preliminary": mac.get("preliminary"),
        }
    )

    # 8) Source-controlled approved manufacturer
    approved = assess_approved_source(
        _base(description="Approved source CAGE 9Z8Y7 only", approved_cages=["9Z8Y7"], approved_source_required=True)
    )
    results.append({"case": "8_approved_source", "state": approved.get("state"), "cages": approved.get("approved_cages")})

    # 9) Procurement path / access
    path_open = classify_procurement_path(_base(source_id="fed_sam", notice_id="n1"))
    path_ebuy = classify_procurement_path(_base(description="Submit response on GSA eBuy"))
    results.append(
        {
            "case": "9_procurement_path",
            "sam_path": path_open.get("path"),
            "sam_direct": path_open.get("ok_for_complete_direct"),
            "ebuy_path": path_ebuy.get("path"),
            "ebuy_reason": path_ebuy.get("reason"),
        }
    )

    # Source routes (no warehouse)
    routes = route_evidence(NEED_DIBBS_RFQ)
    results.append(
        {
            "case": "source_router",
            "warehouse": routes.get("warehouse"),
            "strongest": routes.get("strongest_authority"),
            "route_count": len(routes.get("routes") or []),
        }
    )

    # Optional live store peek
    live_note = None
    try:
        from micro_purchase_lab_store import load_lab_state

        st = load_lab_state() or {}
        tests = st.get("tests") or []
        live_note = {"store_tests": len(tests), "note": "No mass experiment; store inspect only"}
    except Exception as e:
        live_note = {"store_access": "unavailable", "error": str(e)[:200]}

    return {"build": "20260922-m3-micro-lab-integrity-1", "cases": results, "live_store": live_note}


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))

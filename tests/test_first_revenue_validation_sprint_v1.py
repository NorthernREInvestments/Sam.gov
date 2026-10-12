"""Tests for First Revenue Validation Sprint V1."""

from __future__ import annotations

from decimal import Decimal

from buyer_intelligence.first_revenue_validation_sprint_v1 import (
    classify_economics,
    first_revenue_research_priority_score,
    hard_exclusion_reason,
    build_five_rfqs,
)


def test_hard_exclude_surplus_and_multiline():
    assert hard_exclusion_reason({"TITLE": "SURPLUS SALE OF SCRAP COPPER WIRE", "QUOTEABLE_LINES": 1}) 
    assert hard_exclusion_reason({"TITLE": "Electrical Parts", "REAL_LINES": 41, "QUOTEABLE_LINES": 41})
    assert hard_exclusion_reason({"TITLE": "Janitorial Supplies", "REAL_LINES": 1, "DEADLINE": "2000-01-01T00:00:00Z"})


def test_priority_prefers_simple_single_line():
    simple = first_revenue_research_priority_score(
        {
            "TITLE": "Mop Hardware",
            "BUYER": "City of Tampa",
            "REAL_LINES": 1,
            "QUOTEABLE_LINES": 1,
            "PACKET_COUNT": 1,
            "DEADLINE": "2099-12-01T00:00:00Z",
            "SUPPLIER_SET": {"W.W. Grainger, Inc.": 1},
        }
    )
    complex_ok = first_revenue_research_priority_score(
        {
            "TITLE": "Various Industrial Parts Basket",
            "BUYER": "Agency",
            "REAL_LINES": 8,
            "QUOTEABLE_LINES": 8,
            "PACKET_COUNT": 0,
            "DEADLINE": "2099-12-01T00:00:00Z",
        }
    )
    assert not simple["EXCLUDED"]
    assert simple["SCORE"] > complex_ok["SCORE"]


def test_economics_never_zeros_unknown_freight():
    econ = classify_economics(
        selling=Decimal("1000"),
        selling_label="COMPARABLE",
        acquisition=Decimal("400"),
        freight=None,
        financing_fees=None,
        financing_status="FINANCING_UNKNOWN",
        full_lines_priced=True,
    )
    assert econ["STATUS"] == "CONDITIONAL_POSITIVE_ECONOMICS"
    assert "FREIGHT" in econ["MISSING"]
    assert econ["GROSS_SPREAD_SCENARIO"] == "600.00"


def test_reject_1000bulbs_for_cable_assembly():
    from buyer_intelligence.first_revenue_validation_sprint_v1 import _reject_false_host_match

    bad = _reject_false_host_match(
        "CABLE ASSEMBLY,SPECIAL",
        {
            "unit_price": 14.02,
            "source_url": "https://www.1000bulbs.com/search?q=CABLE+ASSEMBLY",
            "evidence_level": "EQUIVALENT_PUBLIC_PRICE_VERIFIED",
            "status": "FOUND",
        },
    )
    assert bad["unit_price"] is None
    assert bad["status"] == "REJECTED_HOST_MISMATCH"


def test_exclude_bridge_bearing_repairs():
    assert hard_exclusion_reason(
        {"TITLE": "Bridge bearing replacements on SN 001-0047", "REAL_LINES": 1, "QUOTEABLE_LINES": 1}
    )


def test_rfq_auto_send_false_and_ceilings_hidden():
    results = [
        {
            "OPPORTUNITY_ID": "abc",
            "Product": "Gasket",
            "Buyer": "Ohio",
            "Deadline": "2099-01-01T00:00:00Z",
            "Exact_Product_Identity": True,
            "Mandatory_Lines": 1,
            "Exact_Public_Price_Lines": 1,
            "Equivalent_Public_Price_Lines": 0,
            "Past_Gov_Price": {"unit_price": "10.00"},
            "Suppliers": ["McMaster-Carr"],
            "Economics": {
                "STATUS": "CONDITIONAL_POSITIVE_ECONOMICS",
                "MISSING": ["FREIGHT"],
                "TARGETS": {"MAX_COST_FOR_500": "100.00"},
            },
            "Plausible_Contribution": {"P500": True},
            "Lines": [
                {
                    "LINE_NO": 1,
                    "DESCRIPTION": "Gasket",
                    "IDENTITY": {"qty": 10, "uom": "EA"},
                    "RETAIL": {"unit_price": 5.0, "source_url": "https://example.com", "evidence_level": "EXACT_PUBLIC_PRICE_VERIFIED"},
                }
            ],
        }
    ]
    rfqs = build_five_rfqs(results)
    assert rfqs["AUTO_SEND"] is False
    assert rfqs["COUNT"] == 1
    assert rfqs["PACKETS"][0]["RFQ"]["INTERNAL_ONLY"]["DO_NOT_DISCLOSE_TO_SUPPLIER"] is True

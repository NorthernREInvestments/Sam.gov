"""General bid-table extraction — multi-page numbered schedules, not commodity-specific."""
from __future__ import annotations

from m3.research.bid_schedule import parse_bid_schedule_tables
from m3.research.extract_fields import extract_quantity_fields, normalize_quantity_unit
from m3.research.procurement_requirements import extract_requirements

# Synthetic municipal pricing table (PDF-style cell-per-line), multi-section.
_SYNTH_TABLE = """
SECTION 5 - PRICE PROPOSAL / BID TABLE
Page 10
BASE BID
Line Item
Description
Quantity
Unit of
Measure
Unit Cost
Total
Vendor
Notes
1
Fixed Markup Over
Index - Bulk liquid
commodity delivered to
the City's existing
stationary storage tank.
40,000
Gallon



2
Cylinder
monitoring and service
coordination during
routine delivery visits.
1
Annual



TOTAL

MODIFICATIONS, LIMITATIONS, ADDITIONAL COSTS
Page 11
Line Item
Description
Quantity
Unit of
Measure
Unit Cost
Total
Vendor
Notes
3
Any required
equipment
modifications,
compatibility
limitations, or
additional costs
1
LS



TOTAL

AS NEEDED SERVICES
Line Item
Description
Quantity
Unit of
Measure
Unit Cost
Total
Vendor
Notes
4
Cylinder
replacement/exchange,
including supply,
delivery, handling, and
related costs.
1
Per
Replacement



5
Emergency/After-
Hours Delivery Fee
1
Per Delivery


TOTAL
"""


def test_numbered_bid_table_extracts_five_purchasing_lines():
    rows = parse_bid_schedule_tables(_SYNTH_TABLE)
    assert len(rows) == 5
    by_ref = {r["line_ref"]: r for r in rows}
    assert by_ref["1"]["quantity"] == 40000.0
    assert by_ref["1"]["unit_of_measure"] == "GAL"
    assert by_ref["1"]["quantity_semantic"] == "EVALUATION_QUANTITY"
    assert by_ref["1"]["line_kind"] == "product"
    assert "5000" not in str(by_ref["1"]["quantity"])  # tank capacity must not become qty
    assert by_ref["2"]["unit_of_measure"] == "ANNUAL"
    assert by_ref["2"]["line_kind"] == "service"
    assert by_ref["3"]["unit_of_measure"] == "LS"
    assert by_ref["4"]["unit_of_measure"] == "PER REPLACEMENT"
    assert by_ref["5"]["unit_of_measure"] == "PER DELIVERY"
    assert by_ref["5"]["line_kind"] == "ancillary_charge"


def test_extract_requirements_uses_bid_table_not_narrative_line_item_mention():
    # Narrative "Line Item 5 of the Pricing Table" must not become the only CLIN
    blob = (
        "Emergency deliveries shall be subject to the rate in Line Item 5 of the Pricing Table.\n"
        + _SYNTH_TABLE
    )
    req = extract_requirements("Bulk Commodity Supply", document_text=blob)
    assert req["line_item_count"] == 5
    assert req["complete_line_items"] == 5
    assert all(li.get("complete") for li in req["line_items"])
    assert {li["unit_of_measure"] for li in req["line_items"]} >= {
        "GAL", "ANNUAL", "LS", "PER REPLACEMENT", "PER DELIVERY",
    }


def test_extract_quantity_fields_multi_line_from_bid_table():
    q = extract_quantity_fields(_SYNTH_TABLE, title="Bulk Commodity Supply")
    assert q["quantity_semantic"] == "MULTI_LINE_ITEM"
    assert q["quantity_line_count"] == 5
    assert len(q["verified_line_items"]) == 5


def test_service_uom_normalization():
    assert normalize_quantity_unit("Annual") == "ANNUAL"
    assert normalize_quantity_unit("LS") == "LS"
    assert normalize_quantity_unit("Per Delivery") == "PER DELIVERY"
    assert normalize_quantity_unit("Per Replacement") == "PER REPLACEMENT"


def test_item_hash_schedule_still_parses():
    """BidBuy-style Item # rows remain supported (non-table path)."""
    text = "Item Information 1-5 of 62 Item # 1: SALT, ROCK Qty 100 TON Item # 2: SALT Qty 50 TON"
    req = extract_requirements("Rock Salt", description=text)
    assert req["schedule_advertised_total"] == 62
    assert req["line_item_count"] == 2

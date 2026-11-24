"""Line items, reseller eligibility, economics and execution feasibility â€” no fabricated values."""
from __future__ import annotations

from m3.research.procurement_requirements import (
    assess_economics,
    assess_execution_feasibility,
    assess_reseller_eligibility,
    extract_requirements,
)

DOC = """
CLIN 0001 Nitrile exam gloves, size large, 100 per box. Quantity: 500 BX. Manufacturer: Halyard, Model 55083. Brand name or equal.
CLIN 0002 Nitrile exam gloves, size medium. Quantity: 300 BX.
Delivery: FOB destination within 30 days ARO.
"""


def test_line_items_parsed_without_defaults():
    req = extract_requirements("Nitrile Gloves", document_text=DOC)
    items = req["line_items"]
    assert [i["clin"] for i in items][:2] == ["CLIN 0001", "CLIN 0002"]
    assert items[0]["quantity"] == 500
    assert items[0]["unit_of_measure"] == "BX"
    assert items[0]["manufacturer"] == "Halyard"
    assert items[0]["model"] == "55083"
    assert items[0]["brand_name_or_equal"] is True
    assert items[1]["manufacturer"] is None
    assert all(i["evidence"].get("quantity") for i in items)


def test_no_text_is_not_quote_ready():
    req = extract_requirements("Office Supplies")
    assert req["requirements_state"] == "NO_TEXT"
    assert req["quote_ready"] is False
    assert req["line_items"] == []


def test_amendment_replaces_line():
    req = extract_requirements("Gloves", document_text=DOC,
                               amendments=[{"id": "A0001", "text": "CLIN 0001 Nitrile exam gloves, size large. Quantity: 650 BX."}])
    assert next(i for i in req["line_items"] if i["clin"] == "CLIN 0001")["quantity"] == 650


def test_set_aside_blocks_and_title_only_never_eligible():
    blocked = assess_reseller_eligibility(title="Gloves", text="This acquisition is a 100% service-disabled veteran-owned small business set-aside.",
                                          source="free", evidence_completeness="DOCUMENT", product_presence="EXPLICIT_PRODUCT")
    assert blocked["eligibility_state"] == "BLOCKED"
    title_only = assess_reseller_eligibility(title="Gloves", text="", source="free", evidence_completeness="TITLE_ONLY",
                                             product_presence="EXPLICIT_PRODUCT")
    assert title_only["eligibility_state"] != "ELIGIBLE"


def test_economics_unproven_without_verified_inputs():
    req = extract_requirements("Gloves", document_text=DOC)
    e = assess_economics(line_items=req["line_items"])
    assert e["profit_class"] == "UNPROVEN"


def test_execution_blocks_bond_and_requires_document():
    assert assess_execution_feasibility(text="A bid bond of 5% is required.", source="free",
                                        evidence_completeness="DOCUMENT")["execution_state"] == "EXECUTION_BLOCKED"
    assert assess_execution_feasibility(text="", source="free", evidence_completeness="TITLE_ONLY")["execution_state"] == "EXECUTION_UNKNOWN"

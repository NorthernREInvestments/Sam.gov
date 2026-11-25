"""Line items, reseller eligibility, economics and execution feasibility â€” no fabricated values."""
from __future__ import annotations

from datetime import datetime, timezone

from m3.research.classify import assess_product_presence
from m3.research.procurement_requirements import (
    assess_economics,
    assess_execution_feasibility,
    assess_product_identity,
    assess_reseller_eligibility,
    assess_sourcing_readiness,
    extract_requirements,
    qualification,
)


def _qual(title, desc=""):
    p = assess_product_presence(title, description=desc)
    r = extract_requirements(title, description=desc)
    s = assess_sourcing_readiness(title=title, requirements=r, eligibility_state="ELIGIBILITY_UNKNOWN")
    return qualification(presence=p, requirements=r, sourcing=s)


def test_qualification_states_are_separate_and_never_overclaim():
    q = _qual("Unication G5 Dual Band P25 Voice Pagers",
              "The County seeks to purchase fifty-three (53) Unication G5 pagers. Quantity: 53 EA.")
    assert q["intake_decision"] == "PROCEED"
    assert q["qualification_state"] == "SUPPLIER_RESEARCH_READY"
    assert not q["conditions"]["SUBMISSION_VERIFIED"] and not q["conditions"]["READY_TO_SUBMIT"]
    r = extract_requirements("Unication G5 Pagers", description="Proposals must be submitted electronically via the OpenGov portal.")
    assert "OpenGov portal" in r["solicitation_requirements"]["submission"][0]
    no_qty = _qual("Bid 2026 Road Salt")
    assert no_qty["intake_decision"] in {"PROCEED", "INVESTIGATE"}
    assert no_qty["qualification_state"] in {"DISCOVERED", "PRODUCT_CANDIDATE"}
    skip = _qual("Janitorial Services for City Hall", "Contractor shall provide custodial services.")
    assert skip["intake_decision"] == "SKIP" and skip["intake_reason"].startswith("principal_deliverable:")

def test_sourcing_readiness_separates_identity_quantity_and_access():
    pagers = extract_requirements("Unication G5 Dual Band P25 Voice Pagers",
                                  description="The County seeks to purchase fifty-three (53) Unication G5 pagers. Quantity: 53 EA.")
    assert assess_product_identity("Unication G5 Dual Band P25 Voice Pagers", pagers)["identity_level"] == "EXACT"
    ready = assess_sourcing_readiness(title="Unication G5 Dual Band P25 Voice Pagers", requirements=pagers,
                                      eligibility_state="EASY_REGISTRATION_REQUIRED")
    assert ready["sourcing_state"] == "SUPPLIER_RESEARCH_READY"
    unknown = assess_sourcing_readiness(title="Unication G5 Dual Band P25 Voice Pagers", requirements=pagers,
                                        eligibility_state="ELIGIBILITY_UNKNOWN")
    assert unknown["sourcing_state"] == "SUPPLIER_RESEARCH_READY"
    assert "eligibility:eligibility_unknown" in unknown["submission_gaps"]
    blocked = assess_sourcing_readiness(title="Unication G5 Dual Band P25 Voice Pagers", requirements=pagers,
                                        eligibility_state="BLOCKED")
    assert blocked["sourcing_state"] == "COMMERCIAL_RESEARCH_READY"
    van = extract_requirements("Fire Supply Delivery Van", description="Purchase of one (1) delivery van. Quantity: 1 EA.")
    assert assess_product_identity("Fire Supply Delivery Van", van)["identity_level"] == "GENERIC"
    assert assess_sourcing_readiness(title="Fire Supply Delivery Van", requirements=van,
                                     eligibility_state="ELIGIBLE")["sourcing_state"] == "DOCUMENT_REQUIRED"
    for t in ("Rebuilt Caterpillar 836G landfill compactor", "2026/2027 Ford F-350 Supply Pickup Truck",
              "RFB-IS-27200339 - COMPUTER - STORAGE - NUTANIX NX-3460S-G10", "RFP 2026-09 Polaris Ranger 1000"):
        assert assess_product_identity(t, {"line_items": []})["identity_level"] == "EXACT", t
    for t in ("Cabling Services for E-Rate Funding Year 27-28", "Bid 2026 Road Salt", "RFP 2026-09 Snow Plows",
              "Purchase Agreement for Creative Production Equipment and Supplies FE-26-04-RFB", "iPad Accessories RO2809",
              "Trade Package - Cargo Building 720.1", "Ford Street Paving 2026", "School Health Supplies GD27-0364-44"):
        assert assess_product_identity(t, {"line_items": []})["identity_level"] == "GENERIC", t


def test_asset_disposal_is_not_a_purchase():
    for t in ("Town of Thomaston - Accepting Bids for 2007 Sterling LT7500 Dual Axel Dump Truck", "Surplus Vehicle Auction",
              "Sale of Used Fire Engine"):
        assert assess_product_presence(t)["notice_type"] == "ASSET_SALE", t
    for t in ("2026 or NEWER FORD F-150 LARIAT SUPERCREW RWD TRUCK", "Frame Extension of Five (5) 2025 Freightliner 114SD Trucks",
              "Purchase of 2027 Transit Bus"):
        assert assess_product_presence(t)["notice_type"] != "ASSET_SALE", t


def test_portal_forecast_page_is_forecast():
    a = assess_product_presence(
        "Synthetic 0W Motor Oil Bid - Supplement",
        document_text="PA - eMarketplace Upcoming Procurements Upcoming Procurement Data General Information Solicitation "
                      "Title: Synthetic 0W Motor Oil Bid Anticipated Method of Procurement: IFB")
    assert a["notice_type"] == "FORECAST"


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
    fed = assess_reseller_eligibility(
        title="SCREW, CAP, HEXAGON HEAD", source="dibbs", evidence_completeness="DOCUMENT",
        text="First Article Testing is required. Technical data is export controlled; JCP certification (DD 2345) required. "
             "Inspection and acceptance at origin.")
    assert fed["eligibility_state"] == "ELIGIBILITY_UNKNOWN"
    assert {"sam_cage_registration_unverified", "first_article_testing", "technical_data_restricted",
            "government_source_inspection"} <= set(fed["eligibility_reasons"])
    missed = assess_reseller_eligibility(
        title="Multi-Function Devices", source="nextbid", evidence_completeness="DOCUMENT",
        text="A Pre-BID Meeting will be held on TUESDAY, NOVEMBER 10TH, 2026 AT 10:00AM. Pre- BID Meeting attendance is "
             "mandatory for Vendors. MANDATORY PRE-BID MEETING ON NOVEMBER 10TH, 2026.",
        now=datetime(2026, 11, 24, tzinfo=timezone.utc))
    assert missed["eligibility_state"] == "BLOCKED"
    upcoming = assess_reseller_eligibility(
        title="Gloves", source="nextbid", evidence_completeness="DOCUMENT",
        text="Mandatory pre-bid meeting on December 3, 2026.", now=datetime(2026, 11, 24, tzinfo=timezone.utc))
    assert upcoming["eligibility_state"] == "ELIGIBILITY_UNKNOWN"
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

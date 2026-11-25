"""Recall-first product presence: established failure fixtures and owner-definition negatives."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from m3.research.classify import DISCOVERY_RETAINED_PRESENCE, assess_product_presence, classify_requirement

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "product_presence_regression.json").read_text(encoding="utf-8"))
_DIBBS = {"NB00380027", "NB00365548"}


def _assess(fx: dict) -> dict:
    return assess_product_presence(
        fx["title"], description=fx["summary"], document_text=fx["doc_head"],
        procurement_kind=fx["procurement_kind"], source="dibbs" if fx["nb_id"] in _DIBBS else None,
    )


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["nb_id"] for f in FIXTURES])
def test_established_failures_are_retained(fx):
    a = _assess(fx)
    assert a["discovery_retained"], (fx["title"], a["product_presence"])
    assert a["product_presence"] in DISCOVERY_RETAINED_PRESENCE


def test_established_failures_detected_rate():
    detected = [f["nb_id"] for f in FIXTURES if _assess(f)["product_detected"]]
    # NB00375184 (sole-source exemption notice for blowers) is POSSIBLE by design
    assert len(detected) >= 15


def test_materials_only_overrides_work_title():
    fx = next(f for f in FIXTURES if f["nb_id"] == "NB00311713")
    a = _assess(fx)
    assert a["product_presence"] == "EXPLICIT_PRODUCT"
    assert classify_requirement(fx["title"], description=fx["summary"])["requirement_class"] == "product_supply"


def test_associated_services_boilerplate_is_not_service_evidence():
    fx = next(f for f in FIXTURES if f["nb_id"] == "NB00347094")
    assert _assess(fx)["product_detected"]


@pytest.mark.parametrize("title", [
    "BUS SHELTER CLEANING AND MAINTENANCE SERVICES",
    "Turblex Blower Maintenance and Repair Services",
    "Actuarial Valuation for GASB OPEB and Additional Services",
    "Annual Audit Services",
    "Janitorial Services for City Hall",
    "Towing Services",
    "Legal Services - Public Defender",
    "Tree Trimming",
    "Election Day Poll Workers",
    "Water Main Replacement Phase 3",
])
def test_services_and_construction_have_no_product_evidence(title):
    a = assess_product_presence(title)
    assert a["product_presence"] == "NO_PRODUCT_EVIDENCE", (title, a["presence_signals"])


@pytest.mark.parametrize("title", [
    "PARKING LOT PATCHING, SEALCOATING, AND RESTRIPING AT MULTIPLE EDGECOMBE COUNTY PARKING LOTS",
    "ARH is requesting bids for a network agreement made up of all ARH facilities.  Winning vendor will supply "
    "24x7x365 support with remote monitoring.",
])
def test_blind_validation_false_positives_not_detected(title):
    assert not assess_product_presence(title)["product_detected"]


def test_call_for_artists_is_a_commission_not_a_product():
    a = assess_product_presence(
        "Call for Artists For the Love of Gilroy Public Art Banners 2026-27",
        description="calling for artists to submit two-dimensional art designs for streetlight banners")
    assert a["product_presence"] not in {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT"}, a["presence_signals"]
    assert assess_product_presence("Art Supplies for Elementary Schools")["product_detected"]


@pytest.mark.parametrize("title", ["Cleaning Supplies", "Purchase of Shelving and Lockers", "SCREW, CAP, HEXAGON HEAD"])
def test_participle_modifiers_and_nomenclature_still_detected(title):
    assert assess_product_presence(title)["product_detected"], title


def test_site_specific_equipment_title_stays_possible_not_detected():
    a = assess_product_presence("Brown Road and 40th Street Traffic Signal")
    assert a["product_presence"] == "POSSIBLE_PRODUCT"
    assert not a["product_detected"]


@pytest.mark.parametrize("title", ["Snow Blower Rental", "Copier Lease"])
def test_rentals_are_categorized_separately(title):
    a = assess_product_presence(title)
    assert a["procurement_category"] == "rental"
    assert a["rental"] in {"RENTAL", "LEASE_OR_PURCHASE"}


@pytest.mark.parametrize("title,desc", [
    ("Fertilizer and Grass Seed", "Supply and delivery of fertilizer and grass seed to the Parks Department."),
    ("Rock Salt", "Furnish and deliver bulk rock salt for winter road maintenance, estimated 2,000 tons."),
    ("Boom Lift Purchase", "The County is requesting bids for the purchase of one (1) 60 ft articulating boom lift."),
    ("Traffic Signal Cabinets", "Purchase of 12 NEMA TS2 signal cabinets delivered to the City yard."),
])
def test_owner_listed_product_purchases_are_detected(title, desc):
    a = assess_product_presence(title, description=desc)
    assert a["product_detected"], (title, a["product_presence"], a["presence_signals"])


@pytest.mark.parametrize("title,desc", [
    ("Professional Auctioneering Services", "The City seeks a licensed auctioneer to conduct online auctions of surplus property."),
    ("Shuttle Bus Operating Services", "Contractor shall provide drivers and operate the downtown shuttle bus service."),
    ("Concrete Barrier Installation", "The Work consists of furnishing and installing 1,200 LF of concrete barrier along Route 9."),
    ("Engineering and Traffic Surveys", "Provide engineering and traffic count surveys at 40 intersections."),
    ("Qualified Contractors List", "Annual prequalification of contractors for on-call paving work."),
    ("Key and Locksmith Counter Services", "Contractor to provide all material and labor required as described in the Scope of Work."),
])
def test_owner_listed_work_and_lists_are_not_detected(title, desc):
    a = assess_product_presence(title, description=desc)
    assert not a["product_detected"], (title, a["product_presence"], a["presence_signals"])
    assert a["discovery_retained"] or a["product_presence"] == "NO_PRODUCT_EVIDENCE"


def test_measured_work_quantity_is_not_goods_quantity():
    a = assess_product_presence("East Sanitary Sewer Replacement",
                                description="The Work consists of installing approximately 4,400 LF of new 8 inch sanitary sewers.")
    assert not a["product_detected"] and a["discovery_retained"]
    assert not any(s["rule"].startswith("quantity_of_goods") for s in a["presence_signals"])


def test_work_dominant_cap_keeps_record_retained_and_traceable():
    a = assess_product_presence("Regional Water Supply 1.5 MG Elevated Water Tank",
                                description="The County is soliciting the construction of a new elevated water tank.")
    assert a["product_presence"] == "POSSIBLE_PRODUCT" and a["presence_capped_by"] == "work_dominant_without_item_evidence"


@pytest.mark.parametrize("title,desc", [
    ("Highway Vehicle (2005 Sterling)", "Town is accepting bids for a highway vehicle (2005 Sterling)."),
    ("Request for Product Demos: Firefighting Hose", "The purpose is to obtain information and schedule product demonstrations."),
])
def test_non_buying_notices_capped(title, desc):
    a = assess_product_presence(title, description=desc)
    assert a["presence_capped_by"] == "non_buying_notice" and not a["product_detected"]


def test_unrecognised_names_are_not_likely_goods():
    for t in ("Owner’s Representative", "DAS Procurement-Related Events", "Oneida Health Center Family Practice"):
        assert assess_product_presence(t)["product_presence"] not in {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT"}, t
    for t in ("TRAPROCK", "Main Line Guardrail IFB", "Traffic Signs"):
        assert assess_product_presence(t)["discovery_retained"], t


def test_independent_fields_present():
    a = assess_product_presence("Uniforms")
    for k in ("product_presence", "procurement_category", "product_identity_confidence", "service_presence",
              "installation_requirement", "construction_requirement", "rental", "software", "prequalification",
              "notice_type", "evidence_completeness", "presence_signals"):
        assert k in a
    assert a["evidence_completeness"] == "TITLE_ONLY"

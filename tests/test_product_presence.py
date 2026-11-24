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


def test_site_specific_equipment_title_stays_possible_not_detected():
    a = assess_product_presence("Brown Road and 40th Street Traffic Signal")
    assert a["product_presence"] == "POSSIBLE_PRODUCT"
    assert not a["product_detected"]


@pytest.mark.parametrize("title", ["Snow Blower Rental", "Copier Lease"])
def test_rentals_are_categorized_separately(title):
    a = assess_product_presence(title)
    assert a["procurement_category"] == "rental"
    assert a["rental"] in {"RENTAL", "LEASE_OR_PURCHASE"}


def test_independent_fields_present():
    a = assess_product_presence("Uniforms")
    for k in ("product_presence", "procurement_category", "product_identity_confidence", "service_presence",
              "installation_requirement", "construction_requirement", "rental", "software", "prequalification",
              "notice_type", "evidence_completeness", "presence_signals"):
        assert k in a
    assert a["evidence_completeness"] == "TITLE_ONLY"

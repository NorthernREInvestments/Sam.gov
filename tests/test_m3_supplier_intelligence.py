"""Supplier intelligence — matching, pricing evidence, confidence, margin protection, queue."""

from __future__ import annotations

from m3_commercial_engine import (
    PRICE_LEVEL_1_ACTUAL,
    PRICE_LEVEL_2_PUBLIC,
    PRICE_LEVEL_4_UNKNOWN,
    SCORE_HIGH,
    SCORE_MEDIUM,
)
from m3_pipeline_store import M3PipelineStore
from m3_supplier_intelligence import (
    MARGIN_PENDING,
    PRODUCT_IDENTITY_INCOMPLETE,
    analyze_supplier_top_opportunities,
    build_product_identity,
    build_supplier_intelligence,
    build_supplier_research_queue,
    compute_margin_status,
    deal_room_supplier_section,
    map_supply_chain,
    score_acquisition_cost_confidence,
)


def _opp(**kwargs):
    base = {
        "canonical_id": kwargs.get("canonical_id", "sup-1"),
        "title": "Cisco Systems Network Switches",
        "description": "Supply of Cisco Catalyst switches, delivery only.",
        "solicitation_number": "RFQ-SUP-1",
        "agency": "Department of Veterans Affairs",
        "status": "OPEN",
        "product_classification": "CORE_PRODUCT",
        "product_category": "IT_NETWORKING",
        "lifecycle": "RESEARCH_QUEUED",
        "research_queued": True,
        "deadline": "2099-12-01",
        "commercial_opportunity_score": SCORE_MEDIUM,
    }
    base.update(kwargs)
    return base


def test_product_identity_cisco_family_sufficient():
    product = build_product_identity(_opp())
    assert product["Manufacturer"] == "Cisco"
    assert product["sufficient_for_pricing_research"] is True
    assert product["identity_status"] == "COMPLETE"


def test_product_identity_incomplete_generic_service():
    product = build_product_identity(
        _opp(
            title="Janitorial Services Building A",
            description="Weekly cleaning services",
            product_category="LIKELY_SERVICE",
        )
    )
    assert product["identity_status"] == PRODUCT_IDENTITY_INCOMPLETE
    assert product["sufficient_for_pricing_research"] is False


def test_supplier_matching_maps_cisco_distributors():
    row = _opp()
    product = build_product_identity(row)
    supply = map_supply_chain(row, product)
    names = {c["company"] for c in supply["all_channels"]}
    assert "Cisco" in names
    assert "CDW" in names or "SHI" in names
    assert supply["supplier_confidence"] in {SCORE_HIGH, SCORE_MEDIUM}


def test_pricing_evidence_levels_and_confidence():
    row = _opp(
        commercial_pricing={
            "verified_wholesale_unit": 420.0,
            "verified_source": "https://example-distributor.test/cisco/item",
            "as_of": "2026-09-17",
        }
    )
    si = build_supplier_intelligence(row, allow_paid_web=False)
    assert si["Pricing_evidence"]["primary_level"] == PRICE_LEVEL_1_ACTUAL
    assert si["ACQUISITION_COST_CONFIDENCE"] == SCORE_HIGH
    assert si["cost_detail"]["best_price"] == 420.0


def test_confidence_unknown_without_price():
    row = _opp()
    product = build_product_identity(row)
    supply = map_supply_chain(row, product)
    conf = score_acquisition_cost_confidence(product=product, supply=supply, evidence=[])
    assert conf["ACQUISITION_COST_CONFIDENCE"] == "UNKNOWN"
    assert conf["best_level"] == PRICE_LEVEL_4_UNKNOWN


def test_margin_protection_without_cost():
    row = _opp(government_revenue=100000, estimated_value=100000)
    margin = compute_margin_status(
        row,
        cost_conf={
            "best_price": None,
            "best_level": PRICE_LEVEL_4_UNKNOWN,
            "ACQUISITION_COST_CONFIDENCE": "UNKNOWN",
        },
    )
    assert margin["margin_status"] == MARGIN_PENDING
    assert margin["estimated_gross_margin"] is None


def test_margin_calculates_only_with_level_1_or_2():
    row = _opp(government_revenue=10000, estimated_value=10000)
    margin = compute_margin_status(
        row,
        cost_conf={
            "best_price": 7000,
            "best_level": PRICE_LEVEL_2_PUBLIC,
            "ACQUISITION_COST_CONFIDENCE": SCORE_MEDIUM,
        },
    )
    assert margin["margin_status"] == "PRELIMINARY_GROSS_MARGIN"
    assert margin["estimated_gross_margin"] == 3000.0


def test_queue_ranks_cisco_dell_over_services():
    rows = [
        _opp(canonical_id="svc", title="Window Washing Services", product_category="LIKELY_SERVICE"),
        _opp(canonical_id="cisco", title="Cisco Systems Network Switches"),
        _opp(canonical_id="dell", title="Dell Storage Arrays", product_category="IT_STORAGE"),
        _opp(
            canonical_id="dla",
            title="31--BUSHING,SLEEVE",
            agency="DEFENSE LOGISTICS AGENCY",
            product_category="PARTS",
        ),
    ]
    q = build_supplier_research_queue(rows, limit=10)
    ids = [i["canonical_id"] for i in q["queue"]]
    assert ids[0] in {"cisco", "dell", "dla"}
    assert ids.index("cisco") < ids.index("svc")
    assert ids.index("dell") < ids.index("svc")


def test_analyze_persists_and_deal_room_shape(tmp_path):
    store = M3PipelineStore(path=tmp_path / "pipe.json", durable=False)
    store._rows["cisco-1"] = _opp(canonical_id="cisco-1")
    store._rows["svc-1"] = _opp(
        canonical_id="svc-1",
        title="Construction Restoration Project",
        product_category="LIKELY_SERVICE",
    )
    store.save()

    out = analyze_supplier_top_opportunities(store, limit=2, allow_paid_web=False)
    assert out["analyzed"] >= 1
    assert out["DEVELOPMENT_NO_OUTREACH"] is True
    assert out["commercial_outreach"] is False
    assert "LEVEL_4" in out["pricing_evidence_levels"]

    row = store.get("cisco-1")
    assert row and row.get("supplier_intelligence")
    sec = deal_room_supplier_section(row)
    assert sec["kind"] == "M3DealRoomSupplierIntelligence"
    assert "Cost_Confidence" in sec
    assert sec["Margin_Status"] in {MARGIN_PENDING, "PRELIMINARY_GROSS_MARGIN"}
    assert sec["Next_Action"]

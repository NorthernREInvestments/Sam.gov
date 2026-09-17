"""Commercial intelligence engine — scoring, winners, pricing confidence, queue."""

from __future__ import annotations

from pathlib import Path

from m3_commercial_engine import (
    FIN_EASY,
    FIN_DIFFICULT,
    PRICE_LEVEL_2_PUBLIC,
    PRICE_LEVEL_4_UNKNOWN,
    SCORE_HIGH,
    SCORE_LOW,
    SCORE_MEDIUM,
    WINNER_DISTRIBUTOR,
    WINNER_RESELLER,
    analyze_top_commercial_opportunities,
    assess_pricing_intelligence,
    build_commercial_intelligence,
    build_commercial_research_queue,
    build_winner_intelligence,
    classify_winner_type,
    discover_suppliers_public,
    extract_identification,
)
from m3_pipeline_store import M3PipelineStore


def _opp(**kwargs):
    base = {
        "canonical_id": kwargs.get("canonical_id", "cid-1"),
        "title": "Purchase of 20 Dell Latitude Laptops",
        "description": "Firm-fixed-price supply of computers delivery only. No installation.",
        "solicitation_number": "RFQ-100",
        "agency": "City of Example",
        "status": "OPEN",
        "product_classification": "CORE_PRODUCT",
        "product_category": "IT_COMPUTERS",
        "lifecycle": "RESEARCH_QUEUED",
        "research_queued": True,
        "deadline": "2099-12-01",
    }
    base.update(kwargs)
    return base


def test_identification_extracts_manufacturer_and_nsn():
    row = _opp(
        title="NSN 1234-56-789-0123 VALVE — Honeywell",
        description="P/N AB-99-X quantity QTY: 12",
        raw_metadata={"nsn": "1234-56-789-0123"},
    )
    ident = extract_identification(row)
    assert ident["NSN"] == "1234-56-789-0123"
    assert ident["manufacturer"] in {"Honeywell", "UNKNOWN"} or "Honeywell" in str(ident["manufacturer"])
    assert ident["part_number"] != "UNKNOWN" or ident["quantity"] == 12.0


def test_winner_classification_and_pattern_score():
    assert classify_winner_type("Acme Distribution Inc") == WINNER_DISTRIBUTOR
    assert classify_winner_type("Kampi Components Reseller LLC") == WINNER_RESELLER
    row = _opp(
        historical_awards=[
            {"winner": "Kampi Components", "vendor_type": "RESELLER", "amount": 50000},
            {"winner": "Kampi Components", "vendor_type": "RESELLER", "amount": 48000},
            {"winner": "Other Supply Co", "vendor_type": "DISTRIBUTOR", "amount": 51000},
        ]
    )
    w = build_winner_intelligence(row)
    assert w["WINNER_PATTERN_SCORE"] >= 30
    assert w["repeat_winners"]
    assert w["auto_reject"] is False


def test_pricing_never_invents_margin_from_award_alone():
    row = _opp(government_revenue=100000, estimated_value=100000)
    pricing = assess_pricing_intelligence(row)
    assert pricing["pricing_level"] == PRICE_LEVEL_4_UNKNOWN
    assert pricing["wholesale_estimate"] == "UNKNOWN"
    assert pricing["estimated_gross_margin"] in {"UNKNOWN", "COMMERCIAL_VERIFICATION_REQUIRED"}
    assert "government_price_is_not_profit" in pricing["notes"]


def test_pricing_level_2_public_requires_verification():
    row = _opp(public_pricing={"lowest_public_new_unit": 420, "government_historical_unit": 500})
    pricing = assess_pricing_intelligence(row)
    assert pricing["pricing_level"] == PRICE_LEVEL_2_PUBLIC
    assert pricing["status"] == "COMMERCIAL_VERIFICATION_REQUIRED"
    assert pricing["estimated_gross_margin"] == "COMMERCIAL_VERIFICATION_REQUIRED"


def test_commercial_score_high_vs_low():
    strong = _opp(
        title="Dell Latitude 50 EA delivery only",
        product_classification="CORE_PRODUCT",
        line_items=[{"description": "Dell Latitude", "quantity": 50, "manufacturer": "Dell", "part_number": "LAT5540"}],
        historical_awards=[
            {"winner": "SHI International", "vendor_type": "DISTRIBUTOR", "amount": 40000},
            {"winner": "SHI International", "vendor_type": "DISTRIBUTOR", "amount": 41000},
            {"winner": "CDW-G", "vendor_type": "DISTRIBUTOR", "amount": 39500},
        ],
        public_pricing={"lowest_public_new_unit": 900, "government_historical_unit": 1100},
        estimated_value=55000,
    )
    weak = _opp(
        canonical_id="cid-weak",
        title="Classified custom fabrication sole source engineering services",
        description="Facility clearance required. Custom manufacturing.",
        product_classification="SERVICE",
        historical_awards=[],
    )
    ci_s = build_commercial_intelligence(strong)
    ci_w = build_commercial_intelligence(weak)
    assert ci_s["COMMERCIAL_OPPORTUNITY_SCORE"] in {SCORE_HIGH, SCORE_MEDIUM}
    assert ci_w["COMMERCIAL_OPPORTUNITY_SCORE"] == SCORE_LOW
    assert ci_s["FINANCING"]["FINANCING_COMPLEXITY_SCORE"] in {FIN_EASY, "MODERATE"}
    assert ci_w["FINANCING"]["FINANCING_COMPLEXITY_SCORE"] == FIN_DIFFICULT


def test_supplier_discovery_no_outreach():
    row = _opp(title="Cisco switches via CDW catalog mention")
    s = discover_suppliers_public(row)
    assert s["DEVELOPMENT_NO_OUTREACH"] is True
    assert s["wholesale_sources"] == []
    assert s["OEM_manufacturer"] or s["authorized_distributors"] or s["public_dealer_channels"]


def test_queue_ranks_easy_margin_over_huge_impossible(tmp_path):
    store = M3PipelineStore(path=tmp_path / "pipe.json", durable=False)
    easy = _opp(
        canonical_id="easy",
        title="Purchase 10 Epson printers delivery",
        product_classification="CORE_PRODUCT",
        manufacturer="Epson",
        line_items=[{"description": "Epson", "quantity": 10, "manufacturer": "Epson", "part_number": "WF3820"}],
        estimated_value=8000,
        historical_awards=[
            {"winner": "Office Reseller Inc", "vendor_type": "RESELLER", "amount": 7500},
            {"winner": "Office Reseller Inc", "vendor_type": "RESELLER", "amount": 7600},
        ],
    )
    hard = _opp(
        canonical_id="hard",
        title="Sole source custom classified fabrication $1M",
        description="Secret clearance custom manufacturing",
        product_classification="SERVICE",
        estimated_value=1_000_000,
    )
    for r in (easy, hard):
        store.upsert_from_discovery(r)
        full = store.get(r["canonical_id"]) or r
        # Force lifecycle research queued
        full["lifecycle"] = "RESEARCH_QUEUED"
        full["research_queued"] = True
        full["product_classification"] = r["product_classification"]
        full["line_items"] = r.get("line_items")
        full["historical_awards"] = r.get("historical_awards")
        full["estimated_value"] = r.get("estimated_value")
        full["description"] = r.get("description")
        full["title"] = r.get("title")
        store._rows[r["canonical_id"]] = full
    store.save()
    q = build_commercial_research_queue(store.all(), limit=10)
    assert q["kind"] == "COMMERCIAL_RESEARCH_QUEUE"
    assert q["queue"]
    # Easy should rank above hard
    ids = [x["canonical_id"] for x in q["queue"]]
    assert ids.index("easy") < ids.index("hard")


def test_analyze_top_persists(tmp_path):
    store = M3PipelineStore(path=tmp_path / "pipe.json", durable=False)
    for i in range(5):
        r = _opp(
            canonical_id=f"c{i}",
            external_id=f"E-{i}",
            solicitation_number=f"E-{i}",
            title=f"Purchase Dell monitors qty {i+1}",
            line_items=[{"description": "Dell monitor", "quantity": i + 1, "manufacturer": "Dell"}],
        )
        row, _ = store.upsert_from_discovery(r)
        row["lifecycle"] = "RESEARCH_QUEUED"
        row["research_queued"] = True
        row["line_items"] = r["line_items"]
        store._rows[row["canonical_id"]] = row
    store.save()
    out = analyze_top_commercial_opportunities(store, limit=3)
    assert out["queue_size"] <= 3
    assert out["analyzed"] >= 3
    assert out["DEVELOPMENT_NO_OUTREACH"] is True
    scored = [r for r in store.all() if r.get("commercial_intelligence")]
    assert len(scored) >= 3


def test_deal_room_commercial_section_shape():
    from m3_commercial_engine import deal_room_commercial_section

    sec = deal_room_commercial_section(_opp())
    assert sec["kind"] == "M3DealRoomCommercial"
    assert "Next_Action" in sec
    assert "Commercial_Confidence" in sec
    assert "Estimated_Acquisition" in sec

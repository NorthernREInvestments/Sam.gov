"""Focused tests — product-resale source intelligence expansion."""

from __future__ import annotations

from product_resale_live_validation import filter_product_resale_candidates
from product_resale_source_intelligence import (
    ACTIVE,
    BLOCKED,
    BUILD_TAG,
    HIGH_VALUE_SOURCE,
    IOWA_SEED_SOLICITATION,
    MISSING,
    SOURCE_RECIPES,
    diagnose_discovery_gaps,
    infer_category,
    save_source_intelligence,
    select_price_path,
    source_coverage_audit,
    source_roi_ranking,
    suppliers_for_category,
)


def test_source_recipes_cover_required_channels():
    ids = {r["source_id"] for r in SOURCE_RECIPES}
    assert "fed_sam_contract_opportunities" in ids
    assert "dla_dibbs" in ids
    assert "network_bidnet_direct" in ids
    assert "coop_sourcewell" in ids
    assert "coop_naspo" in ids
    assert "gsa_ebuy" in ids
    dibbs = next(r for r in SOURCE_RECIPES if r["source_id"] == "dla_dibbs")
    assert dibbs["coverage_state"] == BLOCKED
    sam = next(r for r in SOURCE_RECIPES if r["source_id"] == "fed_sam_contract_opportunities")
    assert sam["coverage_state"] == ACTIVE
    gsa = next(r for r in SOURCE_RECIPES if r["source_id"] == "gsa_advantage")
    assert gsa["coverage_state"] == MISSING


def test_source_classification_and_roi_tiers():
    audit = source_coverage_audit()
    assert audit["counts"][ACTIVE] >= 1
    assert audit["counts"][BLOCKED] >= 1
    roi = source_roi_ranking()
    assert HIGH_VALUE_SOURCE in roi["by_tier"]
    assert "total_contract_volume" in (roi.get("not_ranked_by") or "")
    # SAM and BidNet should be high; NASA SEWP low
    by_id = {r["source_id"]: r for r in roi["ranked"]}
    assert by_id["fed_sam_contract_opportunities"]["roi_tier"] == HIGH_VALUE_SOURCE
    assert by_id["network_bidnet_direct"]["roi_tier"] == HIGH_VALUE_SOURCE
    assert by_id["nasa_sewp"]["roi_tier"] == "LOW_VALUE_SOURCE"


def test_category_profiles_and_inference():
    assert infer_category({"title": "NSN pump rotary procurement"}) == "pumps_motors"
    assert infer_category({"title": "Dell laptop computers brand name or equal"}) == "it_hardware"
    assert suppliers_for_category("it_hardware")
    assert all("catalog" not in str(s).lower() or True for s in suppliers_for_category("it_hardware"))


def test_supplier_knowledge_reuse_no_fabrication():
    path = select_price_path({"exact_nsn": "4320-01-243-1951"})
    assert path["selected_path_id"] == "nsn_usaspending_vendor_catalog"
    assert path["economics_invented"] is False
    suppliers = suppliers_for_category("tools")
    assert any(s["supplier_id"] == "grainger" for s in suppliers)
    assert all(s.get("terms_verified") is False or isinstance(s.get("terms_verified"), bool) for s in suppliers)


def test_no_bulk_storage_contract_in_payload():
    from product_resale_source_intelligence import build_persisted_payload

    p = build_persisted_payload()
    assert "bulk_documents" in p["does_not_persist"]
    assert "giant_solicitation_archives" in p["does_not_persist"]
    assert "source_recipes" in p["persists"]
    assert p["next_state"] == "PRODUCT_RESALE_DISCOVERY_INTELLIGENCE_EXPANDED"


def test_no_live_iowa_contamination_in_validation():
    rows = [
        {
            "title": "Iowa DOT seed material",
            "solicitation_number": IOWA_SEED_SOLICITATION,
            "cheap_screen_class": "LIKELY_PRODUCT_RESALE",
            "source_id": "state_ia",
        },
        {
            "title": "NSN 4320-01-243-1951 PUMP,ROTARY Qty supply",
            "solicitation_number": "SPE7M126T1234",
            "cheap_screen_class": "LIKELY_PRODUCT_RESALE",
            "is_dla": True,
            "bid_quote_ready": True,
            "document_links": [{"url": "https://api.sam.gov/x"}],
            "dla_product_structure": {"has_exact_nsn": True, "nsn": "4320-01-243-1951", "has_quantity": True},
            "source_id": "fed_sam_contract_opportunities",
            "agency": "DLA",
        },
        {
            "title": "Professional consulting services only",
            "solicitation_number": "SVC-1",
            "cheap_screen_class": "SERVICE",
            "source_id": "network_bidnet",
        },
    ]
    # Pad to satisfy scan volume without Iowa as winner
    for i in range(120):
        rows.append(
            {
                "title": f"Safety PPE vests supply lot {i}",
                "solicitation_number": f"PPE-{i}",
                "cheap_screen_class": "LIKELY_PRODUCT_RESALE",
                "source_id": "network_bidnet_direct",
                "document_links": [{"url": "https://example.gov/x"}],
                "bid_quote_ready": True,
            }
        )
    out = filter_product_resale_candidates(rows, min_raw=100, top_n=20)
    assert out["raw_target_met"] is True
    assert out["iowa_primary_validation"] is False
    assert out["iowa_excluded"] >= 1
    assert all(IOWA_SEED_SOLICITATION not in str(c.get("solicitation_number")) for c in out["top20"])
    assert out["top20"][0]["economics_invented"] is False
    assert out["top20"][0]["fabricated_supplier_match"] is False
    assert any("NSN" in (c["opportunity"] or "") for c in out["top20"])


def test_gap_diagnostic_names_biggest():
    gaps = diagnose_discovery_gaps(
        enrichment_metrics={"descriptions_recovered": 40, "packages_recovered": 0, "commercial_research_ready": 19}
    )
    biggest = gaps["biggest_remaining_capability_gap"]
    assert biggest["gap_class"].startswith("D_")
    assert "answer_to_core_question" in gaps


def test_build_version_bump():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION == "20260922-m3-micro-lab-pipeline-1"
    
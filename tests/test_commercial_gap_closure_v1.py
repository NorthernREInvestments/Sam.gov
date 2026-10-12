"""Acceptance tests for commercial gap closure — no fabricated economics."""

from __future__ import annotations

from buyer_intelligence.commercial_gap_closure_v1 import (
    _price_evidence_compatible,
    classify_opportunity_completeness,
    detect_bidding_structure,
)


def test_rejects_mpn_collision_consumer_retail():
    ok, reason = _price_evidence_compatible(
        mpn="1092125",
        description="Part # 1092125 CIRCUIT BREAKER 15KV VACUUM INTERRUPTER 1200A",
        manufacturer=None,
        url="https://www.revzilla.com/motorcycle/kuryakyn-replacement-rubber-pads",
        supplier="revzilla.com",
    )
    assert ok is False
    assert reason == "CATEGORY_MISMATCH_CONSUMER_RETAIL"


def test_rejects_velvac_for_mopec():
    ok, reason = _price_evidence_compatible(
        mpn="600017",
        description="MOPEC STANDARD AUTOPSY CARRIER CHASSIS",
        manufacturer="MOPEC",
        url="https://www.velvac.com/products/brass-non-locking-fuel-cap-600017",
        supplier="velvac.com",
    )
    assert ok is False


def test_accepts_mopec_product_page():
    ok, reason = _price_evidence_compatible(
        mpn="600009-HC1",
        description="MOPEC BARIATRIC CARRIERS WITH SINGLE-SIDED HYDRAULIC CRANK",
        manufacturer="MOPEC",
        url="https://www.mopec.com/product/product-bariatric-carrier-chassis-600009hc1-2/",
        supplier="mopec.com",
    )
    assert ok is True
    assert reason is None


def test_rejects_serp_pages():
    ok, reason = _price_evidence_compatible(
        mpn="AWN1300",
        description="Lochinvar boiler",
        manufacturer="Lochinvar",
        url="https://html.duckduckgo.com/html/?q=Lochinvar+AWN1300+price",
        supplier="duckduckgo.com",
    )
    assert ok is False
    assert reason == "SEARCH_PAGE_NOT_PRODUCT_PAGE"


def test_bidding_structure_does_not_assume_partial():
    structure = detect_bidding_structure({"TITLE": "Annual supply agreement"}, [{"DESCRIPTION": "Widget", "QTY": 1, "MPN": "X1"}])
    assert structure["partial_bids_allowed"] is None
    assert "UNKNOWN_DO_NOT_ASSUME_PARTIAL" in structure["evidence"]


def test_classify_completeness_shape():
    # Smoke against real store if present; otherwise empty entry is incomplete
    row = classify_opportunity_completeness("__missing__")
    assert row["full_mandatory_coverage"] is False
    assert "NO_PRODUCT_LINES" in row["blockers"]

"""BUILD 11 — Offer Blocker Read Model tests."""

from __future__ import annotations

from m3_offer_readiness_read import (
    BLOCKED,
    BUILD_TAG,
    COMPLETE,
    HARD,
    READY,
    RESEARCH_REQUIRED,
    SOFT,
    ST_UNKNOWN,
    attach_offer_readiness_to_deal_room,
    build_offer_readiness_profile,
)


def _row_base(**extra):
    row = {
        "canonical_id": "sol:SPE7M126OR1:dla",
        "agency": "DLA",
        "buyer": "DLA Aviation",
        "title": "NSN 4320-01-243-1951 PUMP",
        "deadline": "2026-10-01",
        "solicitation_number": "SPE7M1-26-Q-0001",
        "lifecycle": "RESEARCH",
    }
    row.update(extra)
    return row


def test_existing_compliance_data_appears():
    row = _row_base(
        bid_compliance={
            "compliance_matrix": {
                "rows": [
                    {
                        "category": "COUNTRY_OF_ORIGIN",
                        "requirement": "Buy American applies",
                        "current_status": "UNRESOLVED",
                        "mandatory": True,
                        "severity": "MANDATORY_MATERIAL",
                        "evidence": {"fact": "origin_unknown"},
                        "source_snippet": "Buy American Act",
                    },
                    {
                        "category": "AUTHORIZED_RESELLER",
                        "requirement": "Authorized reseller required",
                        "current_status": "BLOCKED",
                        "mandatory": True,
                        "severity": "HARD_BLOCKER_IF_UNSATISFIED",
                        "blocking": True,
                        "evidence": "No authorization on file",
                    },
                ]
            }
        }
    )
    profile = build_offer_readiness_profile(row, research_items=[])
    cats = {r["category"] for r in profile["compliance"]["requirements"]}
    assert "COUNTRY_OF_ORIGIN" in cats
    assert "SOURCE_APPROVAL" in cats
    assert any("Buy American" in str(r.get("requirement")) for r in profile["compliance"]["requirements"])


def test_unknown_remains_visible():
    row = _row_base()
    profile = build_offer_readiness_profile(row, research_items=[])
    unknowns = [r for r in profile["compliance"]["requirements"] if r.get("status") == ST_UNKNOWN]
    assert unknowns
    assert profile["compliance"]["counts"]["unknown"] >= 1
    assert profile["unknown_is_not_automatic_failure"] is True
    # UNKNOWN scaffold alone must not force BLOCKED
    assert profile["readiness_state"] != BLOCKED or profile["blockers"]["hard"]


def test_hard_vs_soft_blockers_separated():
    row = _row_base(
        bid_compliance={
            "forms": {
                "forms": [
                    {"form_name": "SF-1449", "requirement": "REQUIRED", "form_status": "FORM_MISSING"},
                ]
            },
            "compliance_matrix": {
                "rows": [
                    {
                        "category": "PACKAGING",
                        "requirement": "MIL-STD packaging unclear",
                        "current_status": "RESEARCH_REQUIRED",
                        "mandatory": False,
                    }
                ]
            },
        },
        dla_product_structure={"packaging_signal": True, "fields": {}},
    )
    profile = build_offer_readiness_profile(row, research_items=[])
    assert profile["blockers"]["hard"]
    assert profile["blockers"]["soft"]
    assert profile["readiness_state"] == BLOCKED
    hard_cats = {r["category"] for r in profile["blockers"]["hard"]}
    soft_cats = {r["category"] for r in profile["blockers"]["soft"]}
    assert "FORMS" in hard_cats
    assert "PACKAGING" in soft_cats


def test_missing_supplier_creates_correct_action():
    row = _row_base(
        product_identity={"identity_state": "EXACT_NSN", "confidence": "HIGH", "nsn": "4320-01-243-1951"},
        dla_product_structure={
            "nsn": "4320-01-243-1951",
            "has_exact_nsn": True,
            "approved_source_signal": True,
            "fields": {"nsn": {"value": "4320-01-243-1951", "confidence": "HIGH"}},
        },
        deal_economics={"PRICE_CONFIDENCE": "HIGH"},
        execution_intelligence={"Financing_Fit": "HIGH"},
    )
    profile = build_offer_readiness_profile(
        row,
        research_items=[
            {
                "opportunity_id": row["canonical_id"],
                "research_type": "SUPPLIER",
                "status": "NEW",
                "why_this_matters": "Supplier authorization unknown",
                "recommended_action": "Contact supplier",
                "missing_information": ["supplier_channels"],
            }
        ],
    )
    soft = profile["blockers"]["soft"]
    assert soft
    supplierish = [
        b
        for b in soft
        if b.get("category") == "SOURCE_APPROVAL"
        or "supplier" in str(b.get("missing_action") or "").lower()
        or "supplier" in str(b.get("requirement") or "").lower()
    ]
    assert supplierish
    actions = " ".join(str(b.get("missing_action") or "") for b in supplierish).lower()
    assert "supplier" in actions or "authorization" in actions or "acquisition" in actions


def test_missing_packaging_creates_correct_action():
    row = _row_base(
        product_identity={"identity_state": "EXACT_NSN", "confidence": "HIGH"},
        dla_product_structure={
            "has_exact_nsn": True,
            "packaging_signal": True,
            "fields": {"nsn": {"value": "4320-01-243-1951", "confidence": "HIGH"}},
        },
        deal_economics={"PRICE_CONFIDENCE": "HIGH"},
        execution_intelligence={"Financing_Fit": "SATISFIED"},
    )
    profile = build_offer_readiness_profile(row, research_items=[])
    pack = [b for b in profile["blockers"]["soft"] if b.get("category") == "PACKAGING"]
    assert pack
    assert "packaging" in str(pack[0].get("missing_action") or "").lower()
    assert pack[0].get("blocker_type") == SOFT
    assert profile["readiness_state"] == RESEARCH_REQUIRED


def test_complete_opportunity_can_show_ready():
    row = _row_base(
        product_identity={"identity_state": "EXACT_NSN", "confidence": "HIGH", "nsn": "4320-01-243-1951"},
        dla_product_structure={
            "nsn": "4320-01-243-1951",
            "has_exact_nsn": True,
            "fields": {"nsn": {"value": "4320-01-243-1951", "confidence": "HIGH"}},
        },
        bid_compliance={
            "compliance_matrix": {
                "rows": [
                    {
                        "category": "COUNTRY_OF_ORIGIN",
                        "requirement": "TAA compliant",
                        "current_status": "SATISFIED",
                        "mandatory": True,
                        "evidence": "origin verified",
                    }
                ]
            },
            "submission_instructions": {"method": "DIBBS"},
        },
        deal_economics={"PRICE_CONFIDENCE": "HIGH"},
        execution_intelligence={"Financing_Fit": "SATISFIED"},
    )
    profile = build_offer_readiness_profile(row, research_items=[], include_category_scaffold=True)
    assert profile["readiness_state"] == READY
    assert not profile["blockers"]["hard"]
    assert not profile["blockers"]["soft"]
    assert any(r.get("blocker_type") == COMPLETE for r in profile["compliance"]["requirements"])


def test_attach_preserves_deal_room_keys():
    from m3_mobile_read_model import deal_room_summary

    row = _row_base(
        product_identity={"identity_state": "EXACT_NSN", "confidence": "HIGH"},
        deal_economics={"PRICE_CONFIDENCE": "HIGH"},
        execution_intelligence={"Financing_Fit": "HIGH"},
    )
    deal = deal_room_summary(row)
    keys = set(deal.keys())
    enriched = attach_offer_readiness_to_deal_room(deal, row=row)
    assert "offer_readiness" in enriched
    assert keys.issubset(set(enriched.keys()))
    assert enriched["offer_readiness"]["kind"] == "M3OfferReadinessProfile"


def test_engines_unchanged_endpoints():
    from app import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/m3/mobile/deal/{canonical_id}" in paths
    # Optional specialized readiness routes — presence is informational
    for optional in (
        "/api/m3/offer-readiness/{opportunity_id}",
        "/api/m3/mobile/offer-readiness/{opportunity_id}",
        "/api/m3/research-queue",
        "/api/m3/market-hunt",
    ):
        _ = optional in paths

    # Existing compliance engine import still works and is not replaced
    from bid_compliance_engine import analyze_bid_compliance
    from bid_readiness_engine import evaluate_bid_readiness_ladder

    assert callable(analyze_bid_compliance)
    assert callable(evaluate_bid_readiness_ladder)


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
    assert BUILD_TAG.startswith("20260918-m3-offer-readiness")

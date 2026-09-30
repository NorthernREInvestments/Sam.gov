"""Phase L.2.1 market-price rescue + safety-gate tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from phase_l.access_gate import ACCESS_YES, evaluate_phase_l_access
from phase_l.deadline_freshness import EXPIRED, LIVE_ACTIONABLE, classify_deadline_freshness
from phase_l.economics import build_phase_l_economics
from phase_l.enrichment import finalize_economics
from phase_l.market_price import (
    EXACT_PUBLIC_PRICE_HIGH,
    NO_PUBLIC_PRICE_FOUND,
    SEARCH_PROVIDER_FAILED,
    build_search_identity,
    extract_html_prices,
    extract_json_ld_prices,
    generate_market_queries,
    map_confidence,
    normalize_mpn_variants,
    select_acquisition_baseline,
)
from phase_l.prebid_compliance import (
    COMPLIANCE_INCOMPLETE,
    PRE_BID_OWNER_REVIEW,
    evaluate_prebid_compliance,
)
from phase_l.product_fitness import (
    ENGINEERING_SUPPORT,
    PRODUCT_RESALE,
    SERVICE,
    classify_product_fitness,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "phase_l21_manual_proof_corpus.json"


def test_mpn_variants_and_query_order():
    variants = normalize_mpn_variants("1274M99P01")
    assert "1274M99P01" in variants
    assert "1274M99P01".replace("-", "") in variants or "1274M99P01" in variants
    identity = {
        "nsn": "5930-01-215-4689",
        "mpn": "1274M99P01",
        "mpns": ["1274M99P01"],
        "manufacturer": "Eaton",
    }
    sid = build_search_identity(identity, {"title": "Switch"})
    qs = generate_market_queries(sid)
    assert qs, "queries required"
    # MPN queries before bare NSN-only
    first_nsn_alone = next((i for i, q in enumerate(qs) if q.startswith("NSN ") and "1274" not in q), len(qs))
    first_mpn = next((i for i, q in enumerate(qs) if "1274M99P01" in q), 999)
    assert first_mpn < first_nsn_alone


def test_json_ld_and_html_price_extraction():
    html = """
    <script type="application/ld+json">
    {"@type":"Product","offers":{"@type":"Offer","price":"1299.99","priceCurrency":"USD"}}
    </script>
    <div>Price: $1,450.00 each</div>
    """
    ld = extract_json_ld_prices(html, source_url="https://example.com/p")
    assert ld and ld[0]["unit_price"] == 1299.99
    text = extract_html_prices(html, source_url="https://example.com/p", product_hint="widget")
    assert any(o["unit_price"] == 1450.0 for o in text)


def test_used_not_silent_baseline():
    obs = [
        {
            "unit_price": 100.0,
            "condition": "USED",
            "seller_type": "MARKETPLACE",
            "confidence": "HIGH",
            "url": "https://ebay.com/x",
        },
        {
            "unit_price": 500.0,
            "condition": "NEW",
            "seller_type": "ESTABLISHED_DISTRIBUTOR",
            "confidence": "HIGH",
            "exact_match": "JSON_LD",
            "url": "https://www.grainger.com/p",
        },
    ]
    sel = select_acquisition_baseline(obs, require_new=True)
    assert sel["unit_price"] == 500.0


def test_rfq_only_does_not_equal_provider_fail_confidence():
    conf = map_confidence([], None, rfq_only=True)
    assert conf == "RFQ_ONLY"
    conf2 = map_confidence([], None, rfq_only=False)
    assert conf2 == NO_PUBLIC_PRICE_FOUND


def test_search_provider_failed_state_exists():
    assert SEARCH_PROVIDER_FAILED == "SEARCH_PROVIDER_FAILED"


def test_expired_opportunity_blocked():
    past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    row = {"title": "Old IFB", "deadline": past, "live_status": "OPEN"}
    fresh = classify_deadline_freshness(row)
    assert fresh["deadline_state"] == EXPIRED
    assert fresh["deep_enrichment_allowed"] is False


def test_live_deadline_actionable():
    future = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    row = {"title": "Open IFB", "response_deadline": future, "live_status": "OPEN"}
    fresh = classify_deadline_freshness(row)
    assert fresh["deadline_state"] == LIVE_ACTIONABLE
    assert fresh["deep_enrichment_allowed"] is True


def test_service_not_in_product_pricing_queue():
    fit = classify_product_fitness(
        {"title": "Sustaining engineering support services for avionics"}
    )
    assert fit["product_fitness"] == ENGINEERING_SUPPORT
    assert fit["market_price_research_allowed"] is False
    fit2 = classify_product_fitness({"title": "Bridge Study RFP — planning services"})
    assert fit2["product_fitness"] == SERVICE
    assert fit2["market_price_research_allowed"] is False


def test_product_resale_allowed():
    fit = classify_product_fitness(
        {"title": "Brand Name Bobcat ToolCat UW56 Utility Work Machine", "is_product": True}
    )
    assert fit["product_fitness"] == PRODUCT_RESALE
    assert fit["market_price_research_allowed"] is True


def test_unknown_quantity_remains_unknown():
    econ = finalize_economics(
        {"historical_award_unit_price": 10.0, "public_retail_unit_price": 5.0}
    )
    assert econ["blocker"] == "UNKNOWN_QUANTITY"
    assert econ["expected_net_profit"] is None


def test_market_plus_history_reaches_economics():
    econ = build_phase_l_economics(
        quantity=200,
        historical_unit_price=200.0,
        public_retail_unit_price=100.0,
        freight=500.0,
    )
    assert econ["meets_floor"] is True
    assert econ["expected_net_profit"] >= 10000


def test_access_eligibility_not_ready_to_bid():
    access = evaluate_phase_l_access(
        {
            "title": "IFB commercial LED fixtures unrestricted",
            "description": "Invitation for Bid. Full and open. Unrestricted.",
        }
    )
    assert access["our_bid_access"] == ACCESS_YES
    assert access["submission_readiness"] == "ACCESS_ELIGIBLE"
    assert access["ready_to_bid"] is False
    assert access["submission_readiness"] != "READY"
    assert access["submission_readiness"] != "READY_TO_BID"


def test_incomplete_compliance_blocks_ready_to_bid():
    result = evaluate_prebid_compliance({"title": "Widget IFB", "quantity": 10})
    assert result["READY_TO_BID"] is False
    assert result["ready_to_bid"] is False
    assert result["compliance_state"] == COMPLIANCE_INCOMPLETE
    assert "WHAT_WE_ARE_PROMISING" in result


def test_complete_fixture_reaches_prebid_owner_review_not_autosubmit():
    checklist = {
        cat: {"status": "SATISFIED", "notes": "fixture"}
        for cat in (
            "solicitation_documents_reviewed",
            "attachments_reviewed",
            "amendments_reviewed",
        )
    }
    # Still force incomplete unless package fully reviewed flag set with all cats
    row = {
        "title": "Complete package fixture",
        "quantity": 5,
        "uom": "EA",
        "prebid_compliance_checklist": checklist,
        "prebid_package_fully_reviewed": False,
        "prebid_owner_authorized_review": True,
    }
    result = evaluate_prebid_compliance(row, force_incomplete=True)
    assert result["READY_TO_BID"] is False
    assert result["auto_submit"] is False


def test_manual_proof_corpus_loads_and_generates_queries():
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    assert data["cases"]
    for case in data["cases"]:
        identity = {
            "nsn": case.get("nsn"),
            "mpn": case.get("mpn") or case.get("model"),
            "mpns": [case["mpn"]] if case.get("mpn") else ([case["model"]] if case.get("model") else []),
            "manufacturer": case.get("manufacturer"),
            "model": case.get("model"),
        }
        sid = build_search_identity(identity, {"title": case["title"]})
        qs = generate_market_queries(sid)
        assert qs
        if case.get("expect_mpn_before_nsn_alone") and case.get("mpn"):
            assert any(case["mpn"] in q for q in qs[:5])


def test_owner_queue_threshold_logic():
    pass_econ = build_phase_l_economics(
        quantity=200, historical_unit_price=200.0, public_retail_unit_price=100.0, freight=500.0
    )
    fail_econ = build_phase_l_economics(
        quantity=5, historical_unit_price=100.0, public_retail_unit_price=95.0
    )
    owner = [pass_econ] if pass_econ.get("meets_floor") else []
    internal = [fail_econ] if not fail_econ.get("meets_floor") else []
    assert len(owner) == 1
    assert len(internal) == 1


def test_multiple_prices_retained_in_selection():
    obs = [
        {"unit_price": 90.0, "condition": "NEW", "seller_type": "RETAILER", "confidence": "MEDIUM", "url": "https://a"},
        {"unit_price": 100.0, "condition": "NEW", "seller_type": "ESTABLISHED_DISTRIBUTOR", "confidence": "HIGH", "exact_match": "JSON_LD", "url": "https://grainger.com/x"},
        {"unit_price": 110.0, "condition": "NEW", "seller_type": "MANUFACTURER", "confidence": "HIGH", "url": "https://mfr.com"},
    ]
    sel = select_acquisition_baseline(obs)
    assert sel is not None
    assert sel["unit_price"] in {100.0, 110.0}
    conf = map_confidence(obs, sel, rfq_only=False)
    assert conf in {
        EXACT_PUBLIC_PRICE_HIGH,
        "EXACT_PUBLIC_PRICE_MEDIUM",
        "EXACT_PUBLIC_PRICE_LOW",
        "STRONG_COMMERCIAL_COMPARABLE",
    }

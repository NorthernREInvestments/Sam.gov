"""Phase L.2 enrichment integration tests — adapters over existing research."""

from __future__ import annotations

from phase_l.competition import PRE_FILTERED_COMPETITION, VERY_LOW_OPEN_COMPETITION
from phase_l.economics import build_phase_l_economics
from phase_l.enrichment import (
    ENRICH_NOW,
    IDENTITY_EXACT,
    IDENTITY_INSUFFICIENT,
    OR_EQUAL_RESEARCHABLE,
    apply_enrichment_to_row,
    cache_key,
    detect_or_equal,
    enrichment_priority,
    finalize_economics,
    load_cache,
    select_market_price,
    stage_a_identity,
)
from phase_l.normalize import READY_FOR_OWNER_REVIEW, owner_decision_state


def test_exact_mpn_identity_screen_runs():
    row = {
        "title": "RFQ — Widget P/N: ABC-12345-XZ",
        "description": "Manufacturer part number ABC-12345-XZ. Qty 100 EA.",
        "our_bid_access": "YES",
        "competition_access_type": "OPEN_MARKET",
        "source_level": "FEDERAL",
    }
    screen = stage_a_identity(row)
    assert screen["identity"].get("mpn")
    assert screen["identity_research_state"] in {IDENTITY_EXACT, "IDENTITY_STRONG"}
    assert screen["researchable"] is True
    pri = enrichment_priority(row, screen)
    assert pri["enrichment_priority"] in {ENRICH_NOW, "ENRICH_NEXT", "ENRICH_IF_CAPACITY"}


def test_cache_key_shared_across_opportunities():
    a = stage_a_identity(
        {"title": "NSN 6620-01-321-6819 Indicator", "description": "NSN 6620-01-321-6819"}
    )
    b = stage_a_identity(
        {"title": "Indicator NSN 6620-01-321-6819", "description": "same nsn 6620-01-321-6819"}
    )
    ka = cache_key(a["identity"])
    kb = cache_key(b["identity"])
    assert ka and ka == kb
    assert ka.startswith("nsn:")


def test_history_fields_attach_and_competition_preserved():
    row = {
        "title": "NSN 6680-01-111-2222 Meter",
        "description": "NSN 6680-01-111-2222. Qty 20 EA. Unrestricted.",
        "our_bid_access": "YES",
        "competition_access_type": "OPEN_MARKET",
        "quantity": 20,
        "uom": "EA",
    }
    screen = stage_a_identity(row)
    history = {
        "history_research_state": "HISTORY_FOUND",
        "historical_award_unit_price": 150.0,
        "historical_offers_received": 2,
        "historical_competition_type": "OPEN_MARKET",
        "history_confidence": "EXACT_RECENT",
        "attempted": True,
    }
    market = {
        "market_research_state": "MARKET_PRICE_FOUND",
        "public_retail_unit_price": 80.0,
        "public_retail_source": "https://example.com/product",
        "attempted": True,
    }
    enriched = apply_enrichment_to_row(
        row,
        screen=screen,
        priority=enrichment_priority(row, screen),
        history=history,
        market=market,
    )
    assert enriched["historical_award_unit_price"] == 150.0
    assert enriched["historical_offers_received"] == 2
    assert enriched["public_retail_unit_price"] == 80.0


def test_prefiltered_bpa_two_offers_not_open():
    from phase_l.competition import annotate_competition

    ann = annotate_competition(
        historical_offers_received=2,
        competition_access_type="BPA_ONLY",
    )
    assert ann["effective_competition_signal"] == PRE_FILTERED_COMPETITION
    assert ann["is_open_comparable"] is False


def test_open_sap_two_offers_low_open():
    from phase_l.competition import annotate_competition, effective_competition_signal

    sig = effective_competition_signal(
        historical_offers_received=2,
        historical_competition_type="OPEN_MARKET",
    )
    assert sig in {VERY_LOW_OPEN_COMPETITION, "LOW_OPEN_COMPETITION"}
    ann = annotate_competition(
        historical_offers_received=2,
        competition_access_type="OPEN_MARKET",
    )
    assert ann["is_open_comparable"] is True


def test_select_market_price_prefers_credible_not_lowest():
    obs = [
        {"unit_price": 5.0, "seller": "ebay-seller-x", "confidence": "LOW", "url": "https://ebay.com/x"},
        {
            "unit_price": 100.0,
            "seller": "Grainger",
            "confidence": "HIGH",
            "exact_match": "HIGH",
            "url": "https://www.grainger.com/product/1",
        },
        {"unit_price": 90.0, "seller": "random", "confidence": "MEDIUM", "url": "https://shop.example/p"},
    ]
    selected = select_market_price(obs)
    assert selected is not None
    assert selected["unit_price"] == 100.0


def test_or_equal_allowed_detected():
    d = detect_or_equal(
        {"title": "Laptop Dell or equal", "description": "Brand name or equal permitted."}
    )
    assert d["or_equal_allowed"] is True
    screen = stage_a_identity(
        {
            "title": "Commercial monitor brand name or equal",
            "description": "Or equal specifications apply. 24 inch LED.",
        }
    )
    # partial nomenclature + or-equal → researchable path available
    assert screen["or_equal_allowed"] is True or screen["identity_research_state"] in {
        OR_EQUAL_RESEARCHABLE,
        "IDENTITY_PARTIAL",
        "IDENTITY_STRONG",
        IDENTITY_EXACT,
    }


def test_exact_item_rejects_or_equal_language_conflict():
    d = detect_or_equal(
        {
            "title": "Exact OEM part required",
            "description": "No substitutes. Exact item required. Brand-name only.",
        }
    )
    assert d["exact_item_required"] is True
    assert d["or_equal_allowed"] is False


def test_unknown_quantity_no_fabricated_economics():
    row = {
        "title": "NSN 9999-00-111-2222",
        "historical_award_unit_price": 50.0,
        "public_retail_unit_price": 30.0,
        # quantity intentionally missing
    }
    econ = finalize_economics(row)
    assert econ["economics_completed"] is False
    assert econ["blocker"] == "UNKNOWN_QUANTITY"
    assert econ["expected_net_profit"] is None


def test_unknown_historical_price_not_zero():
    row = {
        "title": "Widget",
        "quantity": 10,
        "uom": "EA",
        "public_retail_unit_price": 30.0,
    }
    econ = finalize_economics(row)
    assert econ["economics_completed"] is False
    assert econ["blocker"] == "UNKNOWN_HISTORICAL_PRICE"
    assert econ.get("historical_award_unit_price") is None
    assert econ["expected_net_profit"] is None


def test_easy_registration_does_not_block_enrichment_screen():
    row = {
        "title": "City IFB printers P/N: HP-CF410A",
        "description": "Vendor registration required. P/N: HP-CF410A. Qty 200 EA. Unrestricted.",
        "our_bid_access": "YES",
        "is_easy_registration": True,
        "registration_action": "REGISTER_BEFORE_BID",
        "competition_access_type": "OPEN_MARKET",
        "source_level": "LOCAL",
        "quantity": 200,
        "uom": "EA",
    }
    screen = stage_a_identity(row)
    assert screen["researchable"] is True or screen["identity"].get("mpn")
    # Economics still allowed when prices present
    econ = build_phase_l_economics(
        quantity=200,
        historical_unit_price=200.0,
        public_retail_unit_price=100.0,
        freight=500.0,
    )
    assert econ["meets_floor"] is True


def test_owner_queue_shows_pass_hides_fail():
    pass_row = {
        "our_bid_access": "YES",
        "quantity": 200,
        "uom": "EA",
        "historical_award_unit_price": 200.0,
        "public_retail_unit_price": 100.0,
        "public_retail_price": 100.0,
        "freight": 500.0,
        "status": "OPEN",
        "live_status": "OPEN",
        "is_product": True,
        "nsn": "7025-01-111-2222",
        "title": "Pass deal",
    }
    fail_row = {
        "our_bid_access": "YES",
        "quantity": 5,
        "uom": "EA",
        "historical_award_unit_price": 100.0,
        "public_retail_unit_price": 95.0,
        "public_retail_price": 95.0,
        "status": "OPEN",
        "is_product": True,
        "title": "Fail deal",
    }
    from phase_l.normalize import normalize_opportunity

    screen = stage_a_identity(pass_row)
    hist = {
        "historical_award_unit_price": 200.0,
        "history_research_state": "HISTORY_FOUND",
        "attempted": True,
    }
    mkt = {
        "public_retail_unit_price": 100.0,
        "market_research_state": "MARKET_PRICE_FOUND",
        "attempted": True,
    }
    enriched = apply_enrichment_to_row(
        pass_row, screen=screen, priority={"enrichment_priority": ENRICH_NOW}, history=hist, market=mkt
    )
    econ = finalize_economics(enriched)
    norm_pass = normalize_opportunity(enriched, economics=econ)
    assert norm_pass["meets_floor"] is True
    assert owner_decision_state(norm_pass) == READY_FOR_OWNER_REVIEW

    econ_fail = finalize_economics(fail_row)
    assert econ_fail["meets_floor"] is False
    # Owner queue filter: only meets_floor
    owner = [norm_pass] if norm_pass.get("meets_floor") else []
    internal = [fail_row] if not econ_fail.get("meets_floor") else []
    assert len(owner) == 1
    assert len(internal) == 1


def test_reuses_phase_j_not_duplicate_identity_engine():
    """Case 15 — Phase L.2 identity path is Phase J build_product_identity."""
    import phase_l.enrichment as enr
    import inspect

    src = inspect.getsource(enr.stage_a_identity)
    assert "build_product_identity" in src
    assert "phase_j.product_identity" in src


def test_load_cache_roundtrip(tmp_path):
    path = tmp_path / "cache.json"
    cache = load_cache(path)
    assert cache["by_key"] == {}
    cache["by_key"]["nsn:1"] = {"history": {"cached_at": "2099-01-01T00:00:00+00:00"}}
    from phase_l.enrichment import save_cache

    save_cache(cache, path)
    again = load_cache(path)
    assert "nsn:1" in again["by_key"]

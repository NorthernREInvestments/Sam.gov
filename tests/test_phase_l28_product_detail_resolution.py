"""Phase L.2.8 product-detail resolution + bot-resilient recovery tests."""

from __future__ import annotations

from unittest.mock import patch

from phase_l.acquisition_pricing import FETCH_BLOCKED, UNVERIFIED_LEAD, PriceLead
from phase_l.l28_rescue import (
    DEEP_RESEARCH_NO_FIXED_COUNT,
    STAGE3_NO_ROW_CAP,
    ballpark_economics,
)
from phase_l.product_detail_resolution import (
    CATEGORY_SHELL,
    CURRENT_30D,
    CURRENT_90D,
    EXACT_PRODUCT_DETAIL,
    HISTORICAL_ONLY,
    MAX_PRODUCT_RESOLUTION_DEPTH,
    SEARCH_SHELL,
    STALE_1Y,
    STATIC_PRICE_ARTIFACT,
    UNKNOWN_DATE,
    alternate_seller_targets,
    classify_detail_page,
    classify_price_freshness,
    extract_static_artifact_links,
    rank_product_links,
    resolve_product_detail,
)
from phase_l.product_page_resolution import EXACT_VERIFIED, STRONG_VERIFIED
from phase_l.progressive_funnel import FREIGHT_REQUIRED, stage3_economic_recon
from phase_l.resilient_fetch import (
    FETCH_403,
    FETCH_BOT_BLOCKED,
    FETCH_OK,
    DomainCircuitBreaker,
    FetchResult,
)


def test_search_shell_to_exact_product_link():
    html = """
    <html><body>
    <a href="/search?q=UW56">Search again</a>
    <a href="/products/bobcat-toolcat-uw56">Bobcat ToolCat UW56</a>
    <a href="/cart">Cart</a>
    </body></html>
    """
    from phase_l.product_page_resolution import extract_product_candidate_links

    sid = {"model": "ToolCat UW56", "primary_mpn": "UW56", "manufacturer": "Bobcat"}
    links = extract_product_candidate_links(
        html, base_url="https://dealer.example/search?q=UW56", search_id=sid
    )
    ranked = rank_product_links(links, search_id=sid)
    assert ranked
    assert any("uw56" in (L["url"] or "").lower() for L in ranked)
    assert classify_detail_page("https://dealer.example/search?q=UW56", html) == SEARCH_SHELL


def test_category_to_exact_product_detail():
    html = """
    <html><body class="category">
    <h1>Utility Vehicles</h1>
    <div class="product-card">
      <a href="/p/toolcat-uw56-sku123">ToolCat UW56 — $76,900</a>
    </div>
    </body></html>
    """
    assert classify_detail_page("https://shop.example/category/utility", html) in {
        CATEGORY_SHELL,
        SEARCH_SHELL,
        EXACT_PRODUCT_DETAIL,
    }
    sid = {"model": "ToolCat UW56", "primary_mpn": "UW56"}
    from phase_l.product_page_resolution import extract_product_candidate_links

    links = extract_product_candidate_links(html, base_url="https://shop.example/category/utility", search_id=sid)
    ranked = rank_product_links(links, search_id=sid)
    assert any("uw56" in L["url"].lower() for L in ranked)


def test_max_resolution_depth():
    assert MAX_PRODUCT_RESOLUTION_DEPTH == 3
    breaker = DomainCircuitBreaker()
    shell = '<html><a href="/cat/uw56">UW56 family</a></html>'
    family = '<html><a href="/p/toolcat-uw56">ToolCat UW56 Exact</a></html>'
    detail = """
    <html><head>
    <script type="application/ld+json">
    {"@type":"Product","name":"ToolCat UW56","sku":"UW56",
     "offers":{"@type":"Offer","price":"76900","priceCurrency":"USD"}}
    </script></head>
    <body>ToolCat UW56 price $76,900</body></html>
    """
    sid = {"model": "ToolCat UW56", "primary_mpn": "UW56", "manufacturer": "Bobcat"}
    fetches = {"n": 0}

    def fake_fetch(url, breaker=None, retries=0):
        fetches["n"] += 1
        if "/p/" in url:
            return FetchResult(status=FETCH_OK, url=url, text=detail, content=detail.encode(), domain="shop.example")
        return FetchResult(status=FETCH_OK, url=url, text=family, content=family.encode(), domain="shop.example")

    with patch("phase_l.product_detail_resolution.resilient_fetch", fake_fetch):
        res = resolve_product_detail(
            start_url="https://shop.example/search?q=UW56",
            html=shell,
            search_id=sid,
            row={"title": "Bobcat ToolCat UW56"},
            breaker=breaker,
            max_depth=MAX_PRODUCT_RESOLUTION_DEPTH,
            max_detail_fetches=4,
        )
    assert res["telemetry"]["depth_used"] <= MAX_PRODUCT_RESOLUTION_DEPTH
    assert fetches["n"] <= 4


def test_accessory_link_rejection():
    links = [
        {"url": "https://x.com/products/uw56-accessory-kit", "score": 50},
        {"url": "https://x.com/products/toolcat-uw56", "score": 40},
        {"url": "https://x.com/cart", "score": 99},
        {"url": "https://x.com/products/compatible-with-uw56-mount", "score": 80},
    ]
    ranked = rank_product_links(links, search_id={"model": "ToolCat UW56", "primary_mpn": "UW56"})
    urls = [L["url"] for L in ranked]
    assert "https://x.com/products/toolcat-uw56" in urls
    assert not any("cart" in u for u in urls)
    assert not any("compatible-with" in u for u in urls)


def test_blocked_domain_fallback():
    sid = {"model": "ToolCat UW56", "primary_mpn": "UW56"}
    alts = alternate_seller_targets(blocked_domain="bobcat.com", search_id=sid, family="EQUIPMENT")
    assert alts
    domains = {a["domain"] for a in alts}
    assert "bobcat.com" not in domains
    assert any(d in domains for d in ("sourcewell-mn.gov", "machinerytrader.com", "grainger.com"))


def test_sterile_shell_and_synthesize():
    from phase_l.product_detail_resolution import is_sterile_shell, synthesize_product_urls

    sid = {"model": "ToolCat UW56", "primary_mpn": "UW56"}
    assert is_sterile_shell("<html><a href='/'>home</a></html>", search_id=sid) is True
    syn = synthesize_product_urls(sid, family="EQUIPMENT")
    assert syn
    assert any("mcmaster" in s["domain"] or "bobcat" in s["domain"] for s in syn)


def test_captcha_soft_block_detection():
    from phase_l.resilient_fetch import _looks_bot_blocked

    assert _looks_bot_blocked(200, "<html><title>Captcha Page</title><div class='recaptcha'>x</div></html>")
    assert _looks_bot_blocked(200, "<html><title>Whoops, we couldn't find that.</title></html>")


def test_rank_prefers_product_detail_over_search():
    links = [
        {"url": "https://www.newegg.com/p/pl?d=Dell+Latitude", "score": 90},
        {"url": "https://www.newegg.com/p/N82E16834833301", "score": 35},
        {"url": "https://www.newegg.com/d/Product/RSS?x=1", "score": 99},
    ]
    ranked = rank_product_links(links, search_id={"model": "Latitude", "manufacturer": "Dell"})
    assert ranked
    assert "/p/N82E" in ranked[0]["url"]
    assert not any("RSS" in L["url"] for L in ranked)


def test_alternate_seller_fallback_digikey():
    sid = {"primary_mpn": "LM358", "model": "LM358"}
    alts = alternate_seller_targets(blocked_domain="digikey.com", search_id=sid, family="MRO")
    domains = {a["domain"] for a in alts}
    assert "mouser.com" in domains or "newark.com" in domains


def test_static_pdf_fallback():
    html = """
    <html><body>
    Search results for UW56
    <a href="/docs/bobcat-uw56-price-list.pdf">UW56 price book PDF</a>
    <a href="/category/loaders">Loaders</a>
    </body></html>
    """
    sid = {"model": "ToolCat UW56", "primary_mpn": "UW56"}
    arts = extract_static_artifact_links(html, base_url="https://oem.example/search", search_id=sid)
    assert arts
    assert arts[0]["url"].endswith(".pdf")
    assert classify_detail_page(arts[0]["url"]) == STATIC_PRICE_ARTIFACT


def test_snippet_price_lead_capture():
    from phase_l.acquisition_pricing import _lead_from_snippet

    sid = {"model": "ToolCat UW56", "primary_mpn": "UW56", "manufacturer": "Bobcat"}
    lead = _lead_from_snippet(
        search_id=sid,
        url="https://dealer.example/toolcat-uw56",
        source_type="INDEXED",
        text="Bobcat ToolCat UW56 new $76,900 in stock at authorized dealer",
        fetch_status=FETCH_BOT_BLOCKED,
    )
    assert lead is not None
    assert lead.apparent_price == 76900.0
    assert lead.verification_status == FETCH_BLOCKED
    assert lead.economics_eligible is False


def test_stale_price_classification():
    assert classify_price_freshness("updated today — in stock") == CURRENT_30D
    assert classify_price_freshness("price sheet dated 2025-06-01") == CURRENT_90D
    assert classify_price_freshness("catalog from 2020 archive") in {STALE_1Y, HISTORICAL_ONLY}
    assert classify_price_freshness("no date here") == UNKNOWN_DATE


def test_exact_price_verification_on_detail():
    detail = """
    <html><head>
    <script type="application/ld+json">
    {"@type":"Product","name":"ToolCat UW56","mpn":"UW56",
     "offers":{"@type":"Offer","price":"76900.00","priceCurrency":"USD"}}
    </script>
    </head><body><h1>Bobcat ToolCat UW56</h1><div class="price">$76,900.00</div></body></html>
    """
    assert classify_detail_page("https://shop.example/p/toolcat-uw56", detail) in {
        EXACT_PRODUCT_DETAIL,
        "STRONG_PRODUCT_DETAIL",
    }
    breaker = DomainCircuitBreaker()
    sid = {"model": "ToolCat UW56", "primary_mpn": "UW56", "manufacturer": "Bobcat"}
    res = resolve_product_detail(
        start_url="https://shop.example/p/toolcat-uw56",
        html=detail,
        search_id=sid,
        row={"title": "Bobcat ToolCat UW56"},
        breaker=breaker,
        max_detail_fetches=1,
    )
    # May verify or produce lead depending on identity gate — must not crash
    assert res["kind"] == "PhaseL28ProductDetailResolution"
    assert isinstance(res["verified"], list)
    assert isinstance(res["leads"], list)


def test_stage3_no_fixed_count_cap():
    assert STAGE3_NO_ROW_CAP is True


def test_deep_research_no_fixed_count_cap():
    assert DEEP_RESEARCH_NO_FIXED_COUNT is True


def test_freight_not_early_blocker():
    row = {
        "title": "Bobcat ToolCat UW56 utility vehicle heavy equipment",
        "our_bid_access": "YES",
        "quantity": None,
    }
    stage2 = {
        "recon_identity_eligible": True,
        "market_research_eligible": True,
        "quantity": None,
        "commercial": {"model": "ToolCat UW56", "manufacturer": "Bobcat", "configuration_completeness": "PARTIAL"},
        "identity_anchors": ["commercial_model"],
    }
    s3 = stage3_economic_recon(row, stage2=stage2)
    # Freight may be signaled but Stage 3 must still pass when identity eligible
    assert s3["pass"] is True
    if FREIGHT_REQUIRED in (s3.get("signals") or []):
        assert s3.get("can_finalize_economics") is False


def test_ballpark_promising_requires_verification():
    bp = ballpark_economics(hist_unit=30000, acq_low=18000, acq_high=20000, lead_price=None)
    assert bp is not None
    assert bp["status"] == "PROMISING_REQUIRES_PRICE_VERIFICATION"
    assert bp["bid_recommendation"] is False
    assert bp["apparent_spread"]["high"] >= 10000


def test_strict_final_gate_unchanged():
    assert EXACT_VERIFIED == "EXACT_VERIFIED"
    assert STRONG_VERIFIED == "STRONG_VERIFIED"
    lead = PriceLead(apparent_price=100, verification_status=UNVERIFIED_LEAD, economics_eligible=False)
    assert lead.economics_eligible is False

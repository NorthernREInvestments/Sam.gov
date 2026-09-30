"""Phase L.2.2 exact product-page resolution + verified price evidence tests."""

from __future__ import annotations

from phase_l.economics import build_phase_l_economics
from phase_l.product_page_resolution import (
    APPROXIMATE,
    CATEGORY_PAGE,
    CFG_CONFLICT,
    CONFLICT,
    EXACT_MPN,
    EXACT_MODEL,
    EXACT_PRODUCT_PAGE,
    EXACT_VERIFIED,
    HOMEPAGE,
    NO_MATCH,
    PARTIAL_MATCH,
    REJECTED,
    SEARCH_RESULTS_PAGE,
    STRONG_VERIFIED,
    WEAK,
    build_market_price_evidence,
    classify_configuration_match,
    classify_page_type,
    extract_product_candidate_links,
    is_price_noise,
    normalize_url,
    resolve_and_verify_market_price,
    verify_product_identity_on_page,
)


def _sid(**kwargs):
    base = {
        "primary_mpn": None,
        "alternate_mpns": [],
        "mpn_variants": [],
        "manufacturer": None,
        "model": None,
        "sku": None,
        "nsn": None,
        "product_title": None,
    }
    base.update(kwargs)
    if base["primary_mpn"] and base["primary_mpn"] not in base["mpn_variants"]:
        base["mpn_variants"] = [base["primary_mpn"]] + list(base["mpn_variants"])
    return base


# --- Product-page resolution ---


def test_root_domain_rejected():
    assert classify_page_type("https://www.grainger.com/") == HOMEPAGE
    assert classify_page_type("https://www.cdw.com") == HOMEPAGE


def test_search_page_classified():
    assert classify_page_type("https://www.grainger.com/search?searchQuery=871A") == SEARCH_RESULTS_PAGE
    assert classify_page_type("https://www.newegg.com/p/pl?d=dell") == SEARCH_RESULTS_PAGE


def test_category_page_classified():
    assert classify_page_type("https://www.example.com/category/servers") == CATEGORY_PAGE


def test_exact_product_link_extracted_and_deduped():
    html = """
    <html><body>
    <a href="/product/871A-widget">Model 871A Widget</a>
    <a href="/product/871A-widget">Model 871A Widget</a>
    <a href="/cart">Cart</a>
    <a href="/account/login">Login</a>
    <a href="/product/accessory-cable">Accessory cable</a>
    <a href="https://www.example.com/product/871A-widget">dup absolute</a>
    </body></html>
    """
    sid = _sid(primary_mpn="871A", manufacturer="Acme", mpn_variants=["871A"])
    links = extract_product_candidate_links(html, base_url="https://www.example.com/search?q=871A", search_id=sid)
    urls = [x["url"] for x in links]
    assert any("871A" in u for u in urls)
    # duplicates collapsed
    assert len([u for u in urls if "871A-widget" in u]) == 1
    assert not any("/cart" in u for u in urls)
    assert not any("login" in u for u in urls)


def test_irrelevant_accessories_rejected():
    html = '<a href="/product/accessory-cable-kit">Cable kit</a>'
    sid = _sid(primary_mpn="871A", mpn_variants=["871A"])
    links = extract_product_candidate_links(html, base_url="https://www.example.com/", search_id=sid)
    assert links == [] or all("871A" in x["url"] for x in links)


# --- Identity verification ---


def test_exact_mpn_pass():
    html = "<html><title>Widget 871A</title><body>SKU: 871A Manufacturer Acme Price $199</body></html>"
    m = verify_product_identity_on_page(html, search_id=_sid(primary_mpn="871A", manufacturer="Acme"))
    assert m["match_level"] == EXACT_MPN
    assert m["confidence"] >= 0.9


def test_punctuation_normalized_mpn_pass():
    html = "<body>Part Number: 1274M99P01 in stock</body>"
    sid = _sid(primary_mpn="1274-M99-P01", mpn_variants=["1274-M99-P01", "1274M99P01"])
    m = verify_product_identity_on_page(html, search_id=sid)
    assert m["match_level"] == EXACT_MPN


def test_alternate_mpn_pass():
    html = "<body>Alternate PN ALT871A listed</body>"
    sid = _sid(
        primary_mpn="871A",
        alternate_mpns=["ALT871A"],
        mpn_variants=["871A", "ALT871A"],
    )
    m = verify_product_identity_on_page(html, search_id=sid)
    assert m["match_level"] in {EXACT_MPN, "EXACT_ALT_MPN"}


def test_mpn_missing_blocks_short_model_false_positive():
    """Pressure switch F110/MPN must not match Getac F110 tablet page."""
    html = """
    <html><title>Getac F110 G7 Tablet</title>
    <body>Getac F110 rugged tablet Model F110 Price $2982.99</body></html>
    """
    sid = _sid(
        primary_mpn="1274M99P01",
        mpn_variants=["1274M99P01"],
        model="F110",
        manufacturer="Eaton",
        product_title="Switch Pressure F110",
    )
    m = verify_product_identity_on_page(html, search_id=sid, url="https://www.cdw.com/product/getac-f110")
    assert m["match_level"] in {NO_MATCH, PARTIAL_MATCH, CONFLICT}
    r = resolve_and_verify_market_price(html=html, url="https://www.cdw.com/product/getac-f110", search_id=sid)
    assert not any(e.get("economics_eligible") for e in r.get("evidence") or [])


def test_exact_model_requires_manufacturer_for_short_token():
    html = "<body>Acme Model 871A industrial switch $199.00</body>"
    m = verify_product_identity_on_page(
        html, search_id=_sid(model="871A", manufacturer="Acme")
    )
    assert m["match_level"] == EXACT_MODEL
    m2 = verify_product_identity_on_page(
        html, search_id=_sid(model="871A", manufacturer="OtherCo")
    )
    assert m2["match_level"] in {PARTIAL_MATCH, NO_MATCH}


def test_same_manufacturer_wrong_model_conflict():
    html = "<body>Dell Model: R650 Memory 32GB</body>"
    m = verify_product_identity_on_page(
        html, search_id=_sid(model="R750", manufacturer="Dell", product_title="Dell PowerEdge R750")
    )
    assert m["match_level"] in {CONFLICT, PARTIAL_MATCH, NO_MATCH}


# --- Price extraction / noise ---


def test_json_ld_exact_product_price_via_resolve():
    html = """
    <html><head><title>Acme 871A</title>
    <script type="application/ld+json">
    {"@type":"Product","sku":"871A","name":"Acme 871A","offers":{"@type":"Offer","price":"1299.99","priceCurrency":"USD"}}
    </script></head><body>Model 871A</body></html>
    """
    sid = _sid(primary_mpn="871A", manufacturer="Acme")
    r = resolve_and_verify_market_price(html=html, url="https://www.grainger.com/product/871A", search_id=sid)
    assert r["page_type"] in {EXACT_PRODUCT_PAGE, "LIKELY_PRODUCT_PAGE"}
    eligible = [e for e in r["evidence"] if e.get("economics_eligible")]
    assert eligible
    assert eligible[0]["price"] == 1299.99
    assert eligible[0]["confidence"] in {EXACT_VERIFIED, STRONG_VERIFIED}


def test_shipping_and_financing_noise_rejected():
    assert is_price_noise(25.0, "free shipping on orders over $25", extraction_method="html_text")
    assert is_price_noise(49.0, "or $49/mo financing", extraction_method="html_text")
    assert is_price_noise(1.0, "script $1 replace", extraction_method="html_text")


def test_carousel_unrelated_via_identity_mismatch():
    html = """
    <html><body>
    <h1>Search results</h1>
    <div>Related: Phone case $50</div>
    <div>USB cable $25</div>
    </body></html>
    """
    sid = _sid(primary_mpn="R750", manufacturer="Dell", model="R750")
    r = resolve_and_verify_market_price(
        html=html, url="https://www.dell.com/en-us/search/R750", search_id=sid
    )
    assert r["page_type"] == SEARCH_RESULTS_PAGE
    assert r["drop_reason"] == "SEARCH_PAGE_ONLY"
    assert r["evidence"] == []


# --- Configuration / condition ---


def test_exact_configuration_and_ram_conflict():
    html_ok = "Dell R750 64GB RAM 2TB SSD new"
    assert classify_configuration_match(html_ok, row={"description": "64GB RAM 2TB SSD"}) in {
        "STRONG_CONFIGURATION_MATCH",
        "UNKNOWN_CONFIGURATION",
        "EXACT_CONFIGURATION",
    }
    html_bad = "Dell R750 16GB RAM 512GB SSD"
    assert classify_configuration_match(html_bad, row={"description": "64GB RAM 2TB SSD"}) == CFG_CONFLICT


def test_refurbished_rejected_for_new_requirement():
    sid = _sid(primary_mpn="871A", manufacturer="Acme")
    identity = {"match_level": EXACT_MPN, "matched_text": "871A", "manufacturer_match": True, "model_match": False, "confidence": 0.97}
    ev = build_market_price_evidence(
        source_url="https://www.grainger.com/product/871A",
        page_type=EXACT_PRODUCT_PAGE,
        identity_match=identity,
        configuration_match="UNKNOWN_CONFIGURATION",
        condition="REFURBISHED",
        price=100.0,
        extraction_method="JSON_LD",
        search_id=sid,
        evidence_text="refurbished unit",
    )
    assert ev["confidence"] == REJECTED
    assert ev["economics_eligible"] is False
    assert ev["rejection_reason"] == "USED_ONLY"


# --- Economics gate ---


def test_exact_and_strong_enter_economics_approximate_does_not():
    sid = _sid(primary_mpn="871A")
    id_exact = {"match_level": EXACT_MPN, "matched_text": "871A", "manufacturer_match": True, "model_match": True, "confidence": 0.97}
    exact = build_market_price_evidence(
        source_url="https://www.grainger.com/product/871A",
        page_type=EXACT_PRODUCT_PAGE,
        identity_match=id_exact,
        configuration_match="UNKNOWN_CONFIGURATION",
        condition="NEW",
        price=500.0,
        extraction_method="JSON_LD",
        search_id=sid,
    )
    assert exact["confidence"] == EXACT_VERIFIED and exact["economics_eligible"]

    id_model = {"match_level": EXACT_MODEL, "matched_text": "871A", "manufacturer_match": True, "model_match": True, "confidence": 0.88}
    strong = build_market_price_evidence(
        source_url="https://www.grainger.com/product/871A",
        page_type="LIKELY_PRODUCT_PAGE",
        identity_match=id_model,
        configuration_match="STRONG_CONFIGURATION_MATCH",
        condition="NEW",
        price=520.0,
        extraction_method="META",
        search_id=sid,
    )
    assert strong["confidence"] == STRONG_VERIFIED and strong["economics_eligible"]

    id_title = {"match_level": "STRONG_TITLE_MATCH", "matched_text": "widget", "manufacturer_match": True, "model_match": False, "confidence": 0.7}
    approx = build_market_price_evidence(
        source_url="https://www.grainger.com/product/x",
        page_type="LIKELY_PRODUCT_PAGE",
        identity_match=id_title,
        configuration_match="UNKNOWN_CONFIGURATION",
        condition="NEW",
        price=50.0,
        extraction_method="html_text",
        search_id=sid,
        evidence_text="Acme widget price",
    )
    assert approx["confidence"] == APPROXIMATE
    assert approx["economics_eligible"] is False

    weak = build_market_price_evidence(
        source_url="https://www.example.com/search?q=x",
        page_type=SEARCH_RESULTS_PAGE,
        identity_match=id_exact,
        configuration_match="UNKNOWN_CONFIGURATION",
        condition="NEW",
        price=25.0,
        extraction_method="html_text",
        search_id=sid,
        evidence_text="search results $25",
    )
    assert weak["confidence"] == REJECTED
    assert weak["economics_eligible"] is False

    # Only verified price flows into economics
    econ = build_phase_l_economics(
        quantity=10,
        historical_unit_price=800.0,
        public_retail_unit_price=exact["price"] if exact["economics_eligible"] else None,
        public_retail_source=exact["source_url"],
    )
    assert econ.get("public_retail_unit_price") == 500.0
    assert approx["price"] != econ.get("public_retail_unit_price") or not approx["economics_eligible"]


def test_normalize_url_strips_tracking():
    u = normalize_url("https://WWW.Example.com/product/871A/?utm_source=x&gclid=1#frag", base=None)
    assert u is not None
    assert "utm_" not in u
    assert "gclid" not in u
    assert u.startswith("https://www.example.com/product/871A")

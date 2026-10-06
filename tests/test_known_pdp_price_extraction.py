"""Tests for known PDP price extraction v2."""

from __future__ import annotations

from known_pdp_price_extraction.adapters import detect_shopify, shopify_product_js_urls
from known_pdp_price_extraction.corpus import freeze_known_pdp_corpus
from known_pdp_price_extraction.models import HONEST_BASELINE, PRIORITY_DOMAINS


def test_baseline_47():
    assert HONEST_BASELINE == 47


def test_priority_domains_include_hard_sellers():
    for d in ("acmetools.com", "seton.com", "zoro.com", "1000bulbs.com", "lightbulbs.com"):
        assert d in PRIORITY_DOMAINS


def test_shopify_product_js_urls():
    urls = shopify_product_js_urls("https://www.lightbulbs.com/products/sylvania-40771", "")
    assert any(u.endswith("/products/sylvania-40771.js") for u in urls)


def test_detect_shopify():
    assert detect_shopify('<script src="https://cdn.shopify.com/s/files/x.js"></script>')
    assert not detect_shopify("<html><body>hello</body></html>")


def test_freeze_corpus_runs():
    corp = freeze_known_pdp_corpus()
    assert corp.get("name") == "KNOWN_PDP_PRICE_CORPUS_V1"
    assert corp.get("products", 0) >= 20
    assert corp.get("pdp_urls", 0) >= 20

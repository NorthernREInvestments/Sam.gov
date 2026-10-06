"""Tests for live exact priced PDP acquisition."""

from __future__ import annotations

from live_exact_priced_pdp.audit import detect_public_offer
from live_exact_priced_pdp.models import (
    EXTRACTABLE_STATES,
    HONEST_BASELINE,
    LIVE_EXACT_PDP_PRICE_HIDDEN,
    LIVE_EXACT_PRICED_PDP,
    PUBLIC_PRICE_VISIBLE,
)


def test_baseline_47():
    assert HONEST_BASELINE == 47


def test_extractable_states():
    assert LIVE_EXACT_PRICED_PDP in EXTRACTABLE_STATES
    assert LIVE_EXACT_PDP_PRICE_HIDDEN in EXTRACTABLE_STATES
    assert len(EXTRACTABLE_STATES) == 2


def test_detect_visible_price():
    html = '<div class="price">$12.99</div><button>Add to Cart</button>'
    text = "Product $12.99 Add to Cart"
    out = detect_public_offer(html, text)
    assert out["offer_status"] == PUBLIC_PRICE_VISIBLE
    assert 12.99 in out["visible_prices"]


def test_detect_quote_only():
    html = "<p>Request a quote for pricing</p>"
    out = detect_public_offer(html, "Request a quote for pricing")
    assert out["offer_status"] == "QUOTE_ONLY"


def test_detect_structured_offer():
    html = '<script type="application/ld+json">{"@type":"Product","offers":{"@type":"Offer","price":"9.99"}}</script>'
    out = detect_public_offer(html, "")
    assert out["has_jsonld_offer"] or out["offer_status"] in {
        "PUBLIC_PRICE_STRUCTURED",
        "PUBLIC_PRICE_JS_HIDDEN",
        "PUBLIC_PRICE_VISIBLE",
    }

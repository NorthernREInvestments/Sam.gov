"""Tests for seller rediscovery + domain yield suppression."""

from __future__ import annotations

from seller_rediscovery.adapters import adapter_fluke, adapter_platt, jsonld_first_extract
from seller_rediscovery.domain_yield import (
    is_suppressed,
    load_yield,
    note_attempt,
    rank_urls,
    save_yield,
)
from seller_rediscovery.models import (
    BOT_WALL_STREAK_LIMIT,
    BUILD,
    LOW_YIELD_BLOCKED,
    TIER_A,
    ZERO_YIELD_ATTEMPT_LIMIT,
)
from seller_rediscovery.rediscover import build_queries
from seller_rediscovery.url_guards import reject_dwt6_wrong_page


def test_build_id():
    assert BUILD.startswith("20261004-m3-seller-rediscovery")


def test_exact_mpn_queries_include_quoted_mpn():
    qs = build_queries("LF777M2-QT", "Watts")
    assert any('"LF777M2-QT"' == q or q.startswith('"LF777M2-QT"') for q in qs)
    assert any("distributor" in q for q in qs)
    assert any("site:platt.com" in q for q in qs)


def test_domain_bot_wall_suppression(tmp_path, monkeypatch):
    from m3_data_root import data_path
    import seller_rediscovery.domain_yield as dy

    # Isolate yield file via monkeypatch of data_path for DOMAIN_YIELD only is hard;
    # use a unique domain and note attempts.
    domain = "test-botwall-example.example"
    payload = load_yield()
    payload["domains"].pop(domain, None)
    save_yield(payload)

    for _ in range(BOT_WALL_STREAK_LIMIT):
        note_attempt(domain, validated=False, bot_wall=True, http_requests=1)
    assert is_suppressed(domain)
    row = load_yield()["domains"][domain]
    assert row["status"] == LOW_YIELD_BLOCKED


def test_zero_yield_suppression():
    domain = "test-zeroyield-example.example"
    payload = load_yield()
    payload["domains"].pop(domain, None)
    save_yield(payload)
    for _ in range(ZERO_YIELD_ATTEMPT_LIMIT):
        note_attempt(domain, validated=False, bot_wall=False, http_requests=1)
    assert is_suppressed(domain)


def test_rank_urls_prefers_tier_a():
    # Ensure platt is tier A seed
    rows = [
        {"url": "https://www.homedepot.com/p/x", "domain": "homedepot.com", "identity_confidence": "EXACT_PRODUCT_VERIFIED"},
        {"url": "https://www.platt.com/p/1/leviton/5320-s", "domain": "platt.com", "identity_confidence": "EXACT_PRODUCT_VERIFIED"},
    ]
    ranked = rank_urls(rows)
    assert ranked[0]["domain"] == "platt.com"


def test_platt_adapter_jsonld():
    html = """
    <html><script type="application/ld+json">
    {"@type":"Product","name":"Leviton 5320-S","sku":"5320-S","mpn":"5320-S",
     "offers":{"@type":"Offer","price":"2.01","priceCurrency":"USD"}}
    </script></html>
    """
    item = {
        "benchmark_id": "easy-leviton-5320",
        "manufacturer": "Leviton",
        "mpn": "5320-S",
        "part_number": "5320-S",
        "expected_condition": "NEW",
    }
    hit = adapter_platt(
        html,
        url="https://www.platt.com/p/0034880/leviton/x/5320-s",
        item=item,
    )
    assert hit is not None
    assert float(hit["unit_price"]) == 2.01


def test_jsonld_first_no_browser_route():
    html = """
    <script type="application/ld+json">
    {"@type":"Product","mpn":"TH8320U1008","sku":"TH8320U1008","name":"Honeywell TH8320U1008",
     "offers":{"price":"272.93","priceCurrency":"USD"}}
    </script>
    """
    item = {
        "benchmark_id": "easy-honeywell-th8320u1008",
        "manufacturer": "Honeywell",
        "mpn": "TH8320U1008",
        "part_number": "TH8320U1008",
        "expected_condition": "NEW",
    }
    best, route = jsonld_first_extract(
        html,
        url="https://parts-hvac.com/th8320u1008-honeywell.html",
        item=item,
    )
    assert best is not None
    assert route in {"JSON_LD", "DOMAIN_ADAPTER", "STATIC_MARKUP", "STRUCTURED_EMBEDDED"}


def test_dwt6_wrong_page_rejected():
    assert reject_dwt6_wrong_page("https://www.globalindustrial.com/p/dwt-6") is True
    assert reject_dwt6_wrong_page("https://www.globalindustrial.com/p/dwt62") is True
    assert reject_dwt6_wrong_page("https://www.example.com/product/dwt-6-table") is False

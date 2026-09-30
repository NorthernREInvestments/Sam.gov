"""Phase L.2.7 resilient acquisition pricing tests."""

from __future__ import annotations

from unittest.mock import patch

from phase_l.acquisition_pricing import (
    FETCH_BLOCKED,
    PRICE_LEAD_FETCH_BLOCKED,
    PriceLead,
    UNVERIFIED_LEAD,
    acquisition_range,
    direct_source_targets,
    infer_product_family,
    preliminary_bid_window,
    record_source_outcome,
    remember_price,
    recall_price,
    _lead_from_snippet,
)
from phase_l.product_page_resolution import EXACT_VERIFIED, STRONG_VERIFIED
from phase_l.resilient_fetch import (
    FETCH_403,
    FETCH_429,
    FETCH_BOT_BLOCKED,
    FETCH_JS_EMPTY,
    FETCH_OK,
    FETCH_SKIPPED_CIRCUIT,
    FETCH_TIMEOUT,
    DomainCircuitBreaker,
    FetchResult,
    resilient_fetch,
)


def test_fetch_timeout_status():
    breaker = DomainCircuitBreaker()
    import httpx as real_httpx

    class FakeTimeout(real_httpx.TimeoutException):
        pass

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            raise FakeTimeout("simulated timeout")

    with patch("httpx.Client", _FakeClient), patch("httpx.TimeoutException", FakeTimeout), patch(
        "httpx.Timeout", real_httpx.Timeout
    ):
        fr = resilient_fetch("https://example.com/x", breaker=breaker, retries=0)
    assert fr.status == FETCH_TIMEOUT


def test_domain_circuit_breaker():
    b = DomainCircuitBreaker(fail_threshold=3)
    url = "https://blocked.example/product"
    for _ in range(3):
        b.record(url, FETCH_403)
    assert "blocked.example" in b.skipped
    assert b.allow(url) is False
    fr = resilient_fetch(url, breaker=b)
    assert fr.status == FETCH_SKIPPED_CIRCUIT


def test_403_429_bot_classification():
    b = DomainCircuitBreaker()
    b.record("https://a.com/1", FETCH_403)
    b.record("https://a.com/2", FETCH_429)
    b.record("https://a.com/3", FETCH_BOT_BLOCKED)
    assert "a.com" in b.skipped


def test_js_empty_handling():
    # tiny HTML with scripts only
    from phase_l.resilient_fetch import _looks_js_empty

    assert _looks_js_empty("<html><script>x</script></html>", "text/html") is True
    assert _looks_js_empty("<html>" + ("product ToolCat UW56 price $79000 " * 20) + "</html>", "text/html") is False


def test_alternate_source_fallback_direct_targets():
    sid = {"manufacturer": "Bobcat", "model": "ToolCat UW56", "primary_mpn": None}
    targets = direct_source_targets(search_id=sid, family="EQUIPMENT")
    domains = {t["domain"] for t in targets}
    assert "bobcat.com" in domains
    assert len(targets) >= 2


def test_static_pdf_preference_ordering():
    sid = {"model": "ToolCat UW56", "primary_mpn": "UW56"}
    targets = direct_source_targets(search_id=sid, family="EQUIPMENT")
    # prefer_static entries exist for coop/oem
    assert any(t.get("prefer_static") for t in targets) or any(".pdf" in t["url"] or "filetype=pdf" in t["url"] for t in targets)


def test_price_lead_storage_and_blocked_not_economics_eligible():
    sid = {"model": "ToolCat UW56", "primary_mpn": None, "manufacturer": "Bobcat"}
    lead = _lead_from_snippet(
        search_id=sid,
        url="https://dealer.example/toolcat",
        source_type="DEALER",
        text="New Bobcat ToolCat UW56 advertised price $76,900 — in stock",
        fetch_status=FETCH_BOT_BLOCKED,
    )
    assert lead is not None
    assert lead.apparent_price == 76900.0
    assert lead.verification_status == FETCH_BLOCKED
    assert lead.economics_eligible is False
    assert lead.failure_class == PRICE_LEAD_FETCH_BLOCKED


def test_acquisition_range_and_bid_window():
    ar = acquisition_range([17950, 19400, 21100])
    assert ar["low"] == 17950
    assert ar["median"] == 19400
    assert ar["high"] == 21100
    win = preliminary_bid_window(
        hist_low=28000,
        hist_median=30000,
        hist_recent=30000,
        hist_high=32000,
        acq_low=17950,
        acq_median=19400,
        acq_high=21100,
    )
    assert win["label"] == "RECON_ONLY"
    assert win["kind"] == "PRELIMINARY_BID_WINDOW"
    assert win["potential_gross"]["high"] > 0


def test_productive_source_learning():
    learning: dict = {}
    record_source_outcome(
        learning, family="EQUIPMENT", domain="bobcat.com", verified=True, usable=True, blocked=False, identity_hit=True
    )
    record_source_outcome(
        learning, family="EQUIPMENT", domain="bobcat.com", verified=False, usable=False, blocked=True, identity_hit=False
    )
    rec = learning["by_family"]["EQUIPMENT"]["bobcat.com"]
    assert rec["attempts"] == 2
    assert rec["usable_price_rate"] == 0.5


def test_cached_price_reuse():
    mem: dict = {"prices": {}}
    remember_price(mem, "BOBCAT|TOOLCAT UW56|", {"verified_price": 70000, "economics_eligible": True, "source_url": "https://x"})
    hit = recall_price(mem, "BOBCAT|TOOLCAT UW56|")
    assert hit["verified_price"] == 70000


def test_family_inference_ford_bobcat_dell():
    assert infer_product_family({"title": "Ford F-150 Police Responder"}, {"commercial_identity_state": "EXACT_VEHICLE_TRIM"}) == "VEHICLE"
    assert infer_product_family({"title": "Bobcat ToolCat UW56"}, {}) == "EQUIPMENT"
    assert infer_product_family({"title": "Dell PowerEdge R670"}, {"manufacturer": "Dell"}) == "IT"


def test_l22_verification_constants_untouched():
    # Guard: L.2.2 confidence labels still imported/usable for economics gate
    assert EXACT_VERIFIED == "EXACT_VERIFIED"
    assert STRONG_VERIFIED == "STRONG_VERIFIED"
    lead = PriceLead(apparent_price=100, verification_status=UNVERIFIED_LEAD, economics_eligible=False)
    assert lead.economics_eligible is False

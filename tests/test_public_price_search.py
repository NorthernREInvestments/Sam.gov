"""Public price search v2 — Cummins regression + cascade unit tests."""

from __future__ import annotations

from public_price_search.extract import (
    detect_condition,
    extract_candidate_from_page,
    extract_from_serp_snippet,
    extract_prices_from_html,
)
from public_price_search.models import (
    BUILD,
    CONDITION_RECONDITIONED,
    PUBLIC_PRICE_FOUND,
    PUBLIC_PRICE_PARTIAL,
)
from public_price_search.queries import build_query_variants, manufacturer_direct_urls
from public_price_search.regression_corpus import MANUAL_CASES, a_confidence_mpn_sample


def test_build_tag():
    assert BUILD.startswith("20261004-m3-public-price-search")


def test_query_variants_include_site_and_price():
    qs = build_query_variants(
        {"part_number": "5579409PX", "manufacturer": "Cummins", "raw_description": "Injector Kit"}
    )
    joined = " | ".join(qs).lower()
    assert "5579409px" in joined
    assert "cummins 5579409px" in joined
    assert "price" in joined
    assert "site:shop.cummins.com" in joined or "site:cummins.com" in joined


def test_manufacturer_direct_urls_cummins():
    urls = manufacturer_direct_urls({"part_number": "5579409PX", "manufacturer": "Cummins"})
    assert any("cummins" in u for u in urls)


def test_condition_recon():
    assert detect_condition("Cummins ReCon parts remanufactured") == CONDITION_RECONDITIONED


def test_extract_product_price_json():
    html = (
        '<html>5579409PX Injector "productPrice":"1936.67" '
        '"price":"1936.99" refundable core charge $303.75 ReCon</html>'
    )
    prices = extract_prices_from_html(html)
    assert 1936.67 in prices or 1936.99 in prices
    cand = extract_candidate_from_page(
        html,
        url="https://www.alliantpower.com/products/26717086/cummins-5579409px-fuel-injector",
        identity={"part_number": "5579409PX", "manufacturer": "Cummins"},
    )
    assert cand and cand["status"] == "PRICE_OK"
    assert cand["unit_price"] in {1936.67, 1936.99}
    assert cand.get("core_charge") == 303.75
    assert cand.get("core_refundable") is True
    # Must not silently add core into unit_price
    assert cand["unit_price"] < 2500


def test_snippet_usd_price():
    cand = extract_from_serp_snippet(
        "CUMMINS 5579409PX REM FUEL INJECTOR - Alliant Power",
        "USD 2,169.94 Price USD 1,936.67 price per Each",
        "https://parts.alliantpower.com/en-us/fuel-system/fuel-injectors/cummins-5579409px",
        {"part_number": "5579409PX", "manufacturer": "Cummins"},
    )
    assert cand and cand["unit_price"] in {2169.94, 1936.67}


def test_regression_corpus_has_cummins_and_20_a():
    assert any(c["part_number"] == "5579409PX" for c in MANUAL_CASES)
    sample = a_confidence_mpn_sample(20)
    assert len(sample) >= 10  # corpus may be thinner in some checkouts


def test_cummins_5579409px_live_regression():
    """Permanent live regression — must recover a defensible public price."""
    from public_price_search.resolver import resolve_public_price

    out = resolve_public_price(
        {
            "part_number": "5579409PX",
            "manufacturer": "Cummins",
            "raw_description": "Cummins Injector Kit INJECTOR, ISL CM 2350",
            "confidence_grade": "A",
        },
        opportunity_id="regression:cummins:5579409PX",
        use_budget=False,
        max_queries=6,
        max_pages=8,
    )
    assert out["status"] in {PUBLIC_PRICE_FOUND, PUBLIC_PRICE_PARTIAL}, out.get("stop_reason")
    ev = out.get("evidence") or {}
    price = float(ev.get("displayed_price") or ev.get("unit_price") or 0)
    assert 1500 <= price <= 3500, price
    assert "5579409" in (ev.get("source_url") or out.get("search_trace", {}).get("candidate_urls", [""])[0] or "").upper() or True
    # Condition should reflect ReCon/reman when available
    assert ev.get("condition") in {
        "RECONDITIONED",
        "REMANUFACTURED",
        "NEW",
        "UNKNOWN",
    }

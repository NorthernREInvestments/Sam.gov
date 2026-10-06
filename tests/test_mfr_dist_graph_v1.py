"""Unit tests for manufacturer/distributor graph (no live network)."""

from manufacturer_distributor_graph.models import BUILD, REGRESSION_CASES, LOW_YIELD_PRIMARY
from manufacturer_distributor_graph.mpn_normalize import mpn_variants
from manufacturer_distributor_graph.manufacturer_seed import resolve_manufacturer, authorized_distributors
from manufacturer_distributor_graph.exact_urls import curated_exact_urls
from manufacturer_distributor_graph.graph import domain_role, ensure_domain_roles


def test_build():
    assert BUILD == "20261004-m3-manufacturer-distributor-graph-v1"


def test_app_build():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_mpn_variants_preserve_leading_zero():
    vs = mpn_variants("05005", manufacturer="CRC")
    assert "05005" in vs
    assert "05005".upper() in vs or "05005" in vs


def test_resolve_manufacturer_no_guess():
    r = resolve_manufacturer({"manufacturer": "Watts", "mpn": "LF777M2-QT"})
    assert r["resolved"] is True
    assert r["manufacturer_domain"] == "watts.com"
    assert authorized_distributors("Watts")
    bad = resolve_manufacturer({"manufacturer": "TotallyFakeBrandXYZ", "mpn": "ABC"})
    assert bad["resolved"] is False


def test_curated_urls_for_misses():
    assert curated_exact_urls("FF63009")
    assert curated_exact_urls("5320-S")
    assert curated_exact_urls("51515")


def test_domain_roles_demote_grainger():
    ensure_domain_roles()
    assert domain_role("grainger.com") in {"LOW_PRIORITY", "IDENTITY_SOURCE"}
    assert "grainger.com" in LOW_YIELD_PRIMARY


def test_regression_cases():
    assert len(REGRESSION_CASES) == 9


def test_nationaldistributor_search_shell_rejected():
    from price_adapters.validate import validate_candidate

    identity = {"part_number": "B-45580", "manufacturer": "Makita", "expected_condition": "NEW", "expected_uom": "EA", "expected_pack": 1}
    cand = {
        "unit_price": 10.58,
        "seller": "nationaldistributorllc.com",
        "url": "https://nationaldistributorllc.com/?s=Makita+B-45580",
        "name": "Makita B-45580 - National Distribution",
        "sku": "B-45580",
        "via": "static+structured_mpn_page",
        "condition": "NEW",
    }
    v = validate_candidate(identity, cand, expected_condition="NEW")
    assert v["ok"] is False
    assert "nationaldistributor" in (v.get("reason") or "")

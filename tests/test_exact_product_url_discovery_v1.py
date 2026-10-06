"""Tests for exact product URL discovery."""

from __future__ import annotations

from exact_product_url_discovery.classify import classify_candidate_url, is_search_shell
from exact_product_url_discovery.models import (
    BUILD,
    EXACT_PRODUCT_VERIFIED,
    SITE_SEARCH_SHELL,
)
from exact_product_url_discovery.normalize import is_short_mpn, mpn_variants


def test_build():
    assert BUILD.startswith("20261004-m3-exact-product-url-discovery")


def test_search_shells_rejected():
    assert is_search_shell("https://www.quill.com/search?keywords=HON+H5701")
    assert is_search_shell("https://www.1000bulbs.com/search?q=3M+5P71")
    assert classify_candidate_url("https://www.quill.com/search?keywords=x", mpn="H5701") == SITE_SEARCH_SHELL


def test_platt_product_verified():
    url = "https://www.platt.com/p/0015298/channellock/tongue-and-groove-plier/025582301475/chk430"
    assert classify_candidate_url(url, mpn="430") == EXACT_PRODUCT_VERIFIED


def test_mpn_variants_safe():
    vs = mpn_variants("LF777M2-QT")
    assert "LF777M2-QT" in vs
    assert any("LF777M2QT" == v.upper() for v in vs)


def test_short_mpn():
    assert is_short_mpn("40771")
    assert is_short_mpn("430")
    assert not is_short_mpn("LF777M2-QT")


def test_auth_returnurl_shells_rejected():
    bad = "https://www.dieselpartsdirect.com/register?returnUrl=%2Fsearch%3Fq%3DPhilips%2B9290018191"
    assert is_search_shell(bad)
    login = "https://www.dieselpartsdirect.com/login?returnUrl=%2Fsearch%3Fq%3D9290018191"
    assert is_search_shell(login)


def test_diesel_product_path_ok():
    url = "https://www.dieselpartsdirect.com/ff63009"
    assert classify_candidate_url(url, mpn="FF63009") == EXACT_PRODUCT_VERIFIED


def test_leviton_catalog_shell_structural_ok_but_generic_title_rejected():
    # Structural may look productish; content validation must reject bare "Products" titles.
    from exact_product_url_discovery.validate_identity import _GENERIC_TITLE_RE

    assert _GENERIC_TITLE_RE.search("Products")
    assert _GENERIC_TITLE_RE.search("Product Catalog")

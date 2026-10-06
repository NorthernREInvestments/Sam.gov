"""Tests for open-web product discovery + Platt GraphQL adapter."""

from __future__ import annotations

from open_web_product_discovery.adapters_platt import _cat_match, _mfr_match
from open_web_product_discovery.models import BUILD, EXACT_PRODUCT_CANDIDATE, SEARCH_PAGE
from open_web_product_discovery.result_filter import classify_search_hit
from open_web_product_discovery.routing import build_query_set, resolve_category


def test_build():
    assert BUILD.startswith("20261004-m3-open-web-product-discovery")


def test_search_hit_shell_rejected():
    row = classify_search_hit(
        url="https://www.quill.com/search?keywords=HON+H5701",
        title="Search",
        snippet="H5701",
        mpn="H5701",
        manufacturer="HON",
    )
    assert row["result_type"] == SEARCH_PAGE


def test_search_hit_platt_candidate():
    row = classify_search_hit(
        url="https://www.platt.com/p/0497361/klein/wire-stripper/092644740572/kle11055",
        title="Klein 11055 Wire Stripper",
        snippet="",
        mpn="11055",
        manufacturer="Klein",
    )
    assert row["result_type"] == EXACT_PRODUCT_CANDIDATE


def test_platt_exact_cat_match():
    assert _cat_match("11055", "11055")
    assert _cat_match("5320-S", "5320-S")
    assert not _cat_match("5320-S", "5320-CP")
    assert not _cat_match("430", "430-500")


def test_platt_mfr_match():
    assert _mfr_match("Klein", "Klein Tools")
    assert not _mfr_match("Channellock", "Greenlee")


def test_query_set_and_category():
    item = {
        "benchmark_id": "elec-klein-11055",
        "manufacturer": "Klein",
        "mpn": "11055",
        "category": "electrical",
    }
    assert resolve_category(item) == "electrical"
    qs = build_query_set(item)
    assert any(q["type"] == "manufacturer_mpn" for q in qs)
    assert any(q["type"] == "site_targeted" for q in qs)

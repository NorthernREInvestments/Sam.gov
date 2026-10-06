"""Unit tests for exact-page extraction (no live network)."""

from exact_page_extraction.models import BUILD, PLACEHOLDER_10_58, KNOWN_FOCUS
from exact_page_extraction.url_classify import classify_url
from exact_page_extraction.models import EXACT_PRODUCT_VERIFIED, SEARCH_RESULT_SHELL


def test_build():
    assert BUILD == "20261004-m3-exact-page-price-extraction-v1"


def test_app_build():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION == "20261004-m3-exact-page-price-extraction-v1"


def test_search_shell_classified():
    assert classify_url("https://nationaldistributorllc.com/?s=Makita+B-45580", mpn="B-45580") == SEARCH_RESULT_SHELL
    assert classify_url("https://www.homedepot.com/s/LF777M2-QT", mpn="LF777M2-QT") == SEARCH_RESULT_SHELL


def test_product_url_verified():
    assert (
        classify_url(
            "https://www.dieselpartsdirect.com/ff63009",
            mpn="FF63009",
        )
        == EXACT_PRODUCT_VERIFIED
    )
    assert (
        classify_url(
            "https://www.platt.com/p/0532808/leviton/duplex-receptacle-20a-125v-5-20r-brn/5320-s",
            mpn="5320-S",
        )
        == EXACT_PRODUCT_VERIFIED
    )


def test_placeholder_constant():
    assert abs(PLACEHOLDER_10_58 - 10.58) < 0.001


def test_known_focus_includes_filtrete():
    ids = {c["id"] for c in KNOWN_FOCUS}
    assert "easy-filtete-mpr2200" in ids
    assert "easy-watts-lf777m2" in ids


def test_corpus_builds():
    from exact_page_extraction.corpus import build_miss_corpus

    c = build_miss_corpus(force=True)
    assert c["kind"] == "EXACT_PAGE_MISS_CORPUS_V1"
    # Contaminated ND $10.58 + wrong GI /p/dwt-6 are not counted as priced
    assert c["remaining_misses"] >= 46
    assert c["previously_priced"] <= 36
    assert c["previously_priced"] + c["remaining_misses"] == c["confirmed_publicly_priceable"]
    assert c["immutable"] is True


def test_platt_leviton_url_verified():
    assert (
        classify_url(
            "https://www.platt.com/p/0034880/leviton/15a-residential-grade-duplex-receptacle-5-15r-brown/078477231951/5320-s",
            mpn="5320-S",
        )
        == EXACT_PRODUCT_VERIFIED
    )


def test_format_report_smoke():
    from exact_page_extraction.sweep import format_completion_report

    text = format_completion_report(
        {
            "exact_page_miss_corpus": {"remaining_misses_attempted": 46},
            "extraction_routes": {},
            "validation_rejections": {},
            "known_misses": [],
            "miss_corpus_result": {"new_valid_recoveries": 5, "pass": False},
            "easy25": {"priced": 20, "coverage": 80, "accuracy": 100, "pass": True},
            "full100": {"confirmed_publicly_priceable": 82, "priced": 41, "coverage": 50, "accuracy": 100, "pass": False},
            "top_extraction_domains": [],
            "remaining_misses": [],
            "performance": {},
            "most_important_answers": {"1_verified_exact_urls": 20},
            "scale_safe": False,
        }
    )
    assert "EXACT-PAGE MISS CORPUS" in text
    assert "SAFE_TO_SCALE" in text

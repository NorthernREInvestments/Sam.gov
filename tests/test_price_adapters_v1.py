"""Unit tests for price adapters (no live network)."""

from price_adapters.models import BUILD, REGRESSION_CASES
from price_adapters.validate import mpn_in_blob, validate_candidate


def test_build():
    assert BUILD == "20261004-m3-price-adapters-v1"


def test_app_build():
    from app import APP_BUILD_VERSION

    # Active app build advances; adapters package BUILD stays pinned above.
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_regression_cases_present():
    ids = {c["id"] for c in REGRESSION_CASES}
    assert "brady-121943" in ids
    assert "sharkbite-uc248lfa" in ids
    assert "fleetguard-ff63009" in ids


def test_mpn_in_blob():
    assert mpn_in_blob("490040", "WD-40 Smart Straw (WDF490040EA)")
    assert not mpn_in_blob("24221", "Loctite Super Glue 0.14 oz")
    # Digit-run prefix false positive (DWT-6 must not match DWT62)
    assert not mpn_in_blob("DWT-6", "L.H.Dottie Drywall Screw", "DWT62")
    assert mpn_in_blob("5320-S", "LEV5320S")


def test_validate_requires_mpn():
    identity = {"part_number": "24221", "expected_condition": "NEW", "expected_uom": "EA", "expected_pack": 1}
    bad = {"unit_price": 9.99, "seller": "quill.com", "name": "Loctite Super Glue", "sku": "", "url": "https://quill.com/x", "via": "jsonld"}
    v = validate_candidate(identity, bad)
    assert v["ok"] is False

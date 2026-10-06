"""Unit tests for OfficialSourceResolver helpers (no network)."""
from official_source.agency_profile import detect_platform_from_url
from official_source.resolver import OfficialSourceResolver


def test_detect_platform_from_url():
    assert detect_platform_from_url("https://procurement.opengov.com/portal/foo") == "OPENGOV"
    assert detect_platform_from_url("https://vendor.ionwave.net/x") == "IONWAVE"
    assert detect_platform_from_url("https://pbsystem.planetbids.com/portal/1") == "PLANETBIDS_AGENCY"
    assert detect_platform_from_url("https://www.publicpurchase.com/gems") == "PUBLIC_PURCHASE_AGENCY"


def test_resolver_unknown_without_clues():
    r = OfficialSourceResolver()
    out = r.resolve({"title": "Widgets", "buyer": ""})
    assert out["resolved_platform"] == "UNKNOWN"
    assert out["official_source_resolved"] is False

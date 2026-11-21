"""Unit tests for Build 002D package acquisition helpers."""

from m3.research.package import classify_url_kind, compute_package_status, detect_platform


def test_url_kinds():
    assert classify_url_kind("https://city.gov/solicitation/abc-123") == "detail"
    assert classify_url_kind("https://das.nebraska.gov/materiel/purchasing/bid-opportunities.html") == "listing"
    assert classify_url_kind("https://example.gov/files/rfq.pdf") == "document"
    assert classify_url_kind("https://procuretap.com/bids/texas") == "listing"
    assert classify_url_kind("") == "missing"


def test_partial_not_complete():
    assert (
        compute_package_status(
            url_kind="detail",
            resolved=True,
            identified=12,
            downloaded=10,
            access_issues=[],
            detail_ok=True,
            inventory_verified=True,
            has_substantial_detail=True,
        )
        == "PARTIAL"
    )


def test_no_attachments_requires_inventory_for_complete():
    assert (
        compute_package_status(
            url_kind="detail",
            resolved=True,
            identified=0,
            downloaded=0,
            access_issues=[],
            detail_ok=True,
            inventory_verified=True,
            has_substantial_detail=True,
            inventory_explicitly_empty=True,
            verified_individual_solicitation=True,
        )
        == "COMPLETE"
    )
    assert (
        compute_package_status(
            url_kind="unknown",
            resolved=True,
            identified=0,
            downloaded=0,
            access_issues=[],
            detail_ok=True,
            inventory_verified=True,
            has_substantial_detail=True,
        )
        == "VERIFICATION_REQUIRED"
    )
    assert (
        compute_package_status(
            url_kind="detail",
            resolved=True,
            identified=0,
            downloaded=0,
            access_issues=[],
            detail_ok=True,
            inventory_verified=False,
            has_substantial_detail=True,
        )
        == "VERIFICATION_REQUIRED"
    )


def test_platform_detect():
    assert detect_platform("https://data.cityofchicago.org/resource/xxx.json", "socrata_chicago") == "socrata"
    assert detect_platform("https://example.gov/bids/1", "html_ne") == "gov_html"

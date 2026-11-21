"""Build 002G record identity tests."""

from m3.research.identity import (
    apply_identity,
    split_multi_notice_text,
    verify_record_identity,
)
from m3.research.package import audit_false_complete_candidate, compute_package_status
from m3.research.qualification import qualify_for_acquisition


def test_forecast_identity():
    r = verify_record_identity(
        {"title": "Buying plan for furniture", "official_url": "https://example.gov/plan", "is_forecast": True},
        source_config={"is_forecast": True, "dataset_purpose": "purchasing_plan"},
    )
    assert r["record_type"] == "PURCHASING_PLAN"
    assert not r["verified_individual_solicitation"]


def test_open_data_thin_row():
    r = verify_record_identity(
        {
            "title": "Some agency request",
            "official_url": "https://data.cityofnewyork.us/resource/dg92-zbpx.json",
            "solicitation_status": "unknown",
        }
    )
    assert r["record_type"] == "GENERAL_PROCUREMENT_INFORMATION"
    assert not r["verified_individual_solicitation"]


def test_ramp_detail_is_individual():
    r = verify_record_identity(
        {
            "title": "BATTERY TESTING KIT",
            "solicitation_number": "233211",
            "buyer_name": "Water & Power",
            "official_url": "https://www.rampla.org/s/opportunity-details?id=006Ql00000ov3nZIAQ",
            "solicitation_status": "open",
        }
    )
    assert r["record_type"] == "INDIVIDUAL_SOLICITATION"
    assert r["verified_individual_solicitation"]


def test_delaware_detail_is_individual():
    r = verify_record_identity(
        {
            "title": "Biden Microgrid",
            "solicitation_number": "ARNG2318-BIDENMICROGRID",
            "buyer_name": "ARNG",
            "official_url": "https://mmp.delaware.gov/Bids/Details/9327",
            "solicitation_status": "open",
        }
    )
    assert r["verified_individual_solicitation"]


def test_junk_title_unrelated():
    r = verify_record_identity(
        {"title": "Glossary", "official_url": "https://example.gov/glossary"}
    )
    assert r["record_type"] == "UNRELATED_CONTENT"


def test_nj_pdf_document():
    r = verify_record_identity(
        {
            "title": "RFP 25-DPP-001 Supply of Batteries",
            "official_url": "https://www.nj.gov/treasury/purchase/bid/rfp25.pdf",
            "buyer_name": "New Jersey Purchase Bureau",
        }
    )
    assert r["record_type"] == "INDIVIDUAL_SOLICITATION"


def test_multi_notice_split():
    text = (
        "Solicitation Number: ABC-100\nBuy widgets\n\n"
        "Solicitation Number: ABC-200\nBuy bolts\n"
    )
    notices = split_multi_notice_text(text)
    assert len(notices) == 2
    assert notices[0]["solicitation_number"] == "ABC-100"


def test_qualification_skips_forecast_identity():
    q = qualify_for_acquisition(
        {
            "requirement_class": "product_supply",
            "solicitation_status": "forecast",
            "record_type": "PROCUREMENT_FORECAST",
        }
    )
    assert q["disposition"] == "SKIP_NON_SOLICITATION_IDENTITY"


def test_false_complete_junk():
    audit = audit_false_complete_candidate(
        {
            "title": "California",
            "package_status": "COMPLETE",
            "downloaded_count": 0,
            "identified_count": 0,
            "url_kind": "unknown",
            "detail_fields": {},
        }
    )
    assert audit["is_false_complete"]


def test_complete_requires_inventory_empty_flag():
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
            verified_individual_solicitation=True,
            inventory_explicitly_empty=True,
        )
        == "COMPLETE"
    )
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
            verified_individual_solicitation=True,
            inventory_explicitly_empty=False,
        )
        == "VERIFICATION_REQUIRED"
    )


def test_apply_identity_sets_fields():
    out = apply_identity(
        {
            "title": "Open Bid for Office Supplies",
            "solicitation_number": "IFB-9",
            "buyer_name": "City Purchasing",
            "official_url": "https://city.gov/solicitation/IFB-9",
            "solicitation_status": "open",
        }
    )
    assert out["record_type"] == "INDIVIDUAL_SOLICITATION"
    assert out["verified_individual_solicitation"] is True

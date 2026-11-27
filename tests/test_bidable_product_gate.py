"""Active UI must reject services / construction / vague / missing category."""

from m3.research.opportunity_ui_ready import bidable_product_fail_reasons, opportunity_ui_ready


def _base(**kwargs):
    item = {
        "title": "Water Softener Salt",
        "display_title": "9,062 40lb bags of water softening salt",
        "deadline_at": "2027-01-05T00:00:00+00:00",
        "solicitation_number": "127641 OR",
        "official_url": "https://example.com/bid",
        "product_category": "chemicals",
        "requirement_class": "product_supply",
        "want_type": "PRODUCT",
    }
    item.update(kwargs)
    return item


def test_product_salt_passes():
    ok, reasons = opportunity_ui_ready(_base())
    assert ok is True
    assert reasons == []


def test_service_title_fails():
    reasons = bidable_product_fail_reasons(
        _base(display_title="Tree Trimming Services - JOB Walk", title="Tree Trimming Services")
    )
    assert any(r.startswith("service_contract:") for r in reasons)


def test_hauling_fails():
    reasons = bidable_product_fail_reasons(
        _base(display_title="Hauling of voting machines and related election material")
    )
    assert any("hauling" in r for r in reasons)


def test_missing_category_fails():
    reasons = bidable_product_fail_reasons(_base(product_category=None))
    assert "missing_product_category" in reasons


def test_vague_title_fails():
    reasons = bidable_product_fail_reasons(_base(display_title="Goods for the division of water"))
    assert "vague_title" in reasons

"""Complex titles must enter the queue for qualification — not silent exclusion."""

from buyer_intelligence.full_scale_production_funnel_v1 import _queue_row_from_any


def test_complex_title_included_and_demoted():
    row = _queue_row_from_any(
        {
            "opportunity_id": "c1",
            "title": "Installation Services and Construction Support Package",
            "product_class": "UNKNOWN_PRODUCTNESS",
            "funnel_class": "LIVE_ELIGIBLE",
            "days_left": 14,
        }
    )
    assert row is not None
    assert row["complex_title_needs_qualify"] is True
    assert float(row["selection_score"]) < 0


def test_common_product_still_preferred():
    complex_row = _queue_row_from_any(
        {
            "opportunity_id": "c2",
            "title": "Professional Consulting Installation Engagement",
            "product_class": "PRODUCT",
            "funnel_class": "LIVE_ELIGIBLE",
            "days_left": 14,
            "authoritative_url": "https://www.bidnetdirect.com/x",
        }
    )
    paper = _queue_row_from_any(
        {
            "opportunity_id": "p1",
            "title": "Copy Paper Supplies",
            "product_class": "PRODUCT",
            "funnel_class": "LIVE_ELIGIBLE",
            "days_left": 14,
            "authoritative_url": "https://www.bidnetdirect.com/x",
        }
    )
    assert paper is not None and complex_row is not None
    assert float(paper["selection_score"]) > float(complex_row["selection_score"])

"""CFE awardable-scope + price-research hook (no network)."""

from __future__ import annotations


def test_detect_awardable_scope_single_line():
    from buyer_intelligence.commercial_funnel_execution_v1 import detect_awardable_scope

    r = detect_awardable_scope(
        title="Copy Paper",
        lines=[{"description": "20lb White Copy Paper", "quantity": 241, "uom": "CS"}],
        package_status="DOCUMENTS_RECOVERED",
    )
    assert r["award_unit"] == "INDIVIDUAL_LINE"
    assert r["complete_awardable_lot"] is True


def test_detect_awardable_scope_large_package_assumed():
    from buyer_intelligence.commercial_funnel_execution_v1 import detect_awardable_scope

    lines = [{"description": f"Item {i}", "quantity": i + 1} for i in range(25)]
    r = detect_awardable_scope(
        title="MRO Supplies",
        lines=lines,
        package_status="DOCUMENTS_RECOVERED",
    )
    assert r["award_unit"] == "COMPLETE_PACKAGE"
    assert r["complete_awardable_package"] is True
    assert r["complete_awardable_lot"] is False


def test_assess_economics_package_preliminary():
    from buyer_intelligence.commercial_funnel_execution_v1 import assess_economics

    row = {
        "historical_unit_price": "12.00",
        "public_unit_price": "8.00",
        "primary_quantity": "10",
        "awardable_scope": {"award_unit": "COMPLETE_PACKAGE"},
    }
    econ = assess_economics(row)
    assert econ["econ_state"] == "PRELIMINARY_MARGIN_PACKAGE_SCOPE"
    assert econ["financeable"] is False

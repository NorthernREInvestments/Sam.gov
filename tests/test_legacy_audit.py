"""Legacy audit / quarantine regression tests. Zero live APIs. Zero DB mutations."""

from __future__ import annotations

from types import SimpleNamespace

import ai_funnel
from data_integrity import (
    STATUS_ASSESSMENT,
    STATUS_CALCULATED,
    STATUS_SOURCE_CONFLICT,
    STATUS_UNPROVEN_LEGACY,
    STATUS_VERIFIED,
    can_hard_reject_on_fact,
    display_fact,
    require_provenance_for_verified_write,
    verified_numeric_or_none,
)
from display_format import format_money_or_unknown
from legacy_data_integrity import (
    amounts_equal,
    audit_contract_record,
    classify_estimated_value,
    classify_freight_or_supplier,
    classify_unit_rate,
    usable_in_actual_profit,
)


def _opp(**kwargs):
    base = dict(
        notice_id="n1",
        title="Groundskeeping",
        description="mowing",
        due_date=None,
        status="new",
        set_aside="Total Small Business",
        naics_code="561730",
        agency="NUWC",
        location="Kapolei, HI",
        analysis={},
        sam_raw={},
        estimated_value=None,
        link=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_legacy_ai_estimated_value_not_verified():
    fact = classify_estimated_value(
        orm_estimated_value="12000",
        sam_raw={},
        analysis={"estimated_value": "$12,000"},
    )
    assert fact["status"] == STATUS_ASSESSMENT
    assert can_hard_reject_on_fact(fact) is False


def test_legacy_unknown_origin_estimated_value_not_verified():
    fact = classify_estimated_value(
        orm_estimated_value="27081.00",
        sam_raw={},
        analysis={},
    )
    assert fact["status"] == STATUS_UNPROVEN_LEGACY
    assert can_hard_reject_on_fact(fact) is False
    assert verified_numeric_or_none(fact) is None


def test_legacy_matching_structured_sam_becomes_verified():
    fact = classify_estimated_value(
        orm_estimated_value="27081.00",
        sam_raw={"award": {"amount": 27081.00}},
        analysis={},
    )
    assert fact["status"] == STATUS_VERIFIED
    assert fact["amount"] == 27081.0
    assert "award.amount" in fact["source_field"]
    assert can_hard_reject_on_fact(fact) is True


def test_arbitrary_numeric_coincidence_cannot_become_verified():
    # Same digits appear in free-text description only — NOT a structured money field
    fact = classify_estimated_value(
        orm_estimated_value="27081.00",
        sam_raw={"descriptionText": "Work at Building 27081 near ZIP 96707 NAICS 561730"},
        analysis={},
    )
    assert fact["status"] == STATUS_UNPROVEN_LEGACY
    assert can_hard_reject_on_fact(fact) is False


def test_source_conflict_unresolved_cannot_hard_reject():
    fact = classify_estimated_value(
        orm_estimated_value="27081.00",
        sam_raw={"award": {"amount": 50000}},
        analysis={},
    )
    assert fact["status"] == STATUS_SOURCE_CONFLICT
    assert fact["legacy_value"] == 27081.0
    assert fact["source_value"] == 50000.0
    assert can_hard_reject_on_fact(fact) is False
    r = ai_funnel.stage0_evaluate(
        _opp(estimated_value="27081.00", sam_raw={"award": {"amount": 50000}})
    )
    assert "value_below_min_profit" not in (r.get("reject_reasons") or [])
    assert r["decision"] != "REJECT" or "value_below_min_profit" not in r["reject_reasons"]


def test_unproven_legacy_cannot_trigger_stage0_hard_reject():
    r = ai_funnel.stage0_evaluate(_opp(estimated_value="$6,000"))
    assert "value_below_min_profit" not in (r.get("reject_reasons") or [])
    assert r["advance"] is True


def test_unproven_supplier_price_not_in_profit_math():
    fact = classify_freight_or_supplier(412.50, field="supplier_price")
    assert fact["status"] == STATUS_UNPROVEN_LEGACY
    assert usable_in_actual_profit(fact) is False
    assert verified_numeric_or_none(fact) is None


def test_unproven_freight_not_zero():
    fact = classify_freight_or_supplier(None, field="freight")
    assert fact["status"] == "UNKNOWN"
    assert fact["value"] is None
    legacy = classify_freight_or_supplier(500, field="freight")
    assert legacy["status"] == STATUS_UNPROVEN_LEGACY
    assert legacy["value"] == 500  # preserved, not replaced with 0


def test_ai_historical_remains_assessment_unless_independently_verified():
    ai_only = classify_estimated_value(
        orm_estimated_value=None,
        sam_raw={},
        analysis={"estimated_value": "$73,000"},
    )
    assert ai_only["status"] == STATUS_ASSESSMENT
    verified = classify_estimated_value(
        orm_estimated_value="73000",
        sam_raw={"awardAmount": 73000},
        analysis={"estimated_value": "$73,000"},
    )
    assert verified["status"] == STATUS_VERIFIED


def test_reproducible_calculation_becomes_calculated():
    from data_integrity import verified_fact

    sqft = verified_fact(10000, source_type="SOLICITATION", source_field="square_footage")
    fact = classify_unit_rate(
        rate=10.0,
        awarded_amount=100000,
        square_footage_fact=sqft,
        visits_per_year=None,
        field="price_per_sqft_per_year",
    )
    assert fact["status"] == STATUS_CALCULATED
    assert usable_in_actual_profit(fact) is True


def test_non_reproducible_legacy_calculation_unproven():
    fact = classify_unit_rate(
        rate=0.012345,
        awarded_amount=100000,
        square_footage_fact=None,
        visits_per_year=52,
        field="price_per_sqft_per_visit",
    )
    assert fact["status"] == STATUS_UNPROVEN_LEGACY


def test_ui_api_distinguishes_verified_from_unproven():
    verified = classify_estimated_value(
        orm_estimated_value="27081",
        sam_raw={"award": {"amount": 27081}},
        analysis={},
    )
    unproven = classify_estimated_value(
        orm_estimated_value="27081",
        sam_raw={},
        analysis={},
    )
    assert "unverified legacy" in format_money_or_unknown(unproven).lower()
    assert "(unverified" not in format_money_or_unknown(verified).lower()
    assert "unverified legacy" in display_fact(unproven).lower()


def test_write_guard_blocks_ai_self_certify_verified():
    import pytest

    with pytest.raises(ValueError):
        require_provenance_for_verified_write(
            field="estimated_value",
            value=1000,
            provenance={"status": "VERIFIED", "source_type": "AI", "source_field": "analysis"},
        )


def test_dry_run_auditor_mutates_zero_records():
    """Auditor helpers never mutate; script defaults to dry-run with --apply disabled."""
    fake_row = _opp(estimated_value="100", sam_raw={}, analysis={})
    fake_row.id = 99
    findings = audit_contract_record(fake_row)
    assert isinstance(findings, list)
    # Prove values preserved (not zeroed)
    for f in findings:
        if f.get("field") == "estimated_value":
            assert f.get("existing_value") != 0
            assert f.get("classification") == STATUS_UNPROVEN_LEGACY

    assert amounts_equal(27081.0, 27081.00)
    assert not amounts_equal(27081.0, 27082.0)

    import scripts.audit_legacy_data_integrity as auditor_mod

    # --apply must refuse mutation
    import sys

    old = sys.argv
    try:
        sys.argv = ["audit_legacy_data_integrity.py", "--apply"]
        assert auditor_mod.main() == 2
    finally:
        sys.argv = old

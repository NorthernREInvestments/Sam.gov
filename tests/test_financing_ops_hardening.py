"""Operational hardening cases A–L for Financing Intelligence."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from financing_intelligence.assess import assess_opportunity_financing, attach_financing_to_opportunity_row
from financing_intelligence.capital import capital_snapshot, confirm_reservation, propose_reservation, update_capital
from financing_intelligence.constants import (
    CREDIT_HARD,
    CREDIT_NOT_REQUIRED,
    PG_NOT_REQUIRED,
    PG_REQUIRED,
    SRC_PO_FINANCE,
    ST_CAPITAL_CONFIRMATION_REQUIRED,
    ST_CAPITAL_STACK_CLOSED,
    ST_EXECUTION_FAIL,
    ST_FINANCEABLE_BUT_TIMING_RISK,
    ST_FINANCING_GAP,
    ST_FINANCING_UNKNOWN,
    ST_LIKELY_FINANCEABLE,
)
from financing_intelligence.cost import estimate_financing_cost
from financing_intelligence.outcomes import historical_outcome_patterns, record_outcome
from financing_intelligence.sources import upsert_source
from financing_intelligence.store import money, reset_data_root, set_data_root
from financing_intelligence.supplier_bridge import verified_supplier_cover


@pytest.fixture()
def fin_tmp(tmp_path):
    set_data_root(tmp_path / "financing_intelligence")
    yield tmp_path
    reset_data_root()


def _lender_named(name="TradeCap", **overrides):
    terms = {
        "max_advance_pct": "90",
        "personal_guarantee": PG_NOT_REQUIRED,
        "personal_credit": CREDIT_NOT_REQUIRED,
        "minimum_gross_margin_pct": "15",
        "federal_contracts_accepted": "YES",
        "can_fund_freight": "YES",
        "product_resale_supported": "YES",
        "startup_new_company_allowed": "YES",
        "fee_pct_of_advance": "4",
    }
    terms.update(overrides)
    return upsert_source({"company_name": name, "source_type": SRC_PO_FINANCE, "active": True, "terms": terms})


def test_case_a_90_10_stack(fin_tmp):
    _lender_named()
    r = assess_opportunity_financing(
        opportunity_id="A",
        contract_value="130000",
        supplier_cost="100000",
        freight="0",
        supplier_terms={"supplier_name": "Ferguson", "net_days": 30, "available_credit": "10000", "verified": True},
        financed_days=45,
    )
    assert r["card"]["status"] == ST_LIKELY_FINANCEABLE
    assert money(r["card"]["owner_cash_required"]) == Decimal("0")
    assert money(r["card"]["unfunded_gap"]) == Decimal("0")
    assert r["card"]["lender_approval_still_required"] is True


def test_case_b_supplier_gap(fin_tmp):
    _lender_named()
    r = assess_opportunity_financing(
        opportunity_id="B",
        contract_value="130000",
        supplier_cost="100000",
        freight="0",
        supplier_terms={"supplier_name": "Ferguson", "net_days": 30, "available_credit": "5000", "verified": True},
    )
    assert r["card"]["status"] == ST_FINANCING_GAP
    assert money(r["card"]["unfunded_gap"]) == Decimal("5000.00")


def test_case_c_cash_not_auto(fin_tmp):
    _lender_named()
    update_capital(business_cash="20000")
    r = assess_opportunity_financing(
        opportunity_id="C",
        contract_value="130000",
        supplier_cost="100000",
        freight="0",
        supplier_terms={"available_credit": "0", "verified": True},
    )
    # 90% covers 90k, gap 10k, deployable 20k → confirmation
    assert r["card"]["status"] == ST_CAPITAL_CONFIRMATION_REQUIRED
    assert money(r["card"]["unfunded_gap"]) == Decimal("10000.00")


def test_case_d_capital_approved(fin_tmp):
    _lender_named()
    update_capital(business_cash="20000")
    prop = propose_reservation(opportunity_id="D", amount="10000")
    confirm_reservation(reservation_id=prop["reservation_id"])
    r = assess_opportunity_financing(
        opportunity_id="D",
        contract_value="130000",
        supplier_cost="100000",
        freight="0",
        supplier_terms={"available_credit": "0", "verified": True},
    )
    assert r["card"]["status"] == ST_CAPITAL_STACK_CLOSED
    assert money(capital_snapshot()["active_commitments"]) == Decimal("10000.00")
    assert money(capital_snapshot(exclude_opportunity_id="OTHER")["deployable_capital"]) == Decimal("10000.00")


def test_case_e_personal_guarantee(fin_tmp):
    _lender_named(personal_guarantee=PG_REQUIRED)
    r = assess_opportunity_financing(
        opportunity_id="E",
        contract_value="130000",
        supplier_cost="100000",
        freight="0",
        supplier_terms={"available_credit": "10000", "verified": True},
    )
    assert r["card"]["status"] == ST_EXECUTION_FAIL
    assert "personal guarantee" in (r["card"]["next_action"] or "").lower() or "pg" in " ".join(
        (r["analysis"]["best_stack"].get("blockers") or [])
    )


def test_case_f_personal_credit(fin_tmp):
    _lender_named(personal_credit=CREDIT_HARD)
    r = assess_opportunity_financing(
        opportunity_id="F",
        contract_value="130000",
        supplier_cost="100000",
        freight="0",
        supplier_terms={"available_credit": "10000", "verified": True},
    )
    assert r["card"]["status"] == ST_EXECUTION_FAIL


def test_case_g_unknown_freight(fin_tmp):
    src = _lender_named(can_fund_freight="UNKNOWN")
    r = assess_opportunity_financing(
        opportunity_id="G",
        contract_value="150000",
        supplier_cost="100000",
        freight="8000",
        supplier_terms={"available_credit": "10000", "verified": True, "supplier_name": "Ferguson"},
    )
    assert r["card"]["status"] != ST_EXECUTION_FAIL
    # Advance on product only → 90k; remaining 18k; supplier 10k → gap 8k OR confirmation
    assert "freight" in (r["card"]["next_action"] or "").lower() or r["analysis"]["best_stack"].get(
        "freight_funding_unknown"
    )


def test_case_h_financing_cost(fin_tmp):
    upsert_source(
        {
            "company_name": "FeeBank",
            "source_type": SRC_PO_FINANCE,
            "terms": {
                "max_advance_pct": "100",
                "personal_guarantee": PG_NOT_REQUIRED,
                "personal_credit": CREDIT_NOT_REQUIRED,
                "federal_contracts_accepted": "YES",
                "can_fund_freight": "YES",
                "origination_fee": "4000",
            },
        }
    )
    r = assess_opportunity_financing(
        opportunity_id="H",
        contract_value="130000",
        supplier_cost="100000",
        freight="0",
        supplier_terms={"available_credit": "0", "verified": True},
    )
    assert money(r["card"]["profit_before_financing"]) == Decimal("30000.00")
    assert money(r["card"]["estimated_financing_cost"]) == Decimal("4000.00")
    assert money(r["card"]["profit_after_financing"]) == Decimal("26000.00")


def test_case_i_time_based_fee(fin_tmp):
    detail = estimate_financing_cost(
        financed_amount=Decimal("100000"),
        rules={"daily_rate": "0.05"},  # 0.05% per day
        financed_days=30,
    )
    # 100000 * 0.05/100 * 30 = 1500
    assert detail["total_decimal"] == Decimal("1500.00")
    unknown = estimate_financing_cost(
        financed_amount=Decimal("100000"),
        rules={"daily_rate": "0.05"},
        financed_days=None,
    )
    assert unknown["partially_unknown"] is True
    assert unknown["total_decimal"] == Decimal("0")


def test_case_j_timing_risk(fin_tmp):
    _lender_named(max_advance_pct="100", fee_pct_of_advance="0")
    today = date.today()
    r = assess_opportunity_financing(
        opportunity_id="J",
        contract_value="130000",
        supplier_cost="100000",
        freight="0",
        supplier_terms={"available_credit": "0", "verified": True, "net_days": 30},
        timing_dates={
            "supplier_payment_due_date": (today + timedelta(days=10)).isoformat(),
            "expected_government_payment_date": (today + timedelta(days=5)).isoformat(),
        },
    )
    assert r["card"]["status"] == ST_FINANCEABLE_BUT_TIMING_RISK
    assert r["card"]["timing_risk"] is True


def test_case_k_no_acquisition_cost(fin_tmp):
    row = {"canonical_id": "K", "estimated_value": 100000}
    attach_financing_to_opportunity_row(row)
    assert row["financing_status"] == ST_FINANCING_UNKNOWN
    assert "acquisition cost" in (row["financing_intelligence"]["next_action"] or "").lower()


def test_case_l_historical_not_auto_rule(fin_tmp):
    src = _lender_named()
    for i in range(4):
        record_outcome(
            {
                "source_id": src["source_id"],
                "opportunity_id": f"H{i}",
                "status": "APPROVED",
                "approved": True,
                "actual_advance_pct": "91.5",
                "actual_approval_time_days": "2",
            }
        )
    record_outcome(
        {
            "source_id": src["source_id"],
            "opportunity_id": "H4",
            "status": "DENIED",
            "declined": True,
            "decline_reason": "margin",
        }
    )
    pat = historical_outcome_patterns(src["source_id"])
    assert pat["authoritative_rule"] is False
    assert pat["approved_count"] == 4
    assert pat["recorded"] == 5
    assert "not a verified lender rule" in (pat["summary"] or "").lower()
    # Patterns must not appear as approved facts on source
    from financing_intelligence.sources import approved_rules_for_source

    rules = approved_rules_for_source(src["source_id"])
    assert "average_observed_advance_pct" not in rules


def test_unverified_supplier_terms_do_not_count(fin_tmp):
    _lender_named()
    cover = verified_supplier_cover({"available_credit": "50000", "net_days": 30, "verified": False})
    assert money(cover["usable_credit"]) == Decimal("0")
    r = assess_opportunity_financing(
        opportunity_id="UV",
        contract_value="130000",
        supplier_cost="100000",
        freight="0",
        supplier_terms={"available_credit": "10000", "net_days": 30},  # not verified
    )
    assert money(r["card"]["unfunded_gap"]) == Decimal("10000.00")


def test_hard_acceptance_end_to_end(fin_tmp):
    """Hard acceptance: 90/10 + fees + timing + gap + confirm."""
    _lender_named(
        max_advance_pct="90",
        can_fund_freight="YES",
        fee_pct_of_advance="5",
        personal_guarantee=PG_NOT_REQUIRED,
        personal_credit=CREDIT_NOT_REQUIRED,
    )
    opp = "HARD-ACCEPT"
    terms_ok = {
        "supplier_name": "Ferguson",
        "net_days": 30,
        "available_credit": "12500",
        "verified": True,
    }
    r1 = assess_opportunity_financing(
        opportunity_id=opp,
        contract_value="180000",
        supplier_cost="120000",
        freight="5000",
        supplier_terms=terms_ok,
        financed_days=40,
    )
    assert r1["card"]["status"] == ST_LIKELY_FINANCEABLE
    assert money(r1["card"]["owner_cash_required"]) == Decimal("0")
    assert money(r1["card"]["estimated_financing_cost"]) > Decimal("0")
    assert money(r1["card"]["profit_after_financing"]) < money(r1["card"]["profit_before_financing"])
    assert r1["card"]["lender_approval_still_required"] is True

    r2 = assess_opportunity_financing(
        opportunity_id=opp,
        contract_value="180000",
        supplier_cost="120000",
        freight="5000",
        supplier_terms={**terms_ok, "available_credit": "5000"},
        financed_days=40,
    )
    assert r2["card"]["status"] == ST_FINANCING_GAP
    gap = money(r2["card"]["unfunded_gap"])
    assert gap == Decimal("7500.00")

    update_capital(business_cash="20000")
    r3 = assess_opportunity_financing(
        opportunity_id=opp,
        contract_value="180000",
        supplier_cost="120000",
        freight="5000",
        supplier_terms={**terms_ok, "available_credit": "5000"},
        financed_days=40,
    )
    assert r3["card"]["status"] == ST_CAPITAL_CONFIRMATION_REQUIRED

    prop = propose_reservation(opportunity_id=opp, amount=gap)
    assert confirm_reservation(reservation_id=prop["reservation_id"])["ok"] is True
    r4 = assess_opportunity_financing(
        opportunity_id=opp,
        contract_value="180000",
        supplier_cost="120000",
        freight="5000",
        supplier_terms={**terms_ok, "available_credit": "5000"},
        financed_days=40,
    )
    assert r4["card"]["status"] == ST_CAPITAL_STACK_CLOSED
    assert money(capital_snapshot()["active_commitments"]) == gap

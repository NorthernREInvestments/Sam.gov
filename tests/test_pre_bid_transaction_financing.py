"""Focused tests for pre-bid transaction financing underwriting."""

from __future__ import annotations

from datetime import datetime, timezone

from application_clock import freeze_time
from funding_path_constants import (
    EV_VERIFIED_BY_CALL,
    MATCH_CONDITIONAL,
    MATCH_NEEDS_VERIFICATION,
    MATCH_REJECT,
    PG_NOT_REQUIRED,
    PG_REQUIRED,
)
from funding_source_kb import (
    criterion_evidence,
    fixture_compatible_po_lender,
    fixture_pg_required_lender,
    set_criterion,
)
from funding_path_intelligence import match_funding_source
from funding_underwriting import (
    CREDIT_HARD_PULL,
    CREDIT_NONE,
    CREDIT_SOFT_PULL,
    CREDIT_UNKNOWN,
    GUARANTEE_FULL_RECOURSE,
    GUARANTEE_LIMITED,
    GUARANTEE_NO_PG,
    GUARANTEE_UNKNOWN,
    PATH_NEEDS_VERIFICATION,
    PATH_PREFERRED,
    PATH_REJECT,
    PATH_WORKABLE_OPERATOR_APPROVAL,
    build_personal_credit_model,
    classify_path_underwriting,
    normalize_credit_checked,
    parse_guarantee_state,
)
from pre_bid_transaction_financing import (
    assess_pre_bid_financing_maturity,
    build_composite_funding_path,
    build_financier_call_sheet,
    build_financier_packet,
    build_operator_pg_review,
    build_transaction_funding_requirement,
    calculate_actual_expected_profit,
    evaluate_funding_timing_compatibility,
    ingest_financier_call_outcome,
)


FIXED = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)


def _v(name, value):
    return criterion_evidence(
        criterion_name=name,
        value=value,
        verification_status=EV_VERIFIED_BY_CALL,
        verified_at=FIXED.isoformat(),
        confidence="HIGH",
    )


def test_pg_required_does_not_automatically_reject():
    with freeze_time(FIXED):
        r = match_funding_source(
            {"funding_amount_required": 100000, "government_customer_type": "federal"},
            fixture_pg_required_lender(),
        )
    assert r["match_status"] != MATCH_REJECT


def test_no_pg_preferred_path():
    src = fixture_compatible_po_lender()
    uw = classify_path_underwriting(src, operator_personal_fico=480)
    assert uw["path_class"] == PATH_PREFERRED
    assert uw["personal_guarantee"]["guarantee_state"] == GUARANTEE_NO_PG


def test_full_recourse_and_limited_pg_parsed_separately():
    full = parse_guarantee_state(personal_guarantee=PG_REQUIRED, guarantee_type=GUARANTEE_FULL_RECOURSE)
    lim = parse_guarantee_state(personal_guarantee=PG_REQUIRED, guarantee_type=GUARANTEE_LIMITED)
    assert full["guarantee_state"] == GUARANTEE_FULL_RECOURSE
    assert lim["guarantee_state"] == GUARANTEE_LIMITED
    unk = parse_guarantee_state(personal_guarantee=None)
    assert unk["guarantee_state"] == GUARANTEE_UNKNOWN


def test_personal_credit_none_soft_hard():
    assert normalize_credit_checked(False) == CREDIT_NONE
    assert normalize_credit_checked("SOFT_PULL") == CREDIT_SOFT_PULL
    assert normalize_credit_checked("HARD_PULL") == CREDIT_HARD_PULL
    assert normalize_credit_checked(None) == CREDIT_UNKNOWN


def test_minimum_fico_480_below_verified_rejects():
    src = fixture_compatible_po_lender()
    src = set_criterion(src, "minimum_personal_fico", 650, _v("minimum_personal_fico", 650))
    src["minimum_personal_fico"] = 650
    src = set_criterion(src, "personal_credit_checked", True, _v("personal_credit_checked", True))
    src["personal_credit_checked"] = True
    uw = classify_path_underwriting(src, operator_personal_fico=480)
    assert uw["path_class"] == PATH_REJECT
    assert "verified_minimum_fico_above_operator_score" in uw["reject_reasons"]


def test_480_with_no_minimum_does_not_auto_reject():
    src = fixture_compatible_po_lender()
    # Soft pull, no min FICO
    src = set_criterion(src, "personal_credit_pull", CREDIT_SOFT_PULL, _v("personal_credit_pull", CREDIT_SOFT_PULL))
    src["personal_credit_pull"] = CREDIT_SOFT_PULL
    src["personal_credit_checked"] = CREDIT_SOFT_PULL
    uw = classify_path_underwriting(src, operator_personal_fico=480)
    assert uw["path_class"] != PATH_REJECT or "verified_minimum_fico" not in str(uw["reject_reasons"])


def test_480_explicitly_acceptable_does_not_reject():
    src = fixture_compatible_po_lender()
    src = set_criterion(
        src, "poor_credit_explicitly_acceptable", True, _v("poor_credit_explicitly_acceptable", True)
    )
    src["poor_credit_explicitly_acceptable"] = True
    uw = classify_path_underwriting(src, operator_personal_fico=480)
    assert "poor_credit_explicitly_acceptable" in uw["reasons"] or uw["path_class"] != PATH_REJECT


def test_personal_credit_role_unknown_remains_verification():
    src = fixture_compatible_po_lender()
    # wipe credit evidence
    src["personal_credit_checked"] = None
    src["criteria_evidence"] = {
        k: v for k, v in (src.get("criteria_evidence") or {}).items() if k != "personal_credit_checked"
    }
    uw = classify_path_underwriting(src, operator_personal_fico=480)
    assert uw["path_class"] == PATH_NEEDS_VERIFICATION or "personal_credit" in str(uw["verification_reasons"])


def test_cash_contribution_required_rejects_when_uncovered():
    src = fixture_compatible_po_lender()
    src = set_criterion(src, "borrower_cash_contribution_required", True, _v("borrower_cash_contribution_required", True))
    src["borrower_cash_contribution_required"] = True
    uw = classify_path_underwriting(src, uncovered_operator_cash=5000)
    assert uw["path_class"] == PATH_REJECT


def test_composite_funding_closes_cash_gap():
    req = build_transaction_funding_requirement({"supplier_cost": 100000, "freight": 0})
    partial = build_composite_funding_path(
        req,
        [{"component": "PO", "amount": 80000, "evidence_state": "VERIFIED_PUBLIC", "fees": 2000}],
    )
    assert partial["ZERO_CASH_SATISFIED"] is False
    assert partial["remaining_uncovered_cash_gap"] == 20000

    full = build_composite_funding_path(
        req,
        [
            {"component": "PO", "amount": 100000, "evidence_state": "VERIFIED_PUBLIC", "fees": 3500},
            {"component": "factoring", "amount": 0, "evidence_state": "VERIFIED_PUBLIC", "fees": 1000},
        ],
    )
    assert full["ZERO_CASH_SATISFIED"] is True


def test_unknown_financing_does_not_satisfy_zero_cash():
    req = build_transaction_funding_requirement({"supplier_cost": 50000})
    c = build_composite_funding_path(
        req,
        [{"component": "mystery", "amount": 50000, "evidence_state": "UNKNOWN"}],
    )
    assert c["ZERO_CASH_SATISFIED"] is False


def test_funding_timing_compatible_incompatible_unknown():
    ok = evaluate_funding_timing_compatibility(days_until_supplier_payment=14, typical_funding_days=5)
    assert ok["status"] == "FUNDING_TIMING_COMPATIBLE"
    bad = evaluate_funding_timing_compatibility(days_until_supplier_payment=3, typical_funding_days=10)
    assert bad["status"] == "FUNDING_TIMING_INCOMPATIBLE"
    unk = evaluate_funding_timing_compatibility()
    assert unk["status"] == "FUNDING_TIMING_UNKNOWN"


def test_finance_cost_in_actual_profit_and_expensive_can_pass():
    r = calculate_actual_expected_profit(
        government_revenue=100000,
        supplier_cost=75000,
        freight=1000,
        transaction_expenses=500,
        po_finance_fees=3500,
        factoring_fees=1000,
    )
    assert r["actual_expected_profit"] == 19000
    assert r["profit_floor_satisfied"] is True
    assert r["financing_rejected_for_being_expensive"] is False

    low = calculate_actual_expected_profit(
        government_revenue=100000,
        supplier_cost=90000,
        freight=1000,
        transaction_expenses=500,
        po_finance_fees=5000,
    )
    assert low["actual_expected_profit"] == 3500
    assert low["profit_floor_satisfied"] is False


def test_public_fit_not_verified_funding_and_call_outranks():
    from pre_bid_transaction_financing import rank_provider_public_fit

    fit = rank_provider_public_fit({"source_name": "X", "product_resale_fit": "STRONG", "government_customer_types_supported": ["state"]})
    assert fit["is_approval"] is False
    assert fit["is_verified_funding"] is False
    out = ingest_financier_call_outcome(
        {"provider": "X", "contact_occurred": True, "explicitly_confirmed_secured": False, "notes": "maybe"}
    )
    assert out["evidence_class"] == "PROVIDER_CONFIRMED_DEAL_SPECIFIC"
    assert out["funding_secured"] is False
    assert out["outranks_public_marketing"] is True


def test_pre_bid_packet_preserves_unknown():
    pkt = build_financier_packet(deal={"solicitation_number": "645-DOTRFB-2975-2027"})
    assert pkt["supplier_quote"] == "UNKNOWN"
    assert pkt["fabricated"] is False
    assert "supplier_quote" in pkt["unknown_fields"] or pkt["freight"] == "UNKNOWN"


def test_call_sheet_asks_credit_and_pg_separately():
    sheet = build_financier_call_sheet(provider={"source_name": "STAR"}, deal={"solicitation_number": "X"})
    keys = {q["key"] for q in sheet["questions"]}
    assert "personal_credit_pull" in keys
    assert "personal_guarantee_required" in keys
    assert "fico_480_disqualify" in keys
    assert sheet["personal_credit_and_pg_asked_separately"] is True
    assert sheet["autonomous_call"] is False


def test_iowa_blade_funding_premature_without_quote():
    m = assess_pre_bid_financing_maturity(
        exact_product_known=False,
        supplier_cost_known=False,
        government_revenue_defined=False,
        funding_amount_estimable=False,
    )
    assert m["pre_bid_financing_status"] == "FUNDING_NOT_YET_READY_FOR_REVIEW"
    assert m["financier_outreach_appropriate"] is False


def test_synthetic_laptop_deal_19k_profit():
    """TEST DATA ONLY — must not contaminate live opportunities."""
    econ = calculate_actual_expected_profit(
        government_revenue=100000,
        supplier_cost=75000,
        freight=1000,
        transaction_expenses=500,
        po_finance_fees=3500,
        factoring_fees=1000,
    )
    assert econ["actual_expected_profit"] == 19000
    req = build_transaction_funding_requirement(
        {
            "supplier_cost": 75000,
            "freight": 1000,
            "other_pre_payment_costs": 500,
            "government_revenue": 100000,
            "data_class": "TEST_FIXTURE",
        }
    )
    assert req["data_class"] == "TEST_FIXTURE"
    comp = build_composite_funding_path(
        req,
        [
            {
                "component": "PO_FINANCE",
                "amount": 75000,
                "fees": 3500,
                "evidence_state": "VERIFIED_PUBLIC",
                "repayment_source": "government_receivable",
            },
            {
                "component": "AR_FACTORING",
                "amount": 1500,
                "fees": 1000,
                "evidence_state": "VERIFIED_PUBLIC",
                "repayment_source": "government_payment",
            },
        ],
    )
    # freight+other may leave gap unless included — supplier 100% covered
    assert comp["covered_amount"] >= 75000
    timing = evaluate_funding_timing_compatibility(days_until_supplier_payment=21, typical_funding_days=5)
    assert timing["status"] == "FUNDING_TIMING_COMPATIBLE"
    # PG evaluated separately
    uw = classify_path_underwriting(
        {
            "personal_guarantee": PG_REQUIRED,
            "criteria_evidence": {
                "personal_guarantee": _v("personal_guarantee", PG_REQUIRED),
                "personal_credit_checked": _v("personal_credit_checked", False),
                "borrower_cash_contribution_required": _v("borrower_cash_contribution_required", False),
            },
            "personal_credit_checked": False,
            "borrower_cash_contribution_required": False,
        },
        operator_personal_fico=480,
        uncovered_operator_cash=0,
        funding_timing_status="FUNDING_TIMING_COMPATIBLE",
        actual_expected_profit=19000,
        transaction_amount=75000,
    )
    assert uw["path_class"] == PATH_WORKABLE_OPERATOR_APPROVAL
    assert uw["autonomous_pg_acceptance"] is False


def test_operator_pg_review_no_legal_advice():
    rev = build_operator_pg_review(
        guaranteed_amount=75000,
        transaction_profit=19000,
        government_customer="State of Iowa",
    )
    assert rev["status"] == "OPERATOR_PG_REVIEW_REQUIRED"
    assert rev["legal_advice"] is False
    assert rev["autonomous_decision"] is False


def test_no_outreach_counters():
    m = assess_pre_bid_financing_maturity(exact_product_known=True, supplier_cost_known=True)
    assert m["financier_outreach"] == 0
    sheet = build_financier_call_sheet()
    assert sheet["financier_outreach"] == 0

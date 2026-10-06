"""Financing Intelligence — acceptance + targeted unit tests (§28 / §32)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from financing_intelligence.assess import assess_opportunity_financing, attach_financing_to_opportunity_row
from financing_intelligence.capital import (
    capital_snapshot,
    confirm_reservation,
    propose_reservation,
    release_reservation,
    update_capital,
)
from financing_intelligence.constants import (
    CREDIT_HARD,
    CREDIT_NOT_REQUIRED,
    EV_COMPLETED_TRANSACTION,
    EV_MARKETING_CLAIM,
    FACT_APPROVED,
    FACT_PROPOSED,
    FACT_REJECTED,
    PG_NOT_REQUIRED,
    PG_REQUIRED,
    SRC_PO_FINANCE,
    ST_CAPITAL_CONFIRMATION_REQUIRED,
    ST_CAPITAL_STACK_CLOSED,
    ST_EXECUTION_FAIL,
    ST_FINANCING_GAP,
    ST_FINANCING_UNKNOWN,
    ST_LIKELY_FINANCEABLE,
)
from financing_intelligence.facts import decide_fact, list_facts
from financing_intelligence.notes_extract import extract_proposed_facts_from_notes, ingest_call_notes
from financing_intelligence.sources import approved_rules_for_source, upsert_source
from financing_intelligence.stack_engine import build_capital_stacks, estimate_financing_cost, match_source_to_opportunity
from financing_intelligence.store import money, reset_data_root, set_data_root


@pytest.fixture()
def fin_tmp(tmp_path):
    set_data_root(tmp_path / "financing_intelligence")
    yield tmp_path
    reset_data_root()


def _lender_a(**term_overrides):
    terms = {
        "max_advance_pct": "90",
        "personal_guarantee": PG_NOT_REQUIRED,
        "personal_credit": CREDIT_NOT_REQUIRED,
        "minimum_gross_margin_pct": "20",
        "federal_contracts_accepted": "YES",
        "can_fund_freight": "YES",
        "fee_pct_of_advance": "6",
        "pays_supplier_directly": "YES",
    }
    terms.update(term_overrides)
    return upsert_source(
        {
            "company_name": "Lender A",
            "source_type": SRC_PO_FINANCE,
            "active": True,
            "terms": terms,
        }
    )


# --- §32 acceptance ---


def test_acceptance_capital_stack_close_gap_confirm(fin_tmp):
    """End-to-end §32 scenario."""
    snap0 = capital_snapshot()
    assert money(snap0["deployable_capital"]) == Decimal("0")

    src = _lender_a()
    opp = "OPP-ACCEPT-32"
    supplier_full = {
        "supplier_name": "Demo Supplier",
        "net_days": 30,
        "available_credit": "12500", "verified": True,  # remaining 10% of $125k prepay
    }

    r1 = assess_opportunity_financing(
        opportunity_id=opp,
        contract_value="180000",
        supplier_cost="120000",
        freight="5000",
        supplier_terms=supplier_full,
        jurisdiction="FEDERAL",
    )
    card = r1["card"]
    assert card["status"] == ST_LIKELY_FINANCEABLE
    assert money(card["unfunded_gap"]) == Decimal("0")
    assert money(card["company_capital_confirmed"]) == Decimal("0")
    assert card["lender_approval_still_required"] is True
    assert any(L.get("role") == "OUTSIDE_FINANCING" for L in card["suggested_stack"])
    assert any(L.get("role") == "SUPPLIER_TERMS" for L in card["suggested_stack"])
    # 90% of 125000 = 112500; 6% fee
    assert money(card["estimated_financing_cost"]) == Decimal("6750.00")
    assert money(card["profit_before_financing"]) == Decimal("55000.00")
    assert money(card["profit_after_financing"]) == Decimal("48250.00")
    assert card.get("primary_lender") == "Lender A" or src["company_name"] == "Lender A"

    # Shrink supplier terms → gap
    r2 = assess_opportunity_financing(
        opportunity_id=opp,
        contract_value="180000",
        supplier_cost="120000",
        freight="5000",
        supplier_terms={"supplier_name": "Demo Supplier", "net_days": 30, "available_credit": "5000", "verified": True},
    )
    assert r2["card"]["status"] == ST_FINANCING_GAP
    gap = money(r2["card"]["unfunded_gap"])
    assert gap == Decimal("7500.00")

    # Record capital — must NOT auto-close
    update_capital(business_cash="20000")
    r3 = assess_opportunity_financing(
        opportunity_id=opp,
        contract_value="180000",
        supplier_cost="120000",
        freight="5000",
        supplier_terms={"supplier_name": "Demo Supplier", "net_days": 30, "available_credit": "5000", "verified": True},
    )
    assert r3["card"]["status"] == ST_CAPITAL_CONFIRMATION_REQUIRED
    assert r3["card"]["capital_confirmation"] is not None
    need = money(r3["card"]["capital_confirmation"]["deal_requires_company_capital"])
    assert need == gap

    # Confirm reservation → stack closed + reserved
    prop = propose_reservation(opportunity_id=opp, amount=need)
    conf = confirm_reservation(reservation_id=prop["reservation_id"], confirmed_by="owner")
    assert conf["ok"] is True

    r4 = assess_opportunity_financing(
        opportunity_id=opp,
        contract_value="180000",
        supplier_cost="120000",
        freight="5000",
        supplier_terms={"supplier_name": "Demo Supplier", "net_days": 30, "available_credit": "5000", "verified": True},
    )
    assert r4["card"]["status"] == ST_CAPITAL_STACK_CLOSED

    snap = capital_snapshot()
    assert money(snap["active_commitments"]) == need
    assert money(snap["deployable_capital"]) == Decimal("20000") - need

    # Other deal cannot see full cash
    snap_b = capital_snapshot(exclude_opportunity_id="OTHER")
    assert money(snap_b["deployable_capital"]) == Decimal("20000") - need


# --- Capital ---


def test_default_capital_zero(fin_tmp):
    assert money(capital_snapshot()["deployable_capital"]) == Decimal("0")


def test_capital_reserve_and_release(fin_tmp):
    update_capital(business_cash="30000", minimum_operating_reserve="10000")
    assert money(capital_snapshot()["deployable_capital"]) == Decimal("20000")
    prop = propose_reservation(opportunity_id="A", amount="12000")
    # proposed does not deduct
    assert money(capital_snapshot()["deployable_capital"]) == Decimal("20000")
    confirm_reservation(reservation_id=prop["reservation_id"])
    assert money(capital_snapshot()["deployable_capital"]) == Decimal("8000")
    release_reservation(prop["reservation_id"])
    assert money(capital_snapshot()["deployable_capital"]) == Decimal("20000")


def test_no_double_count_across_deals(fin_tmp):
    update_capital(business_cash="30000", minimum_operating_reserve="0")
    p1 = propose_reservation(opportunity_id="A", amount="12000")
    confirm_reservation(reservation_id=p1["reservation_id"])
    assert money(capital_snapshot(exclude_opportunity_id="B")["deployable_capital"]) == Decimal("18000")


# --- Matching / sources ---


def test_pg_hard_fail(fin_tmp):
    src = _lender_a(personal_guarantee=PG_REQUIRED)
    rules = approved_rules_for_source(src["source_id"])
    m = match_source_to_opportunity(
        source=src,
        rules=rules,
        contract_value=Decimal("180000"),
        supplier_cost=Decimal("120000"),
        gross_margin_pct=Decimal("30"),
    )
    assert m["hard_fail"] is True
    assert "pg_required_owner_disallows" in m["reasons"]


def test_personal_credit_hard_fail(fin_tmp):
    src = _lender_a(personal_credit=CREDIT_HARD)
    rules = approved_rules_for_source(src["source_id"])
    m = match_source_to_opportunity(
        source=src,
        rules=rules,
        contract_value=Decimal("180000"),
        supplier_cost=Decimal("120000"),
        gross_margin_pct=Decimal("30"),
    )
    assert m["hard_fail"] is True


def test_margin_and_size_rules(fin_tmp):
    src = _lender_a(minimum_gross_margin_pct="40", minimum_transaction_size="200000")
    rules = approved_rules_for_source(src["source_id"])
    m = match_source_to_opportunity(
        source=src,
        rules=rules,
        contract_value=Decimal("180000"),
        supplier_cost=Decimal("120000"),
        gross_margin_pct=Decimal("30"),
    )
    assert m["hard_fail"] is True


def test_unknown_not_fail(fin_tmp):
    src = upsert_source(
        {
            "company_name": "Sparse Lender",
            "source_type": SRC_PO_FINANCE,
            "terms": {"max_advance_pct": "80", "federal_contracts_accepted": "YES"},
        }
    )
    rules = approved_rules_for_source(src["source_id"])
    m = match_source_to_opportunity(
        source=src,
        rules=rules,
        contract_value=Decimal("100000"),
        supplier_cost=Decimal("70000"),
        gross_margin_pct=Decimal("25"),
    )
    assert m["compatible"] is True
    assert "personal_guarantee" in m["unknown_fields"]


# --- Stacks ---


def test_stack_100_percent_po(fin_tmp):
    _lender_a(max_advance_pct="100", can_fund_freight="YES")
    a = build_capital_stacks(
        opportunity_id="X",
        contract_value=Decimal("100000"),
        supplier_cost=Decimal("70000"),
        freight=Decimal("2000"),
    )
    assert a["best_stack"]["status"] == ST_LIKELY_FINANCEABLE
    assert money(a["best_stack"]["unfunded_gap"]) == Decimal("0")


def test_stack_supplier_only(fin_tmp):
    a = build_capital_stacks(
        opportunity_id="X",
        contract_value=Decimal("50000"),
        supplier_cost=Decimal("30000"),
        freight=Decimal("0"),
        supplier_terms={"available_credit": "30000", "net_days": 30, "verified": True},
    )
    assert money(a["best_stack"]["unfunded_gap"]) == Decimal("0")
    assert a["best_stack"]["status"] in {ST_LIKELY_FINANCEABLE, ST_FINANCING_UNKNOWN}


def test_execution_fail_incompatible(fin_tmp):
    _lender_a(personal_guarantee=PG_REQUIRED)
    a = build_capital_stacks(
        opportunity_id="X",
        contract_value=Decimal("180000"),
        supplier_cost=Decimal("120000"),
        freight=Decimal("5000"),
        supplier_terms={},
    )
    assert a["best_stack"]["status"] == ST_EXECUTION_FAIL
    assert any("pg_" in b for b in (a["best_stack"].get("blockers") or []))


def test_financing_unknown_without_sources(fin_tmp):
    a = assess_opportunity_financing(
        opportunity_id="U",
        contract_value="180000",
        supplier_cost="120000",
        freight="5000",
        persist=False,
    )
    assert a["card"]["status"] == ST_FINANCING_UNKNOWN


# --- Economics ---


def test_fees_reduce_profit(fin_tmp):
    cost = estimate_financing_cost(financed_amount=Decimal("100000"), rules={"fee_pct_of_advance": "5"})
    assert cost == Decimal("5000.00")
    _lender_a(max_advance_pct="100", fee_pct_of_advance="5", can_fund_freight="YES")
    a = build_capital_stacks(
        opportunity_id="E",
        contract_value=Decimal("150000"),
        supplier_cost=Decimal("100000"),
        freight=Decimal("0"),
    )
    assert money(a["best_stack"]["estimated_financing_cost"]) == Decimal("5000.00")
    assert money(a["best_stack"]["profit_after_financing"]) == money(a["best_stack"]["profit_before_financing"]) - Decimal(
        "5000.00"
    )


# --- Evidence / facts gate ---


def test_raw_notes_do_not_auto_activate(fin_tmp):
    src = upsert_source({"company_name": "TradeCap", "source_type": SRC_PO_FINANCE})
    ingest_call_notes(
        raw_notes=(
            "Spoke to Mike. They like gov POs above $100k. Said they can usually cover the whole "
            "supplier invoice if the margin is 20%+. No hard credit pull initially. PG depends on deal. "
            "Supplier has to be established. About 3 days underwriting. They pay supplier directly."
        ),
        source_id=src["source_id"],
        company_name="TradeCap",
        contact="Mike",
    )
    proposed = list_facts(status=FACT_PROPOSED)
    assert proposed
    # Proposed facts must not yet be APPROVED
    assert not any(f.get("status") == FACT_APPROVED for f in list_facts(source_id=src["source_id"]))
    decide_fact(proposed[0]["fact_id"], decision="REJECT")
    assert list_facts(status=FACT_REJECTED)
    remaining = list_facts(status=FACT_PROPOSED)
    if remaining:
        decide_fact(remaining[0]["fact_id"], decision="APPROVE")
        rules = approved_rules_for_source(src["source_id"])
        assert remaining[0]["field"] in rules


def test_extract_notes_fields(fin_tmp):
    facts = extract_proposed_facts_from_notes(
        "gov POs above $100k. whole supplier invoice if margin is 20%+. no hard credit pull. PG depends. pay supplier directly. 3 days underwriting."
    )
    fields = {f["field"] for f in facts}
    assert "pays_supplier_directly" in fields
    assert "minimum_gross_margin_pct" in fields or "preferred_deal_size_min" in fields


# --- Opportunity integration ---


def test_attach_without_cost_is_unknown(fin_tmp):
    row = {"canonical_id": "Z", "estimated_value": 100000}
    attach_financing_to_opportunity_row(row)
    assert row["financing_status"] == ST_FINANCING_UNKNOWN
    assert row.get("financing_rank_penalty") == 0


def test_available_capital_alone_does_not_close(fin_tmp):
    _lender_a(max_advance_pct="50")
    update_capital(business_cash="100000")
    r = assess_opportunity_financing(
        opportunity_id="NC",
        contract_value="180000",
        supplier_cost="120000",
        freight="5000",
        supplier_terms={},
    )
    assert r["card"]["status"] == ST_CAPITAL_CONFIRMATION_REQUIRED
    assert r["card"]["status"] != ST_CAPITAL_STACK_CLOSED


def test_evidence_hierarchy_constants():
    from financing_intelligence.constants import EVIDENCE_RANK

    assert EVIDENCE_RANK[EV_COMPLETED_TRANSACTION] > EVIDENCE_RANK[EV_MARKETING_CLAIM]

"""Funding source production seed + verification queue tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from funding_path_constants import (
    CALL_DO_NOT_CALL_NOW,
    CALL_NOW,
    EV_INFERRED,
    EV_STALE,
    EV_VERIFIED_PUBLIC,
    FIT_FUTURE_MATCH,
    FIT_NEEDS_VERIFICATION,
    FIT_REJECT_NOW,
    PG_CONDITIONAL,
    PG_NOT_REQUIRED,
    PG_REQUIRED,
    RESALE_STRONG,
    ROLE_BROKER,
    ROLE_DIRECT_FUNDER,
)
from funding_path_intelligence import assess_government_contract_financing, build_operator_call_sheet
from funding_source_kb import (
    criterion_evidence,
    empty_funding_source_profile,
    interpret_pg_marketing,
    mark_stale_criteria,
    production_knowledge_base_seed,
    set_criterion,
)
from funding_source_research import (
    apply_call_result_to_criteria,
    assign_call_priority,
    build_call_now_queue,
    build_production_funding_kb,
    call_result_entry_template,
    classify_launch_fit,
    evaluate_zero_cash,
    export_funding_source_research,
    government_financing_verification_action,
    supplier_funding_verification_queue,
)


def test_separate_noble_programs():
    kb = build_production_funding_kb()
    noble = [p for p in kb["programs"] if p["organization_name"] == "Noble Funding"]
    assert len(noble) >= 2
    names = {p["program_name"] for p in noble}
    assert any("PO" in n or "Work Order" in n for n in names)
    assert any("Contract Financing" in n for n in names)
    mins = {p.get("minimum_funding_amount") or p.get("minimum_transaction") for p in noble}
    assert 4000000 in mins
    assert 300000 in mins


def test_direct_funder_broker_distinction():
    kb = build_production_funding_kb()
    roles = {p["funding_provider_role"] for p in kb["programs"]}
    assert ROLE_DIRECT_FUNDER in roles
    assert ROLE_BROKER in roles
    crest = next(p for p in kb["programs"] if p["organization_name"] == "Crestmont Capital")
    assert crest["funding_provider_role"] == ROLE_BROKER
    # Broker typical thresholds not encoded as verified underwriting mins
    assert crest.get("minimum_margin_pct") is None or not (
        (crest.get("criteria_evidence") or {}).get("minimum_margin_pct", {}).get("verification_status")
        == EV_VERIFIED_PUBLIC
    )


def test_official_evidence_verified_public():
    kb = build_production_funding_kb()
    ccc = next(p for p in kb["programs"] if "Commerce" in p["organization_name"] and "Purchase Order" in p["program_name"])
    ev = ccc["criteria_evidence"]["minimum_transaction"]
    assert ev["verification_status"] == EV_VERIFIED_PUBLIC
    assert ev["value"] == 100000
    assert ev["source_url"]
    assert "100,000" in (ev["source_quote_or_summary"] or "")


def test_third_party_discovery_not_verified_underwriting():
    kb = build_production_funding_kb()
    drip = next(p for p in kb["programs"] if "Drip" in p["organization_name"])
    assert drip.get("minimum_transaction") is None
    assert not any(
        isinstance(ev, dict) and ev.get("verification_status") == EV_VERIFIED_PUBLIC and ev.get("criterion_name") == "minimum_transaction"
        for ev in (drip.get("criteria_evidence") or {}).values()
    )


def test_no_pg_options_conditional():
    ev = interpret_pg_marketing("No personal guarantee options available for qualified borrowers")
    assert ev["value"] == PG_CONDITIONAL
    assert ev["value"] != PG_NOT_REQUIRED
    kb = build_production_funding_kb()
    noble = next(p for p in kb["programs"] if "Junior Capital" in p["program_name"] or "Contract Financing (Junior" in p["program_name"])
    assert noble["personal_guarantee"] == PG_CONDITIONAL


def test_verified_pg_cash_credit_reject_now():
    p = empty_funding_source_profile()
    p = set_criterion(
        p,
        "personal_guarantee",
        PG_REQUIRED,
        criterion_evidence(
            criterion_name="personal_guarantee",
            value=PG_REQUIRED,
            verification_status=EV_VERIFIED_PUBLIC,
            verified_at="2026-09-15T00:00:00+00:00",
        ),
    )
    p["personal_guarantee"] = PG_REQUIRED
    assert classify_launch_fit(p)["launch_fit"] == FIT_REJECT_NOW

    p2 = empty_funding_source_profile()
    p2 = set_criterion(
        p2,
        "borrower_cash_contribution_required",
        True,
        criterion_evidence(
            criterion_name="borrower_cash_contribution_required",
            value=True,
            verification_status=EV_VERIFIED_PUBLIC,
            verified_at="2026-09-15T00:00:00+00:00",
        ),
    )
    p2["borrower_cash_contribution_required"] = True
    assert classify_launch_fit(p2)["launch_fit"] == FIT_REJECT_NOW

    p3 = empty_funding_source_profile()
    p3 = set_criterion(
        p3,
        "personal_credit_used_for_approval",
        True,
        criterion_evidence(
            criterion_name="personal_credit_used_for_approval",
            value=True,
            verification_status=EV_VERIFIED_PUBLIC,
            verified_at="2026-09-15T00:00:00+00:00",
        ),
    )
    p3["personal_credit_used_for_approval"] = True
    assert classify_launch_fit(p3)["launch_fit"] == FIT_REJECT_NOW


def test_one_year_tib_and_revenue_reject_launch():
    kb = build_production_funding_kb()
    ccc = next(p for p in kb["programs"] if p["organization_name"] == "Commerce Commercial Credit")
    assert ccc["launch_fit"] == FIT_REJECT_NOW
    fcc = next(p for p in kb["programs"] if p["program_name"] == "Purchase Order Financing" and "1st" in p["organization_name"])
    assert fcc["launch_fit"] == FIT_REJECT_NOW
    noble_cf = next(p for p in kb["programs"] if "Junior Capital" in p["program_name"])
    assert noble_cf["launch_fit"] == FIT_REJECT_NOW  # $5M revenue


def test_four_million_minimum_rejects_or_future():
    kb = build_production_funding_kb()
    noble_po = next(p for p in kb["programs"] if "Work Order" in p["program_name"] or "PO / Work" in p["program_name"])
    assert noble_po["launch_fit"] in {FIT_REJECT_NOW, FIT_FUTURE_MATCH}
    assert (noble_po.get("minimum_funding_amount") or noble_po.get("minimum_transaction")) == 4000000


def test_unknown_pg_and_cash_needs_verification():
    p = empty_funding_source_profile()
    p["path_hints_transaction_based"] = True
    p["product_resale_fit"] = RESALE_STRONG
    fit = classify_launch_fit(p)
    assert fit["launch_fit"] == FIT_NEEDS_VERIFICATION
    assert fit["unknowns"]["personal_guarantee"] == "UNKNOWN"
    assert fit["unknowns"]["borrower_cash_contribution_required"] == "UNKNOWN"


def test_zero_cash_gap_and_full_coverage():
    g90 = evaluate_zero_cash(coverage_pct=90, supplier_cost=100000)
    assert g90["borrower_gap_required"] == 10000
    assert g90["zero_cash_execution_possible"] is False
    assert g90["status"] == "HARD_FAIL_GAP"
    g100 = evaluate_zero_cash(coverage_pct=100, supplier_cost=100000)
    assert g100["borrower_gap_required"] == 0
    assert g100["zero_cash_execution_possible"] is True
    unk = evaluate_zero_cash(coverage_pct=None)
    assert unk["status"] == "NEEDS_VERIFICATION"


def test_product_resale_and_first_contract_unknown():
    kb = build_production_funding_kb()
    strong = [p for p in kb["programs"] if p.get("product_resale_fit") == RESALE_STRONG]
    assert strong
    elint = next(p for p in kb["programs"] if p["organization_name"] == "Elint Capital")
    assert elint["critical_unknown_matrix"]["first_government_contract_allowed"] == "UNKNOWN"


def test_call_now_excludes_rejects_and_questions_order():
    kb = build_production_funding_kb()
    for p in kb["programs"]:
        if p["launch_fit"] == FIT_REJECT_NOW:
            assert p["call_priority"] == CALL_DO_NOT_CALL_NOW
    queue = build_call_now_queue(kb)
    assert queue
    assert all(q.get("approved") is False and q.get("funding_secured") is False for q in queue)
    sheet = queue[0]["call_sheet"]
    keys = [q["key"] for q in sheet["questions"][:3]]
    assert keys == ["first_gov_product_resale", "zero_cash_no_pg", "transaction_vs_personal_credit"]
    assert sheet["preferred_contact_name"] is None
    assert sheet["named_contact_invented"] is False
    assert "who_to_ask_for" in sheet or queue[0]["who_to_ask_for"]


def test_verified_not_reasked_stale_reasked():
    kb = build_production_funding_kb()
    elint = next(p for p in kb["programs"] if p["organization_name"] == "Elint Capital")
    from funding_path_intelligence import build_funding_requirement

    req = build_funding_requirement(
        opportunity={"government_customer_type": "federal", "product_category": "equipment"},
        economics={"estimated_bid_value": 185000, "estimated_supplier_cost": 132000},
        extras={"cash_required_before_delivery": 132000},
    )
    sheet = build_operator_call_sheet(requirement=req, source=elint)
    keys = {q["key"] for q in sheet["questions"]}
    assert "personal_guarantee" not in keys or "personal_guarantee" in sheet.get("skipped_verified_questions", [])
    # stale
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    elint["criteria_evidence"]["personal_guarantee"]["reverification_due_at"] = past
    stale = mark_stale_criteria(elint)
    assert stale["criteria_evidence"]["personal_guarantee"]["verification_status"] == EV_STALE
    sheet2 = build_operator_call_sheet(requirement=req, source=stale)
    assert "personal_guarantee" in {q["key"] for q in sheet2["questions"]}


def test_call_result_template_and_notes():
    tmpl = call_result_entry_template()
    assert "PG" in tmpl["template_fields"]
    assert tmpl["field_to_criterion"]["PG"] == "personal_guarantee"
    applied = apply_call_result_to_criteria(
        {
            "SOURCE": "Elint Capital",
            "PERSON_SPOKEN_TO": "Alex",
            "PG": "NOT_REQUIRED",
            "OTHER_NOTES": "verbatim free-form notes retained",
        }
    )
    assert applied["raw_operator_notes"] == "verbatim free-form notes retained"
    assert applied["auto_verified"] is False
    assert applied["proposed_criteria"]["personal_guarantee"]["requires_confirmation"] is True


def test_supplier_queue_and_gov_financing():
    sq = supplier_funding_verification_queue(supplier_name="Acme Dist")
    assert any("direct payment" in q.lower() for q in sq["questions"])
    g = government_financing_verification_action()
    assert g["availability"] == "NEEDS_VERIFICATION"
    assert assess_government_contract_financing()["availability"] == "NEEDS_VERIFICATION"


def test_export_contains_evidence_urls_and_no_approval():
    result = export_funding_source_research()
    assert result["program_count"] >= 15
    data = production_knowledge_base_seed()
    assert data["programs"]
    assert data["OpenAI"] == 0 and data["SAM"] == 0
    # CSV row evidence
    import csv
    from pathlib import Path

    csv_path = Path(result["csv_path"])
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert any(r.get("evidence_urls") for r in rows)
    assert all(r.get("approved") in {"False", False} or r["approved"] == "False" for r in rows)


def test_ranking_never_approved_no_apis():
    kb = build_production_funding_kb()
    assert kb["applications_submitted"] == 0
    assert kb["OpenAI"] == 0 and kb["SAM"] == 0 and kb["USAspending"] == 0 and kb["paid"] == 0
    assert all(p.get("approved") is False and p.get("funding_secured") is False for p in kb["programs"])
    assert 15 <= kb["counts"]["programs"] <= 25


def test_star_100_pct_coverage_zero_cash_component():
    kb = build_production_funding_kb()
    star = next(p for p in kb["programs"] if p["organization_name"] == "STAR Funding")
    assert star["maximum_supplier_cost_coverage_pct"] == 100
    zc = evaluate_zero_cash(coverage_pct=100)
    assert zc["zero_cash_execution_possible"] is True

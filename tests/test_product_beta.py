"""Near-final product beta tests — zero live APIs."""

from __future__ import annotations

from types import SimpleNamespace

from data_integrity import STATUS_VERIFIED, unknown_fact, verified_fact
from deal_engine import (
    DECISION_BID,
    DECISION_NEEDS_RESEARCH,
    DECISION_REJECT,
    DECISION_WATCH,
    build_deal_economics,
    compute_deal_score,
    decide_product_deal,
    evaluate_financing_execution_gate,
    evaluate_portfolio_capacity,
    gross_retention_policy,
)
from economic_integrity import COST_NOT_APPLICABLE, COST_REQUIRED_UNKNOWN, COST_VERIFIED, cost_item
from opportunity_source import SOURCE_FEDERAL, list_sources, normalize_sam_raw
from product_deal import FIT_CORE_PRODUCT, FIT_SECONDARY_SERVICE, resolve_core_fit
from product_discovery import (
    classify_product_discovery_hit,
    naics_product_indicator,
    preflight_product_sync,
    psc_product_indicator,
    run_product_sync,
)
from product_matching import (
    MATCH_COMPLIANT_EQUAL,
    MATCH_EXACT,
    MATCH_NONCOMPLIANT,
    MATCH_POSSIBLE,
    MATCH_UNKNOWN,
    match_product_to_requirements,
)
from stage3_engine import stage3_preflight


def test_product_discovery_filtering_naics_psc():
    assert naics_product_indicator("423830")["indicator"] == "PRODUCT_SECTOR"
    assert naics_product_indicator("561730")["indicator"] == "NON_PRODUCT_OR_UNKNOWN"
    assert psc_product_indicator("7025")["indicator"] == "POSSIBLE_PRODUCT"
    assert psc_product_indicator("S208")["indicator"] == "LIKELY_SERVICE"
    assert psc_product_indicator(None)["indicator"] == "UNKNOWN"


def test_product_discovery_hit_classification():
    hit = classify_product_discovery_hit(
        {
            "title": "Industrial Tools and Hardware Supplies",
            "naics_code": "423710",
            "classificationCode": "5120",
            "notice_id": "n-product-1",
            "sam_raw": {"classificationCode": "5120", "naicsCode": "423710"},
        }
    )
    assert hit["core_fit"] == FIT_CORE_PRODUCT or hit["is_product_candidate"] is True
    svc = classify_product_discovery_hit(
        {
            "title": "Groundskeeping Services",
            "naics_code": "561730",
            "classificationCode": "S208",
            "notice_id": "n-svc-1",
            "sam_raw": {"classificationCode": "S208"},
        }
    )
    assert svc["core_fit"] == FIT_SECONDARY_SERVICE
    assert svc["is_product_candidate"] is False


def test_source_normalization_sam():
    norm = normalize_sam_raw(
        {
            "noticeId": "abc123",
            "title": "Office Furniture",
            "naicsCode": "423210",
            "classificationCode": "7110",
            "solicitationNumber": "SOL-1",
            "typeOfSetAsideDescription": "Total Small Business",
            "responseDeadLine": "12/31/2026",
        }
    )
    assert norm.source == SOURCE_FEDERAL
    assert norm.source_opportunity_id == "abc123"
    assert norm.psc_code == "7110"
    fields = norm.to_gt_contract_fields()
    assert fields["notice_id"] == "abc123"
    assert "sam_raw" in fields


def test_source_registry():
    names = [s["source_name"] for s in list_sources()]
    assert "sam_gov_federal" in names


def test_discovery_preflight_no_live():
    pre = preflight_product_sync(max_naics=2)
    assert pre["LIVE_API_REQUESTS"] == 0
    assert pre["would_execute_live"] is False
    blocked = run_product_sync(authorize_live=False)
    assert blocked.get("executed") is False


def test_product_first_priority():
    assert resolve_core_fit(stage1_category="PRODUCT_RESELL")["core_fit"] == FIT_CORE_PRODUCT
    assert resolve_core_fit(stage1_category="LABOR_HEAVY")["core_fit"] == FIT_SECONDARY_SERVICE


def test_matching_exact_brand_only_and_auth():
    reqs = {
        "part_model_number": verified_fact("DCD791", source_type="S", source_field="m"),
        "brand": verified_fact("DeWalt", source_type="S", source_field="b"),
        "brand_name_only": verified_fact(True, source_type="S", source_field="bo"),
        "approved_equal_language": unknown_fact(source_field="eq"),
        "manufacturer_authorization_requirement": verified_fact(
            True, source_type="S", source_field="auth"
        ),
    }
    exact = match_product_to_requirements(
        product={"model": "DCD791", "brand": "DeWalt", "authorized_dealer": True},
        product_requirements=reqs,
    )
    assert exact["match_class"] == MATCH_EXACT
    assert exact["bid_ready"] is True

    bad_brand = match_product_to_requirements(
        product={"model": "DCD791", "brand": "Other", "authorized_dealer": True},
        product_requirements=reqs,
    )
    assert bad_brand["match_class"] == MATCH_NONCOMPLIANT

    auth_unknown = match_product_to_requirements(
        product={"model": "DCD791", "brand": "DeWalt"},
        product_requirements=reqs,
    )
    assert auth_unknown["authorization_gate"] == "UNRESOLVED"
    assert auth_unknown["bid_ready"] is False


def test_matching_or_equal_compliant():
    reqs = {
        "part_model_number": verified_fact("ABC", source_type="S", source_field="m"),
        "brand": verified_fact("BrandX", source_type="S", source_field="b"),
        "brand_name_only": unknown_fact(source_field="bo"),
        "approved_equal_language": verified_fact(True, source_type="S", source_field="eq"),
        "manufacturer_authorization_requirement": unknown_fact(source_field="a"),
    }
    m = match_product_to_requirements(
        product={
            "model": "XYZ",
            "brand": "BrandX",
            "salient_characteristics_verified": True,
        },
        product_requirements=reqs,
    )
    assert m["match_class"] == MATCH_COMPLIANT_EQUAL
    assert m["bid_ready"] is True


def test_financing_gates():
    assert evaluate_financing_execution_gate(term_verified=False)["status"] in {
        "UNKNOWN",
        "NEEDS_VERIFICATION",
    }
    net30 = evaluate_financing_execution_gate(
        financing_term={"advance_structure": "NET-30"},
        term_verified=False,
    )
    assert "net30" in " ".join(net30["reasons"]).lower() or net30["status"] != "PASS"

    pg_fail = evaluate_financing_execution_gate(
        financing_term={"pg_required": True, "personal_credit_required": False, "cash_deposit_required": False},
        term_verified=True,
    )
    assert pg_fail["status"] == "NEEDS_VERIFICATION"
    assert pg_fail.get("operator_pg_review_required") is True

    credit_fail = evaluate_financing_execution_gate(
        financing_term={
            "pg_required": False,
            "personal_credit_required": True,
            "personal_credit_materially_disqualifies": True,
            "cash_deposit_required": False,
        },
        term_verified=True,
    )
    assert credit_fail["status"] == "FAIL"

    cash_fail = evaluate_financing_execution_gate(
        financing_term={"pg_required": False, "personal_credit_required": False, "cash_deposit_required": True},
        term_verified=True,
    )
    assert cash_fail["status"] == "FAIL"

    marketing = evaluate_financing_execution_gate(
        financing_term={"is_generic_marketing": True},
        term_verified=True,
    )
    assert marketing["status"] == "FAIL"

    pas = evaluate_financing_execution_gate(
        financing_term={
            "pg_required": False,
            "personal_credit_required": False,
            "cash_deposit_required": False,
            "supplier_payment_mechanics": "Accepts government PO and waits for payment",
            "government_payment_mechanics": "Net government receivable",
            "advance_structure": "PO financing zero upfront",
        },
        term_verified=True,
    )
    assert pas["status"] == "PASS"


def test_actual_profit_and_retention():
    incomplete = build_deal_economics(
        operator_bid_amount=50000,
        bid_amount_verified=True,
        costs={
            "supplier": cost_item(category="supplier", status=COST_REQUIRED_UNKNOWN, required=True),
            "freight": cost_item(category="freight", status=COST_NOT_APPLICABLE, basis="fob"),
            "financing": cost_item(category="financing", status=COST_REQUIRED_UNKNOWN, required=True),
        },
    )
    assert incomplete["actual_profit"] is None
    assert incomplete["actual_profit_status"] == "INCOMPLETE"

    complete = build_deal_economics(
        operator_bid_amount=50000,
        bid_amount_verified=True,
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=30000, required=True),
            "freight": cost_item(category="freight", status=COST_VERIFIED, value=2000, required=True),
            "financing": cost_item(category="financing", status=COST_VERIFIED, value=1000, required=True),
            "installation": cost_item(category="installation", status=COST_NOT_APPLICABLE, basis="product_only"),
            "subcontract": cost_item(category="subcontract", status=COST_NOT_APPLICABLE, basis="product_only"),
            "fees": cost_item(category="fees", status=COST_NOT_APPLICABLE, basis="none"),
        },
    )
    assert complete["actual_profit"] == 17000.0
    assert complete["meets_min_actual_profit"] is True
    ret = gross_retention_policy(50000)
    assert ret["status"] == "POLICY"
    assert ret["retained_gross_reserve"] == 10000.0


def test_decisions_and_hard_gate_overrides_score():
    econ_low = build_deal_economics(
        operator_bid_amount=20000,
        bid_amount_verified=True,
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=15000, required=True),
            "freight": cost_item(category="freight", status=COST_NOT_APPLICABLE, basis="x"),
            "financing": cost_item(category="financing", status=COST_VERIFIED, value=0, required=True),
            "installation": cost_item(category="installation", status=COST_NOT_APPLICABLE, basis="x"),
            "subcontract": cost_item(category="subcontract", status=COST_NOT_APPLICABLE, basis="x"),
            "fees": cost_item(category="fees", status=COST_NOT_APPLICABLE, basis="x"),
        },
    )
    # profit = 5000 < 10k
    assert econ_low["actual_profit"] == 5000.0
    fin_pass = evaluate_financing_execution_gate(
        financing_term={
            "pg_required": False,
            "personal_credit_required": False,
            "cash_deposit_required": False,
            "supplier_payment_mechanics": "PO",
            "government_payment_mechanics": "gov pay",
            "advance_structure": "zero upfront",
        },
        term_verified=True,
    )
    d = decide_product_deal(
        core_fit=FIT_CORE_PRODUCT,
        economics=econ_low,
        financing_gate=fin_pass,
        match_result={"match_class": MATCH_EXACT},
        research_tasks=[],
    )
    assert d["decision"] == DECISION_REJECT
    assert "ACTUAL_PROFIT_BELOW_10K" in d["reason_codes"]

    score = compute_deal_score(economics=econ_low, decision=d, financing_gate=fin_pass)
    assert score["hard_gate_override"] is True
    assert score["score"] == 0

    needs = decide_product_deal(
        core_fit=FIT_CORE_PRODUCT,
        economics=build_deal_economics(operator_bid_amount=None),
        financing_gate=evaluate_financing_execution_gate(),
        research_tasks=[{"code": "CURRENT_SUPPLIER_QUOTE_REQUIRED", "blocking": True, "status": "NOT_STARTED"}],
    )
    assert needs["decision"] == DECISION_NEEDS_RESEARCH

    watch = decide_product_deal(
        core_fit=FIT_SECONDARY_SERVICE,
        economics={},
        financing_gate={},
    )
    assert watch["decision"] == DECISION_WATCH


def test_bid_happy_path_structure():
    econ = build_deal_economics(
        operator_bid_amount=80000,
        bid_amount_verified=True,
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=50000, required=True),
            "freight": cost_item(category="freight", status=COST_VERIFIED, value=3000, required=True),
            "financing": cost_item(category="financing", status=COST_VERIFIED, value=2000, required=True),
            "installation": cost_item(category="installation", status=COST_NOT_APPLICABLE, basis="x"),
            "subcontract": cost_item(category="subcontract", status=COST_NOT_APPLICABLE, basis="x"),
            "fees": cost_item(category="fees", status=COST_NOT_APPLICABLE, basis="x"),
        },
    )
    assert econ["actual_profit"] == 25000.0
    fin = evaluate_financing_execution_gate(
        financing_term={
            "pg_required": False,
            "personal_credit_required": False,
            "cash_deposit_required": False,
            "supplier_payment_mechanics": "Accepts government PO",
            "government_payment_mechanics": "Receivable",
            "advance_structure": "zero upfront no PG",
        },
        term_verified=True,
    )
    d = decide_product_deal(
        core_fit=FIT_CORE_PRODUCT,
        economics=econ,
        financing_gate=fin,
        match_result={"match_class": MATCH_EXACT},
        research_tasks=[],
    )
    assert d["decision"] == DECISION_BID


def test_portfolio_unresolved():
    p = evaluate_portfolio_capacity()
    assert p["portfolio_status"] == "UNRESOLVED"


def test_sibling_products_isolation_in_planner():
    from product_deal import collect_known_knowledge_from_postgres

    try:
        collect_known_knowledge_from_postgres(SimpleNamespace(id=1), allow_sibling_products_table=True)
        assert False
    except ValueError:
        pass


def test_stage3_preflight_no_external():
    opp = SimpleNamespace(id=None, notice_id="t1", pricing_intel=None)
    pre = stage3_preflight(
        opp,
        stage0={"classification": "PRODUCT_RESELL"},
        stage1={"category": "PRODUCT_RESELL"},
        stage2={
            "facts": {
                "scope": {
                    "exact_model": verified_fact("M1", source_type="S", source_field="m"),
                    "quantity": unknown_fact(source_field="q"),
                    "brand_name_or_equal": unknown_fact(source_field="b"),
                    "products_services": unknown_fact(source_field="p"),
                    "summary": unknown_fact(source_field="s"),
                    "unit_of_measure": unknown_fact(source_field="u"),
                    "salient_characteristics": unknown_fact(source_field="sc"),
                },
                "procurement": {"category": {"value": "PRODUCT_RESELL", "status": STATUS_VERIFIED}},
                "execution": {},
                "compliance": {},
                "dates": {},
                "economic_evidence": {},
                "identity": {},
            }
        },
    )
    assert pre["LIVE_API_REQUESTS_IF_EXECUTED_POSTGRES_ONLY"] == 0
    assert pre["authorize_required_for_external"] is True


def test_historical_not_current_revenue():
    from historical_context import extract_historical_from_pricing_intel

    items = extract_historical_from_pricing_intel(
        {"source": "USAspending.gov", "awards": [{"award_id": "A1", "award_amount": 99000}], "fetched_at": "2026-01-01"}
    )
    assert items[0]["temporal"] == "HISTORICAL"
    assert items[0]["amount_fact"].get("is_current_revenue") is False


def test_service_functionality_still_classifies():
    from ai_funnel import CLASS_LABOR_HEAVY, classify_opportunity

    cls, _ = classify_opportunity(
        SimpleNamespace(
            title="Janitorial Services Building A",
            naics_code="561720",
            description="",
            sam_raw={"classificationCode": "S201"},
            notice_id="j1",
            set_aside="Total Small Business",
        )
    )
    assert cls == CLASS_LABOR_HEAVY

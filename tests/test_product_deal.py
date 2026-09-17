"""Tests for product-first deal model. Zero live APIs."""

from __future__ import annotations

from types import SimpleNamespace

from data_integrity import STATUS_ASSESSMENT, STATUS_UNKNOWN, STATUS_VERIFIED, unknown_fact, verified_fact
from economic_integrity import COST_NOT_APPLICABLE, COST_REQUIRED_UNKNOWN, map_cost_requirements_for_canonical_class
from product_deal import (
    FIT_CORE_PRODUCT,
    FIT_SECONDARY_SERVICE,
    FIT_UNKNOWN,
    build_product_requirements_view,
    build_product_research_needs,
    collect_known_knowledge_from_postgres,
    paid_work_priority_rank,
    plan_product_research,
    resolve_core_fit,
)


def test_product_resell_is_core_product():
    fit = resolve_core_fit(stage1_category="PRODUCT_RESELL")
    assert fit["core_fit"] == FIT_CORE_PRODUCT
    assert fit["product_purity"] == "CLEAN"
    assert fit["status"] == "POLICY"
    assert paid_work_priority_rank(fit) < paid_work_priority_rank(
        resolve_core_fit(stage1_category="LABOR_HEAVY")
    )


def test_service_classes_are_secondary():
    for cat in ("LABOR_HEAVY", "SUBCONTRACTABLE_SERVICE", "SPECIALIST_SERVICE", "CONSTRUCTION"):
        fit = resolve_core_fit(stage1_category=cat)
        assert fit["core_fit"] == FIT_SECONDARY_SERVICE, cat


def test_unknown_fit():
    fit = resolve_core_fit(stage1_category="UNKNOWN")
    assert fit["core_fit"] == FIT_UNKNOWN


def test_free_text_cannot_turn_service_into_product():
    fit = resolve_core_fit(
        stage1_category="LABOR_HEAVY",
        stage2_category_value="Industrial Tools and Equipment",
    )
    assert fit["core_fit"] == FIT_SECONDARY_SERVICE
    assert fit["canonical_class"] == "LABOR_HEAVY"


def test_free_text_cannot_turn_product_into_service():
    fit = resolve_core_fit(
        stage1_category="PRODUCT_RESELL",
        stage2_category_value="Grounds Maintenance Services",
    )
    assert fit["core_fit"] == FIT_CORE_PRODUCT
    assert fit["canonical_class"] == "PRODUCT_RESELL"


def test_missing_manufacturer_and_quantity_stay_unknown():
    facts = {
        "scope": {
            "exact_model": verified_fact("ABC-123", source_type="SOLICITATION", source_field="scope.exact_model"),
            "quantity": unknown_fact(source_field="scope.quantity"),
            "brand_name_or_equal": unknown_fact(source_field="scope.brand_name_or_equal"),
            "products_services": unknown_fact(source_field="scope.products_services"),
            "summary": unknown_fact(source_field="scope.summary"),
            "unit_of_measure": unknown_fact(source_field="scope.unit_of_measure"),
            "salient_characteristics": unknown_fact(source_field="scope.salient_characteristics"),
        },
        "execution": {},
        "compliance": {},
        "procurement": {},
        "dates": {},
        "economic_evidence": {},
        "identity": {},
    }
    view = build_product_requirements_view(facts)
    assert view["manufacturer"]["status"] == STATUS_UNKNOWN
    assert view["manufacturer"]["value"] is None
    assert view["quantity"]["status"] == STATUS_UNKNOWN
    assert view["quantity"]["value"] is None
    assert view["part_model_number"]["status"] == STATUS_VERIFIED
    assert view["part_model_number"]["value"] == "ABC-123"


def test_brand_name_or_equal_preserved():
    facts = {
        "scope": {
            "brand_name_or_equal": verified_fact(
                "Brand Name or Equal: DeWalt DCD791",
                source_type="SOLICITATION",
                source_field="scope.brand_name_or_equal",
            ),
            "exact_model": unknown_fact(source_field="scope.exact_model"),
            "quantity": unknown_fact(source_field="scope.quantity"),
            "products_services": unknown_fact(source_field="scope.products_services"),
            "summary": unknown_fact(source_field="scope.summary"),
            "unit_of_measure": unknown_fact(source_field="scope.unit_of_measure"),
            "salient_characteristics": unknown_fact(source_field="scope.salient_characteristics"),
        },
        "execution": {},
        "compliance": {},
        "procurement": {},
        "dates": {},
        "economic_evidence": {},
        "identity": {},
    }
    view = build_product_requirements_view(facts)
    assert view["brand_name_or_equal"]["status"] == STATUS_VERIFIED
    assert "DeWalt" in str(view["brand_name_or_equal"]["value"])
    assert view["approved_equal_language"]["value"] is True


def test_product_economics_clean_resell():
    costs = map_cost_requirements_for_canonical_class(canonical_class="PRODUCT_RESELL")
    assert costs["supplier"]["status"] == COST_REQUIRED_UNKNOWN
    assert costs["freight"]["status"] == COST_REQUIRED_UNKNOWN
    # installation/subcontract UNKNOWN must not silently become N/A
    assert costs["subcontract"]["status"] == COST_REQUIRED_UNKNOWN
    assert costs["subcontract"].get("basis")
    assert costs["installation"]["status"] == COST_REQUIRED_UNKNOWN
    assert costs["installation"].get("basis")
    assert "UNKNOWN" in (costs["installation"].get("basis") or "")
    assert costs["financing"]["status"] == COST_REQUIRED_UNKNOWN


def test_product_resell_install_false_verified_is_na():
    costs = map_cost_requirements_for_canonical_class(
        canonical_class="PRODUCT_RESELL",
        installation_required=False,
    )
    assert costs["installation"]["status"] == COST_NOT_APPLICABLE
    assert costs["subcontract"]["status"] == COST_NOT_APPLICABLE
    assert "false VERIFIED" in (costs["installation"].get("basis") or "")


def test_freight_na_requires_basis_for_service():
    costs = map_cost_requirements_for_canonical_class(canonical_class="LABOR_HEAVY")
    assert costs["freight"]["status"] == COST_NOT_APPLICABLE
    assert costs["freight"].get("basis")


def test_actual_profit_null_with_required_unknown():
    plan = plan_product_research(
        SimpleNamespace(id=None, notice_id="n1", pricing_intel=None),
        stage0={"classification": "PRODUCT_RESELL"},
        stage1={"category": "PRODUCT_RESELL"},
        stage2={
            "facts": {
                "scope": {
                    "exact_model": verified_fact("X1", source_type="SOLICITATION", source_field="s"),
                    "quantity": unknown_fact(source_field="q"),
                    "brand_name_or_equal": unknown_fact(source_field="b"),
                    "products_services": unknown_fact(source_field="p"),
                    "summary": unknown_fact(source_field="sum"),
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
        session=None,
    )
    assert plan["economic_readiness"]["actual_profit"] is None
    assert plan["economic_readiness"]["status"] == "INCOMPLETE"
    assert plan["economic_readiness"]["financing"] == COST_REQUIRED_UNKNOWN
    codes = {t["code"] for t in plan["research_tasks"]}
    assert "CURRENT_SUPPLIER_QUOTE_REQUIRED" in codes
    assert "FINANCING_TERMS_REQUIRED" in codes


def test_historical_quote_cannot_satisfy_current_quote_need():
    needs = build_product_research_needs(
        product_requirements={
            "part_model_number": verified_fact("M1", source_type="S", source_field="m"),
            "brand": unknown_fact(source_field="b"),
            "product_description": unknown_fact(source_field="d"),
            "quantity": verified_fact(10, source_type="S", source_field="q"),
            "manufacturer_authorization_requirement": unknown_fact(source_field="a"),
            "brand_name_only": unknown_fact(source_field="bo"),
            "country_of_origin": unknown_fact(source_field="c"),
            "delivery_destination": verified_fact("Base X", source_type="S", source_field="dd"),
            "required_delivery_date": verified_fact("2026-12-01", source_type="S", source_field="rd"),
        },
        economic_requirements={
            "costs": {
                "supplier": {"status": COST_REQUIRED_UNKNOWN},
                "freight": {"status": COST_REQUIRED_UNKNOWN},
                "financing": {"status": COST_REQUIRED_UNKNOWN},
            }
        },
        known_current=[
            {
                "code": "HISTORICAL_SUPPLIER_QUOTE",
                "status": STATUS_VERIFIED,
                "temporal": "historical",
            }
        ],
        core_fit=FIT_CORE_PRODUCT,
    )
    codes = {n["code"] for n in needs}
    assert "CURRENT_SUPPLIER_QUOTE_REQUIRED" in codes


def test_historical_award_not_current_revenue():
    knowledge = collect_known_knowledge_from_postgres(
        SimpleNamespace(
            id=None,
            notice_id="abc",
            pricing_intel={
                "source": "USAspending.gov",
                "awards": [{"award_id": "A1", "award_amount": 50000}],
                "fetched_at": "2026-01-01",
            },
        ),
        session=None,
    )
    hist = knowledge["known_historical"]
    assert any(h.get("code") == "HISTORICAL_AWARD" for h in hist)
    assert all(h.get("temporal") == "historical" for h in hist if h.get("code") == "HISTORICAL_AWARD")
    assert all("≠ current revenue" in (h.get("notes") or "") for h in hist if h.get("code") == "HISTORICAL_AWARD")


def test_planner_never_uses_sibling_products_table():
    try:
        collect_known_knowledge_from_postgres(
            SimpleNamespace(id=1, notice_id="x"),
            allow_sibling_products_table=True,
        )
        assert False, "should have raised"
    except ValueError as e:
        assert "not authorized" in str(e).lower()


def test_planner_skips_research_when_current_quote_verified():
    needs = build_product_research_needs(
        product_requirements={
            "part_model_number": verified_fact("M1", source_type="S", source_field="m"),
            "brand": verified_fact("Brand", source_type="S", source_field="b"),
            "product_description": verified_fact("Widget", source_type="S", source_field="d"),
            "quantity": verified_fact(5, source_type="S", source_field="q"),
            "manufacturer_authorization_requirement": unknown_fact(source_field="a"),
            "brand_name_only": unknown_fact(source_field="bo"),
            "country_of_origin": unknown_fact(source_field="c"),
            "delivery_destination": verified_fact("Site", source_type="S", source_field="dd"),
            "required_delivery_date": verified_fact("2026-11-01", source_type="S", source_field="rd"),
        },
        economic_requirements={
            "costs": {
                "supplier": {"status": COST_REQUIRED_UNKNOWN},
                "freight": {"status": COST_NOT_APPLICABLE, "basis": "FOB destination included"},
                "financing": {"status": COST_REQUIRED_UNKNOWN},
            }
        },
        known_current=[
            {"code": "CURRENT_SUPPLIER_QUOTE", "status": STATUS_VERIFIED, "value": 1000},
        ],
        core_fit=FIT_CORE_PRODUCT,
    )
    codes = {n["code"] for n in needs}
    assert "CURRENT_SUPPLIER_QUOTE_REQUIRED" not in codes
    assert "SUPPLIER_AVAILABILITY_REQUIRED" not in codes


def test_assessment_not_promoted_in_requirements_view():
    facts = {
        "scope": {
            "quantity": {
                "value": 100,
                "status": STATUS_ASSESSMENT,
                "source_type": "AI",
                "source_field": "scope.quantity",
            },
            "exact_model": unknown_fact(source_field="scope.exact_model"),
            "brand_name_or_equal": unknown_fact(source_field="scope.brand_name_or_equal"),
            "products_services": unknown_fact(source_field="scope.products_services"),
            "summary": unknown_fact(source_field="scope.summary"),
            "unit_of_measure": unknown_fact(source_field="scope.unit_of_measure"),
            "salient_characteristics": unknown_fact(source_field="scope.salient_characteristics"),
        },
        "execution": {},
        "compliance": {},
        "procurement": {},
        "dates": {},
        "economic_evidence": {},
        "identity": {},
    }
    view = build_product_requirements_view(facts)
    assert view["quantity"]["status"] == STATUS_ASSESSMENT
    assert view["quantity"]["value"] == 100

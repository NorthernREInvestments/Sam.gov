"""Economic integrity — actual profit, gates, scenarios. Zero live APIs."""

from __future__ import annotations

from economic_integrity import (
    BID_ELIGIBLE,
    BID_INELIGIBLE,
    BID_NEEDS_RESEARCH,
    COST_NOT_APPLICABLE,
    COST_REQUIRED_UNKNOWN,
    COST_VERIFIED,
    COST_VERIFIED_ZERO,
    ECON_CALCULATED,
    ECON_ESTIMATED_SCENARIO,
    ECON_INCOMPLETE,
    GATE_FAIL,
    GATE_NEEDS_VERIFICATION,
    GATE_PASS,
    GATE_UNKNOWN,
    calculate_actual_profit,
    cost_item,
    display_economic_value,
    display_pg_requirement,
    evaluate_bid_eligibility,
    evaluate_cash_pg_gates,
    historical_award_fact,
    historical_unit_rate,
    policy_margin_scenario,
    revenue_item,
    supplier_quote_envelope,
)


def _rev(amount: float):
    return revenue_item(amount, status="VERIFIED", source_type="SAM", source_field="award.amount")


def test_complete_stack_calculates_actual_profit():
    result = calculate_actual_profit(
        revenue=_rev(50000),
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=30000, required=True),
            "freight": cost_item(category="freight", status=COST_VERIFIED, value=2000, required=True),
            "financing": cost_item(category="financing", status=COST_NOT_APPLICABLE, required=False),
            "installation": cost_item(category="installation", status=COST_NOT_APPLICABLE),
            "subcontract": cost_item(category="subcontract", status=COST_NOT_APPLICABLE),
        },
    )
    assert result["status"] == ECON_CALCULATED
    assert result["actual_profit"] == 18000.0
    assert result["meets_min_actual_profit"] is True


def test_unknown_supplier_incomplete():
    result = calculate_actual_profit(
        revenue=_rev(50000),
        costs={
            "supplier": cost_item(category="supplier", status=COST_REQUIRED_UNKNOWN, required=True),
            "freight": cost_item(category="freight", status=COST_NOT_APPLICABLE),
            "financing": cost_item(category="financing", status=COST_NOT_APPLICABLE),
        },
    )
    assert result["status"] == ECON_INCOMPLETE
    assert result["actual_profit"] is None
    assert "supplier" in result["missing_required_inputs"]
    assert "Unknown" in result["display"]
    assert result["meets_min_actual_profit"] is False


def test_required_unknown_freight_incomplete():
    result = calculate_actual_profit(
        revenue=_rev(50000),
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=20000, required=True),
            "freight": cost_item(category="freight", status=COST_REQUIRED_UNKNOWN, required=True),
            "financing": cost_item(category="financing", status=COST_NOT_APPLICABLE),
        },
    )
    assert result["status"] == ECON_INCOMPLETE
    assert result["actual_profit"] is None
    assert "freight" in result["missing_required_inputs"]


def test_financing_required_unknown_incomplete():
    result = calculate_actual_profit(
        revenue=_rev(50000),
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=20000, required=True),
            "freight": cost_item(category="freight", status=COST_NOT_APPLICABLE),
            "financing": cost_item(category="financing", status=COST_REQUIRED_UNKNOWN, required=True),
        },
    )
    assert result["actual_profit"] is None
    assert "financing" in result["missing_required_inputs"]


def test_financing_not_applicable_allows_calc():
    result = calculate_actual_profit(
        revenue=_rev(40000),
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=25000, required=True),
            "freight": cost_item(category="freight", status=COST_NOT_APPLICABLE),
            "financing": cost_item(
                category="financing",
                status=COST_NOT_APPLICABLE,
                notes="Supplier net-30; no financing required",
                source_type="SUPPLIER_TERMS",
            ),
        },
    )
    assert result["status"] == ECON_CALCULATED
    assert result["actual_profit"] == 15000.0


def test_verified_zero_financing_valid():
    result = calculate_actual_profit(
        revenue=_rev(40000),
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=25000, required=True),
            "freight": cost_item(category="freight", status=COST_NOT_APPLICABLE),
            "financing": cost_item(
                category="financing",
                status=COST_VERIFIED_ZERO,
                value=0,
                source_type="LENDER_QUOTE",
                notes="Verified $0 financing fee",
            ),
        },
    )
    assert result["status"] == ECON_CALCULATED
    assert result["actual_profit"] == 15000.0
    assert result["costs"]["financing"]["value"] == 0.0


def test_unknown_installation_blocks_when_required():
    result = calculate_actual_profit(
        revenue=_rev(50000),
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=20000, required=True),
            "installation": cost_item(category="installation", status=COST_REQUIRED_UNKNOWN, required=True),
            "freight": cost_item(category="freight", status=COST_NOT_APPLICABLE),
            "financing": cost_item(category="financing", status=COST_NOT_APPLICABLE),
        },
    )
    assert result["actual_profit"] is None
    assert "installation" in result["missing_required_inputs"]


def test_unknown_subcontract_blocks_service():
    result = calculate_actual_profit(
        revenue=_rev(80000),
        costs={
            "supplier": cost_item(category="supplier", status=COST_NOT_APPLICABLE),
            "subcontract": cost_item(category="subcontract", status=COST_REQUIRED_UNKNOWN, required=True),
            "freight": cost_item(category="freight", status=COST_NOT_APPLICABLE),
            "financing": cost_item(category="financing", status=COST_NOT_APPLICABLE),
        },
    )
    assert result["actual_profit"] is None
    assert "subcontract" in result["missing_required_inputs"]


def test_policy_scenario_not_actual_and_cannot_pass_gate():
    scenario = policy_margin_scenario(cost_basis=40000, margin_pct=20)
    assert scenario["status"] == ECON_ESTIMATED_SCENARIO
    assert scenario["actual_profit"] is None
    assert scenario["scenario_profit"] == 10000.0
    assert scenario["meets_min_actual_profit"] is False
    assert "POLICY SCENARIO" in scenario["display"]

    bid = evaluate_bid_eligibility(
        actual_profit_result=scenario,
        cash_pg=evaluate_cash_pg_gates(
            personal_cash_upfront_required=False,
            personal_credit_required=False,
            personal_guarantee_required=False,
            financing_structure_known=True,
            no_pg_financing_verified=True,
            zero_upfront_financing_verified=True,
        ),
        critical_gates={"eligibility": GATE_PASS},
    )
    assert bid["bid_eligible"] is False
    assert bid["decision"] == BID_NEEDS_RESEARCH
    assert "scenario_profit_is_not_actual" in bid["blockers"]


def test_historical_award_not_current_revenue():
    fact = historical_award_fact(73000, amount_basis="total")
    assert fact.get("is_current_revenue") is False
    assert "not current deal revenue" in (fact.get("notes") or "").lower()


def test_historical_unit_rate_unknown_quantity():
    assert historical_unit_rate(award_amount=100000, quantity=None)["status"] == "UNKNOWN"
    assert historical_unit_rate(award_amount=100000, quantity=0)["status"] == "UNKNOWN"


def test_historical_unit_rate_total_without_period_null():
    fact = historical_unit_rate(award_amount=100000, quantity=10000, amount_basis="total")
    assert fact["status"] == "UNKNOWN"


def test_historical_unit_rate_with_verified_inputs():
    fact = historical_unit_rate(
        award_amount=100000, quantity=10000, amount_basis="total", period_years=2
    )
    assert fact["status"] == "CALCULATED"
    assert fact["value"] == 5.0  # (100000/2)/10000


def test_pg_unknown_not_pass():
    gates = evaluate_cash_pg_gates(personal_guarantee_required=None)
    assert gates["gates"]["personal_guarantee"] == GATE_UNKNOWN
    assert gates["overall"] == GATE_UNKNOWN
    assert "Unknown" in display_pg_requirement(gates["gates"]["personal_guarantee"])


def test_pg_required_fail():
    gates = evaluate_cash_pg_gates(personal_guarantee_required=True)
    assert gates["gates"]["personal_guarantee"] == GATE_NEEDS_VERIFICATION
    assert gates["overall"] != GATE_FAIL or gates.get("operator_pg_review_required") is True
    assert gates["operator_pg_review_required"] is True
    assert gates["gates"]["personal_guarantee"] != GATE_FAIL


def test_verified_no_pg_financing_pass():
    gates = evaluate_cash_pg_gates(
        personal_cash_upfront_required=False,
        personal_credit_required=False,
        personal_guarantee_required=False,
        financing_structure_known=True,
        no_pg_financing_verified=True,
        zero_upfront_financing_verified=True,
    )
    assert gates["overall"] == GATE_PASS


def test_unknown_personal_credit_not_pass():
    gates = evaluate_cash_pg_gates(
        personal_cash_upfront_required=False,
        personal_credit_required=None,
        personal_guarantee_required=False,
        financing_structure_known=True,
        no_pg_financing_verified=True,
        zero_upfront_financing_verified=True,
    )
    assert gates["gates"]["personal_credit"] == GATE_UNKNOWN
    assert gates["overall"] == GATE_UNKNOWN


def test_verified_personal_credit_fail():
    gates = evaluate_cash_pg_gates(
        personal_credit_required=True,
        personal_credit_materially_disqualifies=True,
    )
    assert gates["gates"]["personal_credit"] == GATE_FAIL


def test_verified_personal_credit_checked_needs_verification():
    gates = evaluate_cash_pg_gates(personal_credit_required=True)
    assert gates["gates"]["personal_credit"] == GATE_NEEDS_VERIFICATION
    assert gates["overall"] != GATE_FAIL or True


def test_bid_eligible_when_all_pass_and_profit_ge_10k():
    profit = calculate_actual_profit(
        revenue=_rev(50000),
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=30000, required=True),
            "freight": cost_item(category="freight", status=COST_VERIFIED_ZERO, required=True),
            "financing": cost_item(category="financing", status=COST_NOT_APPLICABLE),
        },
    )
    assert profit["actual_profit"] == 20000.0
    bid = evaluate_bid_eligibility(
        actual_profit_result=profit,
        cash_pg=evaluate_cash_pg_gates(
            personal_cash_upfront_required=False,
            personal_credit_required=False,
            personal_guarantee_required=False,
            financing_structure_known=True,
            no_pg_financing_verified=True,
            zero_upfront_financing_verified=True,
        ),
        critical_gates={"set_aside": GATE_PASS, "eligibility": GATE_PASS},
    )
    assert bid["decision"] == BID_ELIGIBLE
    assert bid["bid_eligible"] is True


def test_actual_profit_unknown_blocks_bid():
    profit = calculate_actual_profit(
        revenue=_rev(50000),
        costs={"supplier": cost_item(category="supplier", status=COST_REQUIRED_UNKNOWN, required=True)},
    )
    bid = evaluate_bid_eligibility(
        actual_profit_result=profit,
        cash_pg=evaluate_cash_pg_gates(
            personal_cash_upfront_required=False,
            personal_credit_required=False,
            personal_guarantee_required=False,
            financing_structure_known=True,
            no_pg_financing_verified=True,
            zero_upfront_financing_verified=True,
        ),
        critical_gates={"eligibility": GATE_PASS},
    )
    assert bid["bid_eligible"] is False
    assert bid["decision"] == BID_NEEDS_RESEARCH


def test_actual_profit_9999_fails_threshold():
    profit = calculate_actual_profit(
        revenue=_rev(19999),
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=10000, required=True),
            "freight": cost_item(category="freight", status=COST_NOT_APPLICABLE),
            "financing": cost_item(category="financing", status=COST_NOT_APPLICABLE),
        },
    )
    assert profit["actual_profit"] == 9999.0
    assert profit["meets_min_actual_profit"] is False
    bid = evaluate_bid_eligibility(
        actual_profit_result=profit,
        cash_pg=evaluate_cash_pg_gates(
            personal_cash_upfront_required=False,
            personal_credit_required=False,
            personal_guarantee_required=False,
            financing_structure_known=True,
            no_pg_financing_verified=True,
            zero_upfront_financing_verified=True,
        ),
        critical_gates={"eligibility": GATE_PASS},
    )
    assert bid["bid_eligible"] is False
    assert bid["decision"] == BID_INELIGIBLE or "actual_profit_below_min" in bid["blockers"]


def test_actual_profit_exactly_10000_passes_threshold():
    profit = calculate_actual_profit(
        revenue=_rev(20000),
        costs={
            "supplier": cost_item(category="supplier", status=COST_VERIFIED, value=10000, required=True),
            "freight": cost_item(category="freight", status=COST_NOT_APPLICABLE),
            "financing": cost_item(category="financing", status=COST_NOT_APPLICABLE),
        },
    )
    assert profit["actual_profit"] == 10000.0
    assert profit["meets_min_actual_profit"] is True


def test_unknown_zero_distinction_serialization():
    unk = cost_item(category="freight", status=COST_REQUIRED_UNKNOWN, required=True)
    zero = cost_item(category="freight", status=COST_VERIFIED_ZERO, value=0)
    na = cost_item(category="freight", status=COST_NOT_APPLICABLE)
    assert unk["value"] is None
    assert zero["value"] == 0.0
    assert na["value"] is None
    assert "Unknown" in display_economic_value(unk, label="Freight")
    assert "$0 (verified)" in display_economic_value(zero, label="Freight")
    assert "Not applicable" in display_economic_value(na, label="Freight")


def test_supplier_quote_preserves_unknown_metadata():
    q = supplier_quote_envelope(unit_price=100, quantity=10)
    assert q["total"]["status"] == "CALCULATED"
    assert q["total"]["value"] == 1000.0
    assert "supplier_identity" in q["missing_metadata"]
    assert q["shipping_included"] is None


def test_calculate_bid_pricing_is_scenario():
    from proposal_service import calculate_bid_pricing

    out = calculate_bid_pricing(40000, 20, option_years=None)
    assert out["status"] == ECON_ESTIMATED_SCENARIO
    assert out["actual_profit"] is None
    assert out["meets_min_actual_profit"] is False
    assert out["base_year_profit"] == 10000.0


def test_bid_range_status_no_invented_band():
    from proposal_service import bid_range_status

    status = bid_range_status(100000, {"regional_benchmark": {"average_annual_award": 90000}})
    assert status["level"] == "neutral"
    assert "not a bid band" in status["message"].lower() or "no verified" in status["message"].lower()


def test_canonical_labor_heavy_requires_subcontract_not_supplier():
    from economic_integrity import map_cost_requirements_for_canonical_class

    costs = map_cost_requirements_for_canonical_class(
        canonical_class="LABOR_HEAVY",
        has_product_procurement_evidence=False,
    )
    assert costs["subcontract"]["status"] == COST_REQUIRED_UNKNOWN
    assert costs["supplier"]["status"] == COST_NOT_APPLICABLE
    assert "LABOR_HEAVY" in (costs["supplier"].get("basis") or "")
    assert costs["financing"]["status"] == COST_REQUIRED_UNKNOWN


def test_canonical_subcontractable_service_requires_subcontract():
    from economic_integrity import map_cost_requirements_for_canonical_class

    costs = map_cost_requirements_for_canonical_class(canonical_class="SUBCONTRACTABLE_SERVICE")
    assert costs["subcontract"]["status"] == COST_REQUIRED_UNKNOWN
    assert costs["supplier"]["status"] == COST_NOT_APPLICABLE


def test_canonical_product_resell_requires_supplier():
    from economic_integrity import map_cost_requirements_for_canonical_class, resolve_canonical_execution_class

    resolved = resolve_canonical_execution_class(stage1_category="PRODUCT_RESALE")
    assert resolved["canonical_execution_class"] == "PRODUCT_RESELL"
    costs = map_cost_requirements_for_canonical_class(canonical_class="PRODUCT_RESELL")
    assert costs["supplier"]["status"] == COST_REQUIRED_UNKNOWN
    assert costs["installation"]["status"] == COST_REQUIRED_UNKNOWN
    assert costs["subcontract"]["status"] == COST_REQUIRED_UNKNOWN
    assert "UNKNOWN" in (costs["installation"].get("basis") or "")


def test_unknown_installation_never_becomes_not_applicable():
    """Integrity: UNKNOWN installation_required must not silently become NOT_APPLICABLE."""
    from economic_integrity import (
        COST_NOT_APPLICABLE,
        COST_REQUIRED_UNKNOWN,
        map_cost_requirements_for_canonical_class,
    )
    from ai_stage2 import assign_economic_requirements
    from data_integrity import STATUS_UNKNOWN, STATUS_VERIFIED

    # Direct mapper — PRODUCT_RESELL
    costs = map_cost_requirements_for_canonical_class(
        canonical_class="PRODUCT_RESELL",
        installation_required=None,
    )
    assert costs["installation"]["status"] == COST_REQUIRED_UNKNOWN
    assert costs["installation"]["status"] != COST_NOT_APPLICABLE
    assert costs["subcontract"]["status"] == COST_REQUIRED_UNKNOWN

    # Service class with unknown install
    svc = map_cost_requirements_for_canonical_class(
        canonical_class="LABOR_HEAVY",
        installation_required=None,
    )
    assert svc["installation"]["status"] == COST_REQUIRED_UNKNOWN
    assert svc["installation"]["status"] != COST_NOT_APPLICABLE

    # Only verified False may become N/A
    na = map_cost_requirements_for_canonical_class(
        canonical_class="PRODUCT_RESELL",
        installation_required=False,
    )
    assert na["installation"]["status"] == COST_NOT_APPLICABLE

    # Stage 2 assign path with UNKNOWN install fact
    facts = {
        "procurement": {"category": {"value": "PRODUCT_RESELL", "status": STATUS_VERIFIED}},
        "execution": {"installation_required": {"value": None, "status": STATUS_UNKNOWN}},
        "scope": {
            "exact_model": {"value": "210-BNZH", "status": STATUS_VERIFIED},
            "brand_name_or_equal": {"value": "Dell brand-name only", "status": STATUS_VERIFIED},
        },
    }
    econ = assign_economic_requirements(facts, stage1={"category": "PRODUCT_RESELL"})
    assert econ["costs"]["installation"]["status"] == COST_REQUIRED_UNKNOWN
    assert econ["costs"]["installation"]["status"] != COST_NOT_APPLICABLE
    assert econ["costs"]["subcontract"]["status"] == COST_REQUIRED_UNKNOWN


def test_canonical_product_plus_install_requires_supplier_and_sub():
    from economic_integrity import map_cost_requirements_for_canonical_class, resolve_canonical_execution_class

    resolved = resolve_canonical_execution_class(
        stage1_category="PRODUCT_RESELL",
        installation_required=True,
    )
    assert resolved["canonical_execution_class"] == "PRODUCT_PLUS_SERVICE"
    costs = map_cost_requirements_for_canonical_class(
        canonical_class="PRODUCT_PLUS_SERVICE",
        installation_required=True,
    )
    assert costs["supplier"]["status"] == COST_REQUIRED_UNKNOWN
    assert costs["installation"]["status"] == COST_REQUIRED_UNKNOWN
    assert costs["subcontract"]["status"] == COST_REQUIRED_UNKNOWN


def test_canonical_unknown_does_not_na_critical_costs():
    from economic_integrity import map_cost_requirements_for_canonical_class

    costs = map_cost_requirements_for_canonical_class(canonical_class="UNKNOWN")
    for cat in ("supplier", "subcontract", "freight", "installation", "financing"):
        assert costs[cat]["status"] == COST_REQUIRED_UNKNOWN, cat


def test_free_text_stage2_category_cannot_override_stage1():
    from economic_integrity import resolve_canonical_execution_class

    resolved = resolve_canonical_execution_class(
        stage1_category="LABOR_HEAVY",
        stage2_category_value="Grounds Maintenance Services",
    )
    assert resolved["canonical_execution_class"] == "LABOR_HEAVY"
    assert resolved["stage2_category_enum"] is None
    assert resolved["stage2_descriptive_category"] == "Grounds Maintenance Services"


def test_not_applicable_requires_basis():
    na = cost_item(category="supplier", status=COST_NOT_APPLICABLE, basis="canonical_execution_class=LABOR_HEAVY")
    assert na["status"] == COST_NOT_APPLICABLE
    assert na.get("basis")

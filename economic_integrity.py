"""Economic integrity — actual profit, cost semantics, cash/PG gates, BID safety.

THE SOFTWARE MAY NEVER MAKE A DEAL LOOK PROFITABLE USING MADE-UP NUMBERS.

UNKNOWN is not zero. Missing cost does not mean free.
Actual profit requires usable VERIFIED / valid CALCULATED inputs only.
POLICY margin scenarios are ESTIMATED_SCENARIO — never actual profit.
"""

from __future__ import annotations

import os
from typing import Any

from data_integrity import (
    STATUS_ASSESSMENT,
    STATUS_CALCULATED,
    STATUS_POLICY,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    calculated_fact,
    can_hard_reject_on_fact,
    make_fact,
    parse_money,
    unknown_fact,
    verified_fact,
    verified_numeric_or_none,
)
from legacy_data_integrity import usable_in_actual_profit

# --- Economic result / cost-item statuses ------------------------------------

ECON_CALCULATED = "CALCULATED"
ECON_INCOMPLETE = "INCOMPLETE"
ECON_ESTIMATED_SCENARIO = "ESTIMATED_SCENARIO"
ECON_UNKNOWN = "UNKNOWN"

COST_UNKNOWN = "UNKNOWN"
COST_VERIFIED_ZERO = "VERIFIED_ZERO"
COST_NOT_APPLICABLE = "NOT_APPLICABLE"
COST_VERIFIED = "VERIFIED"
COST_REQUIRED_UNKNOWN = "REQUIRED_UNKNOWN"
COST_CALCULATED = "CALCULATED"

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"
GATE_UNKNOWN = "UNKNOWN"
GATE_NEEDS_VERIFICATION = "NEEDS_VERIFICATION"

BID_ELIGIBLE = "BID_ELIGIBLE"
BID_WATCH = "WATCH"
BID_NEEDS_RESEARCH = "NEEDS_RESEARCH"
BID_INELIGIBLE = "INELIGIBLE"


def min_actual_profit_usd() -> float:
    try:
        return max(0.0, float(os.getenv("AI_MIN_ACTUAL_PROFIT_USD", "10000")))
    except (TypeError, ValueError):
        return 10000.0


def cost_item(
    *,
    category: str,
    status: str,
    value: float | None = None,
    required: bool = False,
    source_type: str | None = None,
    source_field: str | None = None,
    notes: str | None = None,
    provenance: dict[str, Any] | None = None,
    basis: str | None = None,
) -> dict[str, Any]:
    """Canonical cost envelope. VERIFIED_ZERO ≠ UNKNOWN ≠ NOT_APPLICABLE."""
    st = str(status or COST_UNKNOWN).upper()
    item: dict[str, Any] = {
        "category": category,
        "status": st,
        "required": bool(required),
        "value": None,
        "source_type": source_type,
        "source_field": source_field,
        "notes": notes,
        "provenance": provenance,
        "basis": basis,
    }
    if st == COST_NOT_APPLICABLE:
        item["value"] = None
        item["required"] = False
        item["notes"] = notes or "Cost category does not apply"
        if not item.get("basis"):
            item["basis"] = notes or "not_applicable_without_explicit_basis"
        return item
    if st == COST_UNKNOWN or st == COST_REQUIRED_UNKNOWN:
        item["value"] = None
        if st == COST_REQUIRED_UNKNOWN:
            item["required"] = True
        return item
    if st == COST_VERIFIED_ZERO:
        item["value"] = 0.0
        item["notes"] = notes or "Authoritative evidence establishes cost = 0"
        return item
    if st in {COST_VERIFIED, COST_CALCULATED} and value is not None:
        item["value"] = float(value)
        return item
    item["status"] = COST_UNKNOWN
    item["value"] = None
    return item


# Canonical execution classes for economic mapping (reuse Stage 0/1 enums only).
CANONICAL_EXECUTION_CLASSES = frozenset(
    {
        "PRODUCT_RESELL",
        "PRODUCT_PLUS_SERVICE",
        "SUBCONTRACTABLE_SERVICE",
        "LABOR_HEAVY",
        "SPECIALIST_SERVICE",
        "CONSTRUCTION",
        "UNKNOWN",
    }
)

_SERVICE_EXECUTION_CLASSES = frozenset(
    {
        "SUBCONTRACTABLE_SERVICE",
        "LABOR_HEAVY",
        "SPECIALIST_SERVICE",
        "CONSTRUCTION",
    }
)


def _as_known_canonical(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper().replace(" ", "_")
    if text in {"PRODUCT_RESALE", "PRODUCT"}:
        text = "PRODUCT_RESELL"
    if text in {"MIXED_PRODUCT_SERVICE", "PRODUCT_PLUS_INSTALL"}:
        text = "PRODUCT_PLUS_SERVICE"
    if text in CANONICAL_EXECUTION_CLASSES:
        return text
    return None


def resolve_canonical_execution_class(
    *,
    stage1_category: Any = None,
    stage0_classification: Any = None,
    stage2_category_value: Any = None,
    installation_required: bool | None = None,
    has_product_procurement_evidence: bool = False,
) -> dict[str, Any]:
    """
    Deterministic canonical class for economic cost requirements.

    Free-text Stage 2 descriptive categories NEVER become the economic class
    unless they exactly match a known enum.
    """
    s1 = _as_known_canonical(stage1_category)
    s0 = _as_known_canonical(stage0_classification)
    s2 = _as_known_canonical(stage2_category_value)

    chosen = s1 or s0 or s2 or "UNKNOWN"
    precedence: list[str] = []
    if s1:
        precedence.append("stage1_category")
    elif s0:
        precedence.append("stage0_classification")
    elif s2:
        precedence.append("stage2_category_exact_enum")
    else:
        precedence.append("default_unknown")

    if chosen == "PRODUCT_RESELL" and installation_required is True:
        chosen = "PRODUCT_PLUS_SERVICE"
        precedence.append("installation_required_upgrade")

    descriptive = None
    if stage2_category_value not in (None, "") and not s2:
        descriptive = str(stage2_category_value).strip()

    return {
        "canonical_execution_class": chosen,
        "stage1_category": s1,
        "stage0_classification": s0,
        "stage2_category_enum": s2,
        "stage2_descriptive_category": descriptive,
        "has_product_procurement_evidence": bool(has_product_procurement_evidence),
        "precedence": precedence,
    }


def map_cost_requirements_for_canonical_class(
    *,
    canonical_class: str,
    installation_required: bool | None = None,
    has_product_procurement_evidence: bool = False,
    requires_financing: bool = True,
) -> dict[str, dict[str, Any]]:
    """Map canonical class → cost envelopes with explicit NOT_APPLICABLE basis."""
    klass = _as_known_canonical(canonical_class) or "UNKNOWN"
    costs: dict[str, dict[str, Any]] = {}

    def req(cat: str, *, basis: str) -> None:
        costs[cat] = cost_item(
            category=cat,
            status=COST_REQUIRED_UNKNOWN,
            required=True,
            basis=basis,
            notes=f"{cat} required for executability but dollar value not established",
        )

    def na(cat: str, *, basis: str) -> None:
        costs[cat] = cost_item(
            category=cat,
            status=COST_NOT_APPLICABLE,
            required=False,
            basis=basis,
            notes=basis,
        )

    if klass in _SERVICE_EXECUTION_CLASSES:
        req(
            "subcontract",
            basis=f"canonical_execution_class={klass}; outside field execution cannot be self-performed",
        )
        if has_product_procurement_evidence:
            req(
                "supplier",
                basis=f"canonical_execution_class={klass}; product/material procurement evidence present",
            )
        else:
            na(
                "supplier",
                basis=f"canonical_execution_class={klass}; no product procurement evidence",
            )
        na("freight", basis=f"canonical_execution_class={klass}; no product shipment implied")
        if installation_required is True:
            req("installation", basis=f"canonical_execution_class={klass}; installation_required=true")
        elif installation_required is False:
            na("installation", basis=f"canonical_execution_class={klass}; installation_required=false VERIFIED")
        else:
            # UNKNOWN must not silently become N/A
            req(
                "installation",
                basis=(
                    f"canonical_execution_class={klass}; "
                    "installation_required UNKNOWN — not established as N/A"
                ),
            )
    elif klass == "PRODUCT_RESELL":
        req("supplier", basis=f"canonical_execution_class={klass}")
        req(
            "freight",
            basis=f"canonical_execution_class={klass}; shipment typically required unless verified included",
        )
        if installation_required is True:
            req("installation", basis=f"canonical_execution_class={klass}; installation_required=true")
            req(
                "subcontract",
                basis=(
                    f"canonical_execution_class={klass}; "
                    "installation implies outside execution until quote exists"
                ),
            )
        elif installation_required is False:
            na("installation", basis=f"canonical_execution_class={klass}; installation_required=false VERIFIED")
            na("subcontract", basis=f"canonical_execution_class={klass}; no execution/install requirement")
        else:
            # UNKNOWN must not silently become N/A or zero
            req(
                "installation",
                basis=(
                    f"canonical_execution_class={klass}; "
                    "installation_required UNKNOWN — remains REQUIRED_UNKNOWN until verified N/A or cost"
                ),
            )
            req(
                "subcontract",
                basis=(
                    f"canonical_execution_class={klass}; "
                    "subcontract applicability UNKNOWN until installation/execution requirement resolved"
                ),
            )
    elif klass == "PRODUCT_PLUS_SERVICE":
        req("supplier", basis=f"canonical_execution_class={klass}")
        req("freight", basis=f"canonical_execution_class={klass}")
        req(
            "installation",
            basis=f"canonical_execution_class={klass}; product+service implies install/execution cost",
        )
        req(
            "subcontract",
            basis=f"canonical_execution_class={klass}; outside execution/install required until quote exists",
        )
    else:
        # UNKNOWN — never convenience-N/A critical categories
        req("supplier", basis="canonical_execution_class=UNKNOWN; applicability unresolved")
        req("subcontract", basis="canonical_execution_class=UNKNOWN; applicability unresolved")
        req("freight", basis="canonical_execution_class=UNKNOWN; applicability unresolved")
        req("installation", basis="canonical_execution_class=UNKNOWN; applicability unresolved")

    if requires_financing:
        req(
            "financing",
            basis=(
                f"canonical_execution_class={klass}; "
                "financing required under cash/no-PG operating model until verified terms"
            ),
        )
    else:
        na("financing", basis=f"canonical_execution_class={klass}; financing marked not required by caller")

    na("fees", basis=f"canonical_execution_class={klass}; incidental fees not modeled as required cost category")
    return costs


def revenue_item(
    value: float | None,
    *,
    status: str,
    source_type: str | None = None,
    source_field: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    st = str(status or ECON_UNKNOWN).upper()
    if st == STATUS_VERIFIED and value is not None:
        return {
            "value": float(value),
            "status": STATUS_VERIFIED,
            "source_type": source_type,
            "source_field": source_field,
            "notes": notes,
        }
    if st == STATUS_CALCULATED and value is not None:
        return {
            "value": float(value),
            "status": STATUS_CALCULATED,
            "source_type": source_type or "CALCULATION",
            "source_field": source_field,
            "notes": notes,
        }
    return {
        "value": None,
        "status": STATUS_UNKNOWN,
        "source_type": source_type,
        "source_field": source_field,
        "notes": notes or "Revenue not established",
    }


def _cost_blocks_actual(item: dict[str, Any]) -> bool:
    st = str(item.get("status") or "").upper()
    if st == COST_REQUIRED_UNKNOWN:
        return True
    if item.get("required") and st in {COST_UNKNOWN, COST_REQUIRED_UNKNOWN}:
        return True
    return False


def _cost_numeric_contribution(item: dict[str, Any]) -> float | None:
    """Return amount to subtract, or None if this item cannot enter actual profit math."""
    st = str(item.get("status") or "").upper()
    if st == COST_NOT_APPLICABLE:
        return 0.0  # excluded — contribution 0 without claiming verified zero cost
    if st == COST_VERIFIED_ZERO:
        return 0.0
    if st in {COST_VERIFIED, COST_CALCULATED}:
        if item.get("value") is None:
            return None
        return float(item["value"])
    return None


def infer_required_cost_categories(
    *,
    deal_type: str | None = None,
    requires_shipment: bool | None = None,
    requires_installation: bool | None = None,
    requires_subcontract: bool | None = None,
    requires_financing: bool | None = None,
    shipping_included_in_quote: bool | None = None,
) -> dict[str, bool]:
    """
    Which cost categories are required. Unknown requirement → treat as required
    when deal_type implies it; never assume not required without evidence.
    """
    dtype = (deal_type or "").upper()
    productish = dtype in {"PRODUCT_RESELL", "PRODUCT_PLUS_SERVICE", "PRODUCT"}
    serviceish = dtype in {
        "SUBCONTRACTABLE_SERVICE",
        "LABOR_HEAVY",
        "SPECIALIST_SERVICE",
        "SERVICE",
    }

    ship = requires_shipment
    if ship is None:
        ship = True if productish else False

    install = requires_installation
    if install is None:
        install = True if dtype == "PRODUCT_PLUS_SERVICE" else False

    subcontract = requires_subcontract
    if subcontract is None:
        subcontract = True if serviceish else False

    financing = requires_financing
    if financing is None:
        # Default business rule: $0-upfront model often needs financing — unknown until verified
        financing = True

    freight_required = bool(ship) and shipping_included_in_quote is not True
    return {
        "supplier": True,  # always need a cost basis for actual profit
        "freight": freight_required,
        "installation": bool(install),
        "subcontract": bool(subcontract),
        "financing": bool(financing),
        "fees": False,
    }


def calculate_actual_profit(
    *,
    revenue: dict[str, Any],
    costs: dict[str, dict[str, Any]],
    formula: str | None = None,
) -> dict[str, Any]:
    """
    Actual profit only when revenue is VERIFIED/CALCULATED and every required
    cost is VERIFIED, VERIFIED_ZERO, CALCULATED, or NOT_APPLICABLE.
    """
    missing: list[str] = []
    rev_status = str(revenue.get("status") or "").upper()
    rev_val = revenue.get("value")
    if rev_status not in {STATUS_VERIFIED, STATUS_CALCULATED} or rev_val is None:
        missing.append("revenue")

    total_costs = 0.0
    inputs: list[Any] = [revenue]
    for name, item in (costs or {}).items():
        if _cost_blocks_actual(item):
            missing.append(name)
            continue
        contrib = _cost_numeric_contribution(item)
        if contrib is None:
            if item.get("required"):
                missing.append(name)
            continue
        # NOT_APPLICABLE contributes 0 to sum but is not a fabricated cost
        if str(item.get("status")).upper() != COST_NOT_APPLICABLE:
            total_costs += contrib
        inputs.append(item)

    if missing:
        return {
            "actual_profit": None,
            "status": ECON_INCOMPLETE,
            "revenue": revenue,
            "costs": costs,
            "missing_required_inputs": missing,
            "formula": formula or "revenue - sum(required_costs)",
            "inputs": inputs,
            "meets_min_actual_profit": False,
            "display": _display_incomplete(missing),
        }

    profit = float(rev_val) - total_costs
    result = {
        "actual_profit": round(profit, 2),
        "status": ECON_CALCULATED,
        "revenue": revenue,
        "costs": costs,
        "missing_required_inputs": [],
        "formula": formula or "revenue - sum(usable_costs)",
        "inputs": inputs,
        "total_costs": round(total_costs, 2),
        "meets_min_actual_profit": profit >= min_actual_profit_usd(),
        "min_actual_profit_usd": min_actual_profit_usd(),
        "display": f"Actual profit: ${profit:,.0f}",
    }
    return result


def policy_margin_scenario(
    *,
    cost_basis: float,
    margin_pct: float,
    option_years: int | None = None,
    increase_pct: float | None = None,
    cost_basis_label: str = "sub_quote_or_cost",
) -> dict[str, Any]:
    """
    POLICY / ESTIMATED scenario — NEVER actual profit.
    Does not satisfy the $10K actual-profit gate.
    """
    if cost_basis is None or margin_pct is None:
        return {
            "actual_profit": None,
            "scenario_profit": None,
            "status": ECON_INCOMPLETE,
            "missing_required_inputs": ["cost_basis_or_margin"],
            "meets_min_actual_profit": False,
            "display": "Scenario profit: Unknown — cost basis or policy margin missing",
        }
    margin = float(margin_pct)
    if margin <= 0 or margin >= 100:
        return {
            "actual_profit": None,
            "scenario_profit": None,
            "status": ECON_INCOMPLETE,
            "missing_required_inputs": ["invalid_margin"],
            "meets_min_actual_profit": False,
            "display": "Scenario profit: Unknown — invalid policy margin",
        }
    cost = float(cost_basis)
    bid = cost / (1 - margin / 100.0)
    profit = bid - cost
    years: dict[str, float] = {"base_year": round(bid, 2)}
    if option_years is not None and option_years > 0 and increase_pct is not None:
        prev = bid
        mult = 1 + float(increase_pct) / 100.0
        for i in range(1, int(option_years) + 1):
            prev = prev * mult
            years[f"option_year_{i}"] = round(prev, 2)

    return {
        "actual_profit": None,  # NEVER set actual from scenario
        "scenario_profit": round(profit, 2),
        "scenario_bid": round(bid, 2),
        "status": ECON_ESTIMATED_SCENARIO,
        "margin_percentage": margin,
        "margin_status": STATUS_POLICY,
        "cost_basis": round(cost, 2),
        "cost_basis_label": cost_basis_label,
        "option_years": years if option_years else {"base_year": round(bid, 2)},
        "meets_min_actual_profit": False,  # scenarios never pass actual gate
        "display": (
            f"Scenario profit: ${profit:,.0f} "
            f"(POLICY SCENARIO @ {margin:g}% — not verified actual profit)"
        ),
    }


def historical_award_fact(
    amount: Any,
    *,
    source_field: str = "historical_award",
    amount_basis: str | None = None,
) -> dict[str, Any]:
    """Historical award is context — never current deal revenue."""
    parsed = parse_money(amount, allow_loose=False)
    if parsed is None:
        return unknown_fact(source_field=source_field, notes="Historical award unknown")
    return make_fact(
        parsed,
        status=STATUS_VERIFIED if amount_basis else STATUS_ASSESSMENT,
        source_type="HISTORICAL",
        source_field=source_field,
        confidence="HIGH" if amount_basis else "MEDIUM",
        notes=(
            f"Historical {amount_basis or 'amount'} — not current deal revenue or supplier quote"
        ),
    ) | {"amount_basis": amount_basis, "is_current_revenue": False}


def historical_unit_rate(
    *,
    award_amount: Any,
    quantity: Any,
    amount_basis: str | None = None,
    period_years: float | None = None,
) -> dict[str, Any]:
    """Unit rate only when numerator+denominator verified and semantically compatible."""
    amt = parse_money(award_amount, allow_loose=False)
    try:
        qty = float(quantity) if quantity is not None else None
    except (TypeError, ValueError):
        qty = None
    if amt is None or qty is None or qty <= 0:
        return unknown_fact(
            source_field="historical_unit_rate",
            notes="Cannot derive unit rate — amount or quantity unknown",
        )
    if amount_basis == "total" and period_years is None:
        return unknown_fact(
            source_field="historical_unit_rate",
            notes="Cannot annualize/unitize total award without known period",
        )
    numerator = amt
    if amount_basis == "total" and period_years and period_years >= 1:
        numerator = amt / period_years
    rate = numerator / qty
    return calculated_fact(
        round(rate, 6),
        calculation="historical_amount / quantity" + (" / years" if period_years else ""),
        inputs=[
            verified_fact(amt, source_type="HISTORICAL", source_field="award_amount"),
            verified_fact(qty, source_type="HISTORICAL", source_field="quantity"),
        ],
        source_field="historical_unit_rate",
    ) | {"is_current_revenue": False, "is_supplier_quote": False}


def supplier_quote_envelope(
    *,
    unit_price: Any = None,
    quantity: Any = None,
    total: Any = None,
    supplier_identity: str | None = None,
    quote_reference: str | None = None,
    quote_date: str | None = None,
    quantity_basis: str | None = None,
    shipping_included: bool | None = None,
    tax_fees: Any = None,
    expiration: str | None = None,
    source_type: str = "SUPPLIER_QUOTE",
) -> dict[str, Any]:
    """Retain quote provenance; never fabricate missing metadata."""
    missing_meta = []
    if not supplier_identity:
        missing_meta.append("supplier_identity")
    if quote_reference is None:
        missing_meta.append("quote_reference")
    if quote_date is None:
        missing_meta.append("quote_date")
    if quantity_basis is None:
        missing_meta.append("quantity_basis")
    if shipping_included is None:
        missing_meta.append("shipping_included")

    unit = parse_money(unit_price, allow_loose=False)
    tot = parse_money(total, allow_loose=False)
    try:
        qty = float(quantity) if quantity is not None else None
    except (TypeError, ValueError):
        qty = None

    calculated_total = None
    if unit is not None and qty is not None and qty > 0:
        calculated_total = round(unit * qty, 2)

    if tot is None and calculated_total is not None:
        tot_fact = calculated_fact(
            calculated_total,
            calculation="unit_price * quantity",
            inputs=[
                verified_fact(unit, source_type=source_type, source_field="unit_price"),
                verified_fact(qty, source_type=source_type, source_field="quantity"),
            ],
            source_field="quote_total",
        )
        tot = calculated_total
    elif tot is not None:
        tot_fact = verified_fact(tot, source_type=source_type, source_field="quote_total")
    else:
        tot_fact = unknown_fact(source_field="quote_total")

    return {
        "supplier_identity": supplier_identity,
        "quote_reference": quote_reference,
        "quote_date": quote_date,
        "unit_price": unit,
        "quantity": qty,
        "quantity_basis": quantity_basis,
        "shipping_included": shipping_included,
        "tax_fees": tax_fees if tax_fees is not None else None,
        "expiration": expiration,
        "total": tot_fact,
        "missing_metadata": missing_meta,
        "source_type": source_type,
    }


def evaluate_cash_pg_gates(
    *,
    personal_cash_upfront_required: bool | None = None,
    personal_credit_required: bool | None = None,
    personal_guarantee_required: bool | None = None,
    financing_structure_known: bool | None = None,
    no_pg_financing_verified: bool | None = None,
    zero_upfront_financing_verified: bool | None = None,
    personal_credit_materially_disqualifies: bool | None = None,
) -> dict[str, Any]:
    """
    Hard policy: personal cash upfront = $0.
    Personal credit: FAIL only when verified material disqualification.
    Personal guarantee: NOT automatic FAIL — OPERATOR_REVIEW when required.
    UNKNOWN cannot silently become PASS.
    """
    def _gate(required: bool | None, *, fail_when_true: bool = True) -> str:
        if required is True and fail_when_true:
            return GATE_FAIL
        if required is False:
            return GATE_PASS
        return GATE_UNKNOWN

    cash = _gate(personal_cash_upfront_required)
    # Credit: fail only when materially disqualifying; mere "credit checked" ≠ fail
    if personal_credit_materially_disqualifies is True:
        credit = GATE_FAIL
    elif personal_credit_required is False:
        credit = GATE_PASS
    elif personal_credit_required is True:
        credit = GATE_NEEDS_VERIFICATION  # need FICO role / floor before reject
    else:
        credit = GATE_UNKNOWN

    if personal_guarantee_required is True:
        pg = GATE_NEEDS_VERIFICATION  # OPERATOR_PG_REVIEW — not automatic FAIL
    elif personal_guarantee_required is False:
        pg = GATE_PASS
    else:
        pg = GATE_UNKNOWN

    financing_gate = GATE_UNKNOWN
    if financing_structure_known is False or financing_structure_known is None:
        financing_gate = GATE_NEEDS_VERIFICATION
    elif zero_upfront_financing_verified is True and (
        no_pg_financing_verified is True or personal_guarantee_required is True
    ):
        # PG-required path can still be structurally workable with operator approval
        financing_gate = GATE_PASS if no_pg_financing_verified is True else GATE_NEEDS_VERIFICATION
    elif personal_cash_upfront_required is True:
        financing_gate = GATE_FAIL
    elif personal_guarantee_required is True and personal_cash_upfront_required is not True:
        financing_gate = GATE_NEEDS_VERIFICATION
    elif no_pg_financing_verified is False or zero_upfront_financing_verified is False:
        financing_gate = GATE_FAIL if personal_cash_upfront_required is True else GATE_NEEDS_VERIFICATION
    else:
        financing_gate = GATE_NEEDS_VERIFICATION

    gates = {
        "personal_cash_upfront": cash,
        "personal_credit": credit,
        "personal_guarantee": pg,
        "financing_structure": financing_gate,
    }
    # PG alone must not force overall FAIL
    hard_fails = [v for k, v in gates.items() if v == GATE_FAIL and k != "personal_guarantee"]
    if hard_fails:
        overall = GATE_FAIL
    elif any(v in {GATE_UNKNOWN, GATE_NEEDS_VERIFICATION} for v in gates.values()):
        overall = GATE_UNKNOWN
    else:
        overall = GATE_PASS

    return {
        "gates": gates,
        "overall": overall,
        "operator_pg_review_required": personal_guarantee_required is True,
        "autonomous_pg_acceptance": False,
        "policy": {
            "personal_cash_upfront_usd": 0,
            "personal_credit": "material_disqualification_rejects",
            "personal_guarantee": "operator_risk_decision_not_auto_reject",
        },
    }


def evaluate_bid_eligibility(
    *,
    actual_profit_result: dict[str, Any] | None,
    cash_pg: dict[str, Any] | None,
    critical_gates: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    BID only when critical gates PASS and CALCULATED actual profit >= $10K.
    Scenario profit never qualifies. Incomplete economics => NEEDS_RESEARCH.
    """
    blockers: list[str] = []
    profit = actual_profit_result or {}
    profit_status = str(profit.get("status") or ECON_INCOMPLETE)

    if profit_status == ECON_ESTIMATED_SCENARIO:
        blockers.append("scenario_profit_is_not_actual")
    if profit_status != ECON_CALCULATED or profit.get("actual_profit") is None:
        blockers.append("actual_profit_incomplete")
    elif not profit.get("meets_min_actual_profit"):
        blockers.append("actual_profit_below_min")

    cp = cash_pg or {}
    if cp.get("overall") == GATE_FAIL:
        blockers.append("cash_pg_fail")
    elif cp.get("overall") in {GATE_UNKNOWN, GATE_NEEDS_VERIFICATION, None}:
        blockers.append("cash_pg_unknown")

    for name, state in (critical_gates or {}).items():
        st = str(state or GATE_UNKNOWN).upper()
        if st == GATE_FAIL:
            blockers.append(f"gate_fail:{name}")
        elif st != GATE_PASS:
            blockers.append(f"gate_unknown:{name}")

    if blockers:
        if any(
            b.startswith("gate_fail") or b == "cash_pg_fail" or b == "actual_profit_below_min"
            for b in blockers
        ):
            decision = BID_INELIGIBLE
        elif "scenario_profit_is_not_actual" in blockers or "actual_profit_incomplete" in blockers:
            decision = BID_NEEDS_RESEARCH
        else:
            decision = BID_WATCH
        return {
            "decision": decision,
            "bid_eligible": False,
            "blockers": blockers,
            "actual_profit": profit.get("actual_profit"),
            "actual_profit_status": profit_status,
        }

    return {
        "decision": BID_ELIGIBLE,
        "bid_eligible": True,
        "blockers": [],
        "actual_profit": profit.get("actual_profit"),
        "actual_profit_status": profit_status,
    }


def display_economic_value(item: dict[str, Any] | None, *, label: str = "Value") -> str:
    if not item:
        return f"{label}: Unknown"
    st = str(item.get("status") or "").upper()
    if st in {COST_UNKNOWN, COST_REQUIRED_UNKNOWN, STATUS_UNKNOWN, ECON_INCOMPLETE, ECON_UNKNOWN}:
        req = " — required" if item.get("required") or st == COST_REQUIRED_UNKNOWN else ""
        return f"{label}: Unknown{req}"
    if st == COST_NOT_APPLICABLE:
        return f"{label}: Not applicable"
    if st == COST_VERIFIED_ZERO:
        return f"{label}: $0 (verified)"
    if st == ECON_ESTIMATED_SCENARIO:
        sp = item.get("scenario_profit", item.get("value"))
        return f"{label}: ${float(sp):,.0f} (POLICY SCENARIO — not verified actual profit)" if sp is not None else f"{label}: Unknown"
    if st in {GATE_UNKNOWN, GATE_NEEDS_VERIFICATION}:
        return f"{label}: Needs verification"
    if st == GATE_FAIL:
        return f"{label}: Fail"
    if st == GATE_PASS:
        return f"{label}: Pass"
    val = item.get("value", item.get("actual_profit"))
    if val is None:
        return f"{label}: Unknown"
    return f"{label}: ${float(val):,.0f}"


def display_pg_requirement(state: str | None) -> str:
    st = str(state or GATE_UNKNOWN).upper()
    if st == GATE_PASS:
        return "No PG (verified)"
    if st == GATE_FAIL:
        return "PG required — FAIL"
    return "PG requirement: Unknown"


def _display_incomplete(missing: list[str]) -> str:
    parts = ", ".join(missing) if missing else "inputs"
    return f"Actual profit: Unknown — {parts} required"


def monthly_quote_to_annual(monthly: float | None) -> float | None:
    if monthly is None:
        return None
    try:
        m = float(monthly)
    except (TypeError, ValueError):
        return None
    if m <= 0:
        return None
    return round(m * 12.0, 2)


# Re-export helpers used by callers
__all__ = [
    "ECON_CALCULATED",
    "ECON_INCOMPLETE",
    "ECON_ESTIMATED_SCENARIO",
    "COST_UNKNOWN",
    "COST_VERIFIED_ZERO",
    "COST_NOT_APPLICABLE",
    "COST_VERIFIED",
    "COST_REQUIRED_UNKNOWN",
    "GATE_PASS",
    "GATE_FAIL",
    "GATE_UNKNOWN",
    "GATE_NEEDS_VERIFICATION",
    "BID_ELIGIBLE",
    "BID_WATCH",
    "BID_NEEDS_RESEARCH",
    "BID_INELIGIBLE",
    "min_actual_profit_usd",
    "cost_item",
    "revenue_item",
    "infer_required_cost_categories",
    "calculate_actual_profit",
    "policy_margin_scenario",
    "historical_award_fact",
    "historical_unit_rate",
    "supplier_quote_envelope",
    "evaluate_cash_pg_gates",
    "evaluate_bid_eligibility",
    "display_economic_value",
    "display_pg_requirement",
    "monthly_quote_to_annual",
    "usable_in_actual_profit",
    "can_hard_reject_on_fact",
    "verified_numeric_or_none",
]

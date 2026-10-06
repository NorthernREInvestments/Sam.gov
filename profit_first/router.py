"""Orchestrate profit-first evaluation for an opportunity."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from profit_first.config import BUILD, UNPROVEN, load_targets_from_store
from profit_first.economics import compute_expected_profit, from_line_item_rollup
from profit_first.product_filter import classify_tangible_product
from profit_first.ranking import profit_probability_score
from profit_first.research import research_next_priority


def _f(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _extract_single_line_inputs(rec: dict[str, Any]) -> dict[str, Any]:
    """Pull revenue/cost/freight from canonical record without inventing."""
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    econ = rr.get("economics") if isinstance(rr.get("economics"), dict) else {}
    pe = rec.get("phase_l_economics") if isinstance(rec.get("phase_l_economics"), dict) else {}
    de = rec.get("deal_economics") if isinstance(rec.get("deal_economics"), dict) else {}

    revenue = (
        _f(econ.get("expected_revenue"))
        or _f(econ.get("historical_award_total"))
        or _f(pe.get("expected_revenue"))
        or _f(de.get("expected_revenue") or de.get("revenue"))
        or _f(rec.get("estimated_value"))
    )
    # Prefer unit*qty when available
    hist_u = _f(econ.get("historical_unit_price") or rr.get("historical_award_unit_price") or pe.get("historical_unit_price"))
    qty = _f(econ.get("quantity") or rr.get("quantity") or rec.get("quantity"))
    if revenue is None and hist_u is not None and qty is not None:
        revenue = hist_u * qty

    cost = (
        _f(econ.get("acquisition_cost") or econ.get("public_retail_total"))
        or _f(pe.get("public_retail_total") or pe.get("acquisition_total"))
        or _f(de.get("acquisition_cost") or de.get("product_cost"))
    )
    retail_u = _f(econ.get("public_retail_unit_price") or pe.get("public_retail_unit_price"))
    if cost is None and retail_u is not None and qty is not None:
        cost = retail_u * qty

    freight = _f(econ.get("freight") or pe.get("freight") or de.get("freight") or rec.get("freight"))
    financing = _f(econ.get("financing_cost") or pe.get("financing_cost") or de.get("financing"))
    other = _f(econ.get("other_costs") or pe.get("compliance_direct_costs") or 0) or 0.0

    price_basis = (
        econ.get("price_basis")
        or pe.get("price_basis")
        or ("PUBLIC_RETAIL" if retail_u is not None or econ.get("public_retail_total") else None)
    )
    return {
        "expected_revenue": revenue,
        "product_cost": cost,
        "freight": freight,
        "financing": financing,
        "other_costs": other,
        "price_basis": price_basis,
    }


def evaluate_opportunity_profit(
    *,
    opportunity_id: str,
    rec: dict[str, Any] | None = None,
    title: str | None = None,
    buyer: str | None = None,
    line_item_analysis: dict[str, Any] | None = None,
    expected_revenue: float | None = None,
    product_cost: float | None = None,
    freight: float | None = None,
    financing: float | None = None,
    other_costs: float | None = None,
    price_basis: str | None = None,
    completeness_pct: float | None = None,
    evidence_grade: str | None = None,
    execution_pass: bool | None = None,
    execution_blockers: list[str] | None = None,
    ranking_signals: dict[str, Any] | None = None,
    targets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full profit-first evaluation. Reuses line_item_economics when provided."""
    rec = rec or {}
    targets = targets or load_targets_from_store()
    product = classify_tangible_product(rec, title=title or rec.get("title"))

    if product.get("action") == "REJECT_OR_DEPRIORITIZE" and product.get("is_tangible_product") is False:
        econ = {
            "profit_status": UNPROVEN,
            "expected_profit": None,
            "missing_facts": ["NOT_TANGIBLE_PRODUCT"],
            "note": "Pure service / non-product — deprioritized before economics",
        }
        return {
            "kind": "ProfitFirstEvaluation",
            "build": BUILD,
            "opportunity_id": opportunity_id,
            "title": title or rec.get("title"),
            "buyer": buyer or rec.get("buyer"),
            "evaluated_at": now_utc().isoformat(),
            "product": product,
            "economics": econ,
            "research": research_next_priority(econ, identity_known=False),
            "ranking": {"profit_probability_score": 5, "band": "LOW", "factors": ["-not_tangible_product"]},
            "owner_card": owner_profit_card({"economics": econ, "research": {"priority": "NONE"}, "product": product}),
            "route": "DEPRIORITIZE_NON_PRODUCT",
        }

    if line_item_analysis:
        econ = from_line_item_rollup(line_item_analysis, targets=targets)
        if completeness_pct is None:
            completeness_pct = (line_item_analysis.get("rollup") or {}).get("coverage_pct")
        if evidence_grade is None:
            evidence_grade = (line_item_analysis.get("rollup") or {}).get("completeness_grade")
        if not price_basis:
            price_basis = "PUBLIC_RETAIL"
        # Recompute with explicit overrides if caller provided
        if expected_revenue is not None or product_cost is not None:
            econ = compute_expected_profit(
                expected_revenue=expected_revenue if expected_revenue is not None else econ.get("expected_revenue"),
                product_cost=product_cost if product_cost is not None else econ.get("product_cost"),
                freight=freight if freight is not None else econ.get("freight"),
                financing=financing if financing is not None else econ.get("financing"),
                other_costs=other_costs if other_costs is not None else econ.get("other_costs"),
                price_basis=price_basis,
                completeness_pct=completeness_pct,
                evidence_grade=evidence_grade,
                execution_pass=execution_pass if execution_pass is not None else True,
                execution_blockers=execution_blockers,
                targets=targets,
                unresolved_material=float((line_item_analysis.get("rollup") or {}).get("unresolved_value_share") or 0) > 0.15,
            )
    else:
        pulled = _extract_single_line_inputs(rec)
        econ = compute_expected_profit(
            expected_revenue=expected_revenue if expected_revenue is not None else pulled["expected_revenue"],
            product_cost=product_cost if product_cost is not None else pulled["product_cost"],
            freight=freight if freight is not None else pulled["freight"],
            financing=financing if financing is not None else pulled["financing"],
            other_costs=other_costs if other_costs is not None else pulled["other_costs"],
            price_basis=price_basis or pulled["price_basis"],
            completeness_pct=completeness_pct if completeness_pct is not None else (100.0 if expected_revenue and product_cost else None),
            evidence_grade=evidence_grade or ("B" if (expected_revenue and product_cost) else "D"),
            execution_pass=execution_pass,
            execution_blockers=execution_blockers,
            targets=targets,
        )

    identity_known = bool(
        rec.get("solicitation_event_id")
        or (ranking_signals or {}).get("exact_identity")
        or line_item_analysis
    )
    research = research_next_priority(econ, identity_known=identity_known)
    ranking = profit_probability_score(econ=econ, signals=ranking_signals)

    status = econ.get("profit_status")
    if status in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE"} and execution_pass is not False:
        route = "OWNER_CANDIDATE"
    elif status == "EXECUTION_BLOCKED":
        route = "EXECUTION_BLOCKER"
    elif status == "UNPROFITABLE":
        route = "HIDE_OR_REJECT"
    elif status in {"POSSIBLE_PROFIT", "UNPROVEN"}:
        route = "CONTINUE_RESEARCH"
    else:
        route = "CONTINUE_RESEARCH"

    out = {
        "kind": "ProfitFirstEvaluation",
        "build": BUILD,
        "opportunity_id": opportunity_id,
        "title": title or rec.get("title"),
        "buyer": buyer or rec.get("buyer"),
        "evaluated_at": now_utc().isoformat(),
        "product": product,
        "economics": econ,
        "research": research,
        "ranking": ranking,
        "route": route,
        "line_item_ref": bool(line_item_analysis),
    }
    out["owner_card"] = owner_profit_card(out)
    return out


def owner_profit_card(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Owner-facing summary — answers the nine questions without technical noise."""
    econ = evaluation.get("economics") or {}
    research = evaluation.get("research") or {}
    product = evaluation.get("product") or {}
    signals = econ.get("proof_signals") or []
    price_basis = econ.get("price_basis") or ("Public retail" if "PROFITABLE_AT_PUBLIC_RETAIL" in signals else "UNKNOWN")
    if isinstance(price_basis, str) and price_basis.upper() == "PUBLIC_RETAIL":
        price_basis_label = "Public retail"
    else:
        price_basis_label = str(price_basis)

    next_action = research.get("priority")
    reason = research.get("reason")
    # Discount hint when cost known
    cost = econ.get("product_cost")
    discount_note = None
    if cost and cost > 0:
        discount_note = f"every 5% discount adds ~${round(cost * 0.05):,} profit"

    if evaluation.get("route") == "OWNER_CANDIDATE":
        next_label = "Verify execution + request supplier confirmation"
        if discount_note:
            next_label = f"Request supplier quote — {discount_note}"
    elif next_action == "ACQUISITION_COST":
        next_label = "Find current acquisition / public retail price"
    elif next_action == "GOVERNMENT_VALUE":
        next_label = "Find historical award / bid tab / government value"
    elif next_action == "FREIGHT":
        next_label = "Estimate freight — may erase spread"
    elif next_action == "FINANCING":
        next_label = "Confirm financing cost"
    elif evaluation.get("route") == "HIDE_OR_REJECT":
        next_label = "Do not pursue — unprofitable on evidence"
    else:
        next_label = reason or "Continue research"

    unknown = list(econ.get("missing_facts") or [])
    return {
        "kind": "OwnerProfitCard",
        "what_buying": evaluation.get("title") or product.get("class"),
        "expected_revenue": econ.get("expected_revenue"),
        "product_cost": econ.get("product_cost"),
        "freight": econ.get("freight"),
        "financing": econ.get("financing"),
        "other": econ.get("other_costs"),
        "expected_profit": econ.get("post_financing_profit") or econ.get("expected_profit"),
        "price_basis": price_basis_label,
        "proof": econ.get("profit_status"),
        "proof_signals": signals,
        "completeness": econ.get("completeness_pct"),
        "execution": (
            "PASS"
            if econ.get("execution_pass") is True
            else ("FAIL" if econ.get("execution_pass") is False else "UNKNOWN")
        ),
        "still_unknown": unknown,
        "next": next_label,
        "decision_basis": econ.get("decision_basis"),
        "note": econ.get("note"),
    }

"""Autonomous Pursuit Qualification — research before future human outreach."""

from __future__ import annotations

from typing import Any

from cost_intelligence import freight_range_from_cost, research_bom_cost_intelligence
from executable_deal_constants import PROFIT_FLOOR_USD
from operating_mode import future_action_phrasing, is_development_no_outreach, mode_snapshot
from package_readiness_layers import assess_package_readiness_layers
from pursuit_qualification_constants import (
    ACTION_TIMING_FUTURE,
    ACTION_TIMING_NONE,
    ECON_CLEARLY_UNECONOMIC,
    ECON_POSSIBLE_FLOOR,
    ECON_STRONG,
    ECON_UNLIKELY_FLOOR,
    ECON_UNKNOWN,
    DEFAULT_FINANCE_FEE_PCT_BASE,
    DEFAULT_FINANCE_FEE_PCT_HIGH,
    DEFAULT_FINANCE_FEE_PCT_LOW,
    PKG_INCOMPLETE,
    PKG_PRELIMINARY,
    PROFIT_FLOOR,
    PURSUIT_DEFER,
    PURSUIT_INSUFFICIENT,
    PURSUIT_PRELIMINARY,
    PURSUIT_REJECT,
    PURSUIT_RESEARCHING,
    PURSUIT_WORTHY,
    PURSUIT_WORTHY_UNCERTAIN,
    REASON_AUTH,
    REASON_COMPLIANCE,
    REASON_DEADLINE_SHORT,
    REASON_INSUFFICIENT,
    REASON_NO_SUPPLIER,
    REASON_PRODUCT_UNRESOLVED,
    REASON_PROFIT_IMPLAUSIBLE,
    REASON_SERVICE_HEAVY,
    REV_MODERATE,
    REV_STRONG,
    REV_UNKNOWN,
    REV_WEAK,
)
from reusable_knowledge import ReusableKnowledgeStore


def finance_allowance(cost_base: float | None) -> dict[str, Any]:
    if cost_base is None:
        return {
            "low": None,
            "base": None,
            "high": None,
            "status": "UNKNOWN",
            "note": "screening finance fee allowance — not verified funding",
            "verified": False,
        }
    return {
        "low": round(cost_base * DEFAULT_FINANCE_FEE_PCT_LOW, 2),
        "base": round(cost_base * DEFAULT_FINANCE_FEE_PCT_BASE, 2),
        "high": round(cost_base * DEFAULT_FINANCE_FEE_PCT_HIGH, 2),
        "status": "ESTIMATED_RANGE",
        "note": "conservative transaction-finance fee scenario for screening only",
        "verified": False,
    }


def revenue_context_from_history(
    *,
    cost_ranges: dict[str, Any] | None,
    buyer_history: dict[str, Any] | None = None,
    stated_estimate: float | None = None,
) -> dict[str, Any]:
    """Boundaries only — never guaranteed winning/current revenue."""
    hist = buyer_history or {}
    awards = hist.get("comparable_award_amounts") or []
    unit_prices = hist.get("historical_unit_prices") or []
    strength = REV_UNKNOWN
    low = high = base = None
    notes = ["historical_award_not_guaranteed_current_revenue"]

    if stated_estimate is not None:
        base = float(stated_estimate)
        low = base * 0.85
        high = base * 1.15
        strength = REV_MODERATE
        notes.append("public_budget_or_estimate")
    if awards:
        nums = [float(a) for a in awards if a is not None]
        if nums:
            low = min(nums) if low is None else min(low, min(nums))
            high = max(nums) if high is None else max(high, max(nums))
            base = sum(nums) / len(nums) if base is None else base
            strength = REV_STRONG if len(nums) >= 2 else REV_MODERATE
    if unit_prices and cost_ranges:
        # weak: imply revenue must exceed cost+floor
        pass

    # If only cost known, revenue context weak: need bid >= cost + floor
    if base is None and cost_ranges and cost_ranges.get("base_cost_estimate") is not None:
        c = float(cost_ranges["base_cost_estimate"])
        # Market boundary: government may pay near commercial landed + markup; not a prediction
        low = c * 1.05
        base = c * 1.20
        high = c * 1.45
        strength = REV_WEAK
        notes.append("derived_from_commercial_cost_plus_markup_band_only")

    return {
        "strength": strength,
        "low": None if low is None else round(low, 2),
        "base": None if base is None else round(base, 2),
        "high": None if high is None else round(high, 2),
        "notes": notes,
    }


def preliminary_economic_potential(
    *,
    revenue: dict[str, Any],
    cost_ranges: dict[str, Any] | None,
    freight: dict[str, Any],
    finance: dict[str, Any],
) -> dict[str, Any]:
    if not cost_ranges or cost_ranges.get("base_cost_estimate") is None:
        return {
            "status": ECON_UNKNOWN,
            "low_profit": None,
            "base_profit": None,
            "high_profit": None,
            "meets_floor_possible": None,
            "note": "insufficient cost evidence for profit screening",
        }
    c_low = float(cost_ranges["low_cost_estimate"])
    c_base = float(cost_ranges["base_cost_estimate"])
    c_high = float(cost_ranges["high_cost_estimate"])
    f_low = float(freight.get("low") or 0)
    f_base = float(freight.get("base") or 0)
    f_high = float(freight.get("high") or 0)
    fin_low = float(finance.get("low") or 0)
    fin_base = float(finance.get("base") or 0)
    fin_high = float(finance.get("high") or 0)

    # Conservative pairing: high cost + high freight/finance vs low revenue
    rev_low = revenue.get("low")
    rev_base = revenue.get("base")
    rev_high = revenue.get("high")
    if rev_base is None:
        return {
            "status": ECON_UNKNOWN,
            "low_profit": None,
            "base_profit": None,
            "high_profit": None,
            "meets_floor_possible": None,
            "note": "no revenue context boundary",
        }

    # Profit cases
    low_profit = float(rev_low or rev_base) - c_high - f_high - fin_high
    base_profit = float(rev_base) - c_base - f_base - fin_base
    high_profit = float(rev_high or rev_base) - c_low - f_low - fin_low

    floor = float(PROFIT_FLOOR or PROFIT_FLOOR_USD)
    if high_profit < floor * 0.3:
        status = ECON_CLEARLY_UNECONOMIC
    elif high_profit < floor:
        status = ECON_UNLIKELY_FLOOR
    elif base_profit >= floor:
        status = ECON_STRONG
    elif high_profit >= floor:
        status = ECON_POSSIBLE_FLOOR
    else:
        status = ECON_UNLIKELY_FLOOR

    return {
        "status": status,
        "low_profit": round(low_profit, 2),
        "base_profit": round(base_profit, 2),
        "high_profit": round(high_profit, 2),
        "profit_floor": floor,
        "meets_floor_possible": high_profit >= floor,
        "meets_floor_base": base_profit >= floor,
        "note": "screening ranges only — not a winning-price or guaranteed profit",
    }


def build_buyer_history_compact(
    *,
    agency: str | None,
    title: str | None,
    product_category: str | None = None,
    prior_packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compact buyer intelligence — no database mirror. Uses prior packet cues only unless extended."""
    awards = []
    vendors = []
    competition = "UNKNOWN"
    if prior_packet:
        hist = prior_packet.get("buyer_history") or prior_packet.get("historical") or {}
        if isinstance(hist, dict):
            awards = list(hist.get("award_amounts") or [])[:10]
            vendors = list(hist.get("vendors") or [])[:10]
            competition = hist.get("competition") or competition
    # Seed / Iowa DOT: known public pattern from prior M3 research without inventing amounts
    blob = f"{agency or ''} {title or ''} {product_category or ''}".lower()
    notes = []
    if "iowa" in blob and "seed" in blob:
        notes.append("Iowa DOT periodically buys native/wildflower seed via SciQuest RFBs")
        notes.append("prior_similar_solicitations_observed_in_m3_packets")
        competition = competition if competition != "UNKNOWN" else "UNKNOWN"
    return {
        "agency": agency,
        "comparable_award_amounts": awards,
        "historical_unit_prices": [],
        "winning_vendors": vendors,
        "bidders": "UNKNOWN",
        "competition": competition,
        "frequency": "UNKNOWN",
        "notes": notes,
        "strength": REV_WEAK if notes else REV_UNKNOWN,
        "usaspending_pulled": False,
    }


def decide_pursuit(
    *,
    transactional_fit: bool,
    deadline_status: str | None,
    readiness: dict[str, Any],
    suppliers: list[dict[str, Any]],
    economics: dict[str, Any],
    service_heavy: bool = False,
    funding_incompatible: bool = False,
    funding_verification_required: bool = False,
    funding_exhausted: bool = False,
) -> dict[str, Any]:
    reasons: list[str] = []
    if service_heavy:
        return {"state": PURSUIT_REJECT, "reasons": [REASON_SERVICE_HEAVY]}
    if not transactional_fit:
        return {"state": PURSUIT_REJECT, "reasons": [REASON_SERVICE_HEAVY]}
    if deadline_status == "EXPIRED":
        return {"state": PURSUIT_REJECT, "reasons": [REASON_DEADLINE_SHORT]}
    if deadline_status in {"DUE_TODAY", "DUE_WITHIN_24_HOURS"}:
        reasons.append(REASON_DEADLINE_SHORT)

    if readiness.get("layered_status") == PKG_INCOMPLETE and not readiness.get("priceable_requirements"):
        return {"state": PURSUIT_INSUFFICIENT, "reasons": [REASON_PRODUCT_UNRESOLVED, REASON_INSUFFICIENT]}

    if not suppliers:
        # Without any supplier candidate path, defer — do not reject solely for missing formal quote
        if economics.get("status") in {ECON_STRONG, ECON_POSSIBLE_FLOOR}:
            return {
                "state": PURSUIT_PRELIMINARY,
                "reasons": [REASON_NO_SUPPLIER, "economics_promising_but_supplier_path_unconfirmed"],
            }
        return {"state": PURSUIT_DEFER, "reasons": [REASON_NO_SUPPLIER]}

    econ = economics.get("status")
    if econ == ECON_CLEARLY_UNECONOMIC:
        return {"state": PURSUIT_REJECT, "reasons": [REASON_PROFIT_IMPLAUSIBLE]}
    if econ == ECON_UNLIKELY_FLOOR:
        return {"state": PURSUIT_DEFER, "reasons": [REASON_PROFIT_IMPLAUSIBLE] + reasons}

    # UNKNOWN / unverified financing must NOT force REJECT/DEFER alone
    # Only TRANSACTION_FUNDING_EXHAUSTED (affirmative) kills pursuit
    if funding_exhausted or (funding_incompatible and funding_exhausted):
        return {"state": PURSUIT_REJECT, "reasons": [REASON_FINANCING] + reasons}
    if funding_verification_required or (funding_incompatible and not funding_exhausted):
        # Treat legacy funding_incompatible without exhaustion as verification-required, not reject
        reasons.append("FUNDING_VERIFICATION_REQUIRED")
        funding_verification_required = True

    material_uncertainty = (
        bool(readiness.get("compliance_material_unresolved"))
        or econ == ECON_UNKNOWN
        or funding_verification_required
    )
    if readiness.get("compliance_material_unresolved"):
        reasons.append(REASON_COMPLIANCE)
        reasons.append(REASON_AUTH)

    if econ in {ECON_STRONG, ECON_POSSIBLE_FLOOR} and readiness.get("preliminary_analysis_complete"):
        if econ == ECON_POSSIBLE_FLOOR:
            return {
                "state": PURSUIT_WORTHY_UNCERTAIN,
                "reasons": reasons + ["profit_floor_only_in_optimistic_case"],
                "funding_blocks_execution_only": funding_verification_required,
            }
        if material_uncertainty or reasons:
            return {
                "state": PURSUIT_WORTHY_UNCERTAIN,
                "reasons": reasons or ["material_uncertainty_with_economic_potential"],
                "funding_blocks_execution_only": funding_verification_required,
            }
        return {
            "state": PURSUIT_WORTHY,
            "reasons": ["preliminary_profit_floor_plausible"] + reasons,
            "funding_blocks_execution_only": False,
        }

    if econ == ECON_UNKNOWN and readiness.get("priceable_requirements"):
        return {"state": PURSUIT_PRELIMINARY, "reasons": [REASON_INSUFFICIENT] + reasons}

    return {"state": PURSUIT_DEFER, "reasons": reasons or [REASON_INSUFFICIENT]}


def future_actions_for_pursuit(
    *,
    deal_id: str,
    pursuit_state: str,
    economics: dict[str, Any],
    readiness: dict[str, Any],
    suppliers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Always FUTURE in development mode — never ACTION_NOW for outreach."""
    actions: list[dict[str, Any]] = []
    worthy = pursuit_state in {PURSUIT_WORTHY, PURSUIT_WORTHY_UNCERTAIN}
    if not worthy:
        return [
            {
                "action_type": "NONE",
                "timing": ACTION_TIMING_NONE,
                "deal_id": deal_id,
                "why": "Opportunity not pursuit-worthy — future supplier quote not justified",
                "call_value": "Formal quote would waste operator time under current evidence",
            }
        ]

    profit_note = (
        f"Preliminary profit range ${economics.get('low_profit')}–${economics.get('high_profit')} "
        f"(base ${economics.get('base_profit')}); floor ${economics.get('profit_floor')}"
    )
    if economics.get("meets_floor_base"):
        call_value = f"Formal supplier quote justified if pursued: {profit_note}"
    elif economics.get("meets_floor_possible"):
        call_value = (
            f"Formal quote only weakly justified — floor cleared only in optimistic case: {profit_note}"
        )
    else:
        call_value = f"Formal quote not justified under current screening: {profit_note}"
    best = (suppliers[0].get("supplier_name") or suppliers[0].get("name")) if suppliers else "supplier"
    actions.append(
        {
            "action_type": "CALL_SUPPLIER",
            "timing": ACTION_TIMING_FUTURE,
            "deal_id": deal_id,
            "who_where": best,
            "why": future_action_phrasing(f"Call {best} for formal quote"),
            "call_value": call_value,
            "label": "FUTURE_SUPPLIER_QUOTE_REQUIRED",
        }
    )
    if readiness.get("compliance_material_unresolved"):
        actions.append(
            {
                "action_type": "DOWNLOAD_AUTH_DOCUMENT",
                "timing": ACTION_TIMING_FUTURE,
                "deal_id": deal_id,
                "why": future_action_phrasing("Obtain auth-gated specification if pursued"),
                "call_value": "Needed for COMPLIANT_PRODUCT_REQUIREMENTS before bid — not for preliminary screen",
                "label": "FUTURE_AUTH_SPEC_REQUIRED",
            }
        )
    actions.append(
        {
            "action_type": "CALL_FINANCIER",
            "timing": ACTION_TIMING_FUTURE,
            "deal_id": deal_id,
            "why": future_action_phrasing("Obtain pre-bid financing indication if pursued after quote"),
            "call_value": "Only after supplier cost/freight mature — funding not verified from marketing",
            "label": "FUTURE_FINANCING_VERIFICATION",
        }
    )
    assert all(a.get("timing") != "ACTION_NOW" for a in actions) or not is_development_no_outreach()
    if is_development_no_outreach():
        for a in actions:
            assert a["timing"] == ACTION_TIMING_FUTURE
    return actions


def qualify_opportunity_pursuit(
    opportunity: dict[str, Any],
    *,
    reusable: ReusableKnowledgeStore | None = None,
    prior_packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full autonomous pursuit qualification for one candidate."""
    reusable = reusable or ReusableKnowledgeStore()
    deal_id = str(opportunity.get("solicitation_number") or opportunity.get("deal_id") or "UNKNOWN")
    lines = list(opportunity.get("line_items") or opportunity.get("bom") or [])
    terms = opportunity.get("terms") or {}
    docs = opportunity.get("documents") or []
    auth = list(opportunity.get("auth_barriers") or [])

    readiness = assess_package_readiness_layers(
        documents=docs,
        line_items=lines,
        terms=terms,
        deadline=opportunity.get("bid_deadline") or opportunity.get("deadline"),
        deadline_status=(opportunity.get("deadline_evaluation") or {}).get("status"),
        auth_barriers=auth,
        title=opportunity.get("title"),
        has_authoritative_text=bool(opportunity.get("has_authoritative_text") or lines),
    )

    cost = research_bom_cost_intelligence(
        line_items=lines,
        title=opportunity.get("title"),
        reusable=reusable,
        max_sample=min(25, max(5, len(lines))),
    )
    ranges = cost.get("ranges")
    freight = freight_range_from_cost(
        cost_base=(ranges or {}).get("base_cost_estimate"),
        fob_terms=(terms.get("FOB_terms") or {}).get("value")
        if isinstance(terms.get("FOB_terms"), dict)
        else terms.get("FOB_terms"),
        freight_included=opportunity.get("freight_included"),
    )
    finance = finance_allowance((ranges or {}).get("base_cost_estimate"))
    buyer = build_buyer_history_compact(
        agency=opportunity.get("agency"),
        title=opportunity.get("title"),
        product_category=opportunity.get("product_category"),
        prior_packet=prior_packet,
    )
    revenue = revenue_context_from_history(
        cost_ranges=ranges,
        buyer_history=buyer,
        stated_estimate=opportunity.get("stated_budget"),
    )
    economics = preliminary_economic_potential(
        revenue=revenue, cost_ranges=ranges, freight=freight, finance=finance
    )

    suppliers = list(opportunity.get("supplier_candidates") or opportunity.get("suppliers") or [])
    decision = decide_pursuit(
        transactional_fit=opportunity.get("transactional_fit", True),
        deadline_status=(opportunity.get("deadline_evaluation") or {}).get("status")
        or opportunity.get("deadline_status"),
        readiness=readiness,
        suppliers=suppliers,
        economics=economics,
        service_heavy=bool(opportunity.get("service_heavy")),
        funding_incompatible=bool(opportunity.get("funding_incompatible")),
    )
    futures = future_actions_for_pursuit(
        deal_id=deal_id,
        pursuit_state=decision["state"],
        economics=economics,
        readiness=readiness,
        suppliers=suppliers,
    )

    return {
        "kind": "PursuitQualification",
        "deal_id": deal_id,
        "title": opportunity.get("title"),
        "operating_mode": mode_snapshot(),
        "package_readiness": readiness,
        "cost_intelligence": cost,
        "freight": freight,
        "finance_allowance": finance,
        "buyer_history": buyer,
        "revenue_context": revenue,
        "economic_potential": economics,
        "suppliers": suppliers[:5],
        "pursuit_decision": decision,
        "future_human_actions": futures,
        "external_communication": False,
        "bid_submitted": False,
    }


def build_pursuit_qualification_packet(result: dict[str, Any]) -> dict[str, Any]:
    d = result
    econ = d.get("economic_potential") or {}
    ready = d.get("package_readiness") or {}
    cost = d.get("cost_intelligence") or {}
    return {
        "kind": "PursuitQualificationPacket",
        "OPPORTUNITY": {
            "buyer": d.get("buyer_history", {}).get("agency"),
            "solicitation": d.get("deal_id"),
            "title": d.get("title"),
        },
        "PACKAGE": {
            "preliminary": ready.get("preliminary_analysis_complete"),
            "formal_quote": ready.get("formal_quote_complete"),
            "bid": ready.get("bid_complete"),
            "layered_status": ready.get("layered_status"),
            "requirement_layer": ready.get("requirement_layer"),
            "material_docs": ready.get("material_documents"),
        },
        "PRODUCT": {
            "line_count": cost.get("line_count"),
            "coverage_qty_proxy": cost.get("coverage_of_bom_qty_proxy"),
        },
        "SUPPLIERS": d.get("suppliers"),
        "COST_INTELLIGENCE": {
            "status": cost.get("status"),
            "ranges": cost.get("ranges"),
            "coverage": cost.get("coverage_of_bom_qty_proxy"),
            "exact_vs_comparable": cost.get("exact_vs_comparable"),
        },
        "BUYER_HISTORY": d.get("buyer_history"),
        "ECONOMIC_POTENTIAL": econ,
        "FUNDING": {
            "allowance": d.get("finance_allowance"),
            "verified": False,
            "label": "PRELIMINARY FUNDING CANDIDATES / allowance only",
        },
        "RISKS": {
            "compliance_unresolved": ready.get("compliance_material_unresolved"),
            "freight": (d.get("freight") or {}).get("status"),
        },
        "PURSUIT_DECISION": d.get("pursuit_decision"),
        "FUTURE_HUMAN_ACTIONS": d.get("future_human_actions"),
        "DEVELOPMENT_NOTE": "No immediate outreach — FUTURE HUMAN VERIFICATION IF PURSUED only",
    }

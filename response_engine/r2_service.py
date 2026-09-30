"""R2 orchestration — CLIN → product → technical → quotes → economics → readiness.

Canonical response pricing / technical compliance path.
Reuses: µLab Decimal money, L6 max-buy algebra (Decimal), L.22 quotes via supplier_evidence.
Never READY_TO_SUBMIT. Never fabricates supplier prices. 0 SAM API calls.
"""

from __future__ import annotations

from typing import Any

from application_clock import now_utc

from response_engine.acquisition_cost import build_acquisition_cost_breakdown, line_economics
from response_engine.clins import extract_line_items_from_project
from response_engine.financing import evaluate_execution_gates, financeability_summary
from response_engine.firewall import firewall_report
from response_engine.logistics import (
    classify_freight,
    detect_military_packaging,
    evaluate_delivery,
    packaging_cost_state,
    reconcile_fob,
)
from response_engine.pricing import (
    bid_price_for_target_margin,
    bid_price_for_target_profit,
    calculate_max_buy_decimal,
    compute_profit,
    new_bid_price_scenario,
)
from response_engine.product_offer import evaluate_exact_match, new_offered_product
from response_engine.r2_constants import (
    BUILD,
    CLIN_STRUCTURE_INCOMPLETE,
    DO_NOT_BID,
    ECONOMIC_FAIL,
    ECONOMICS_INCOMPLETE,
    EXECUTION_FAIL,
    FINANCING_REVIEW_REQUIRED,
    PRODUCT_SELECTION_REQUIRED,
    READY_FOR_RESPONSE_BUILD,
    SUPPLIER_QUOTE_REQUIRED,
    TECHNICAL_FAIL,
    TECHNICAL_FAIL_STATUS,
    TECHNICAL_REVIEW_REQUIRED,
    UOM_CONVERSION_BLOCKED,
    UNKNOWN,
    VERIFIED,
)
from response_engine.store import load_project, save_project
from response_engine.supplier_evidence import build_supplier_questions_for_gaps, sanitize_supplier_facing_payload
from response_engine.technical_compliance import evaluate_line_technical_compliance, summarize_technical
from response_engine.uom import convert_quantity


def _utc() -> str:
    return now_utc().isoformat()


def run_r2_analysis(project: dict[str, Any], *, persist: bool = True, refresh_lines: bool = True) -> dict[str, Any]:
    """Populate line_items, technical compliance, economics readiness on ResponseProject."""
    project["r2_build"] = BUILD
    project["r2_analyzed_at"] = _utc()

    # CLIN extraction — only when empty or forced; never wipe owner/amendment edits silently
    if refresh_lines and not project.get("line_items_owner_locked"):
        if not project.get("line_items") or project.get("r2_force_line_refresh"):
            project["line_items"] = extract_line_items_from_project(project)
            project.pop("r2_force_line_refresh", None)

    lines = project.get("line_items") or []
    # UOM normalize each line
    for li in lines:
        conv = convert_quantity(buyer_qty=li.get("quantity"), buyer_uom=li.get("buyer_uom"))
        li["uom_conversion"] = conv
        if conv.get("ok"):
            li["normalized_quantity"] = conv.get("normalized_quantity")
            li["normalized_uom"] = conv.get("normalized_uom")
        else:
            li["normalized_quantity"] = None
            li["uom_block"] = conv.get("block")

    # Technical compliance vs offered products
    products = project.get("offered_products") or []
    evidence = project.get("product_evidence") or []
    tech_items: list[dict[str, Any]] = []
    reqs = [r for r in (project.get("requirements") or []) if not r.get("superseded")]
    for li in lines:
        offered = None
        sel = li.get("selected_offered_product_id")
        for p in products:
            if sel and p.get("offered_product_id") == sel:
                offered = p
                break
            if not sel and p.get("line_item_id") == li.get("line_item_id") and p.get("selected"):
                offered = p
                break
            if not offered and p.get("line_item_id") == li.get("line_item_id"):
                offered = p  # first candidate
        # Update exact match status on offered
        if offered:
            offered["exact_match_status"] = evaluate_exact_match(
                required_mpn=li.get("required_mpn"),
                offered_mpn=offered.get("MPN"),
                product_mode=offered.get("product_mode") or li.get("product_mode") or project.get("product_mode"),
            )
        line_reqs = [r for r in reqs if not r.get("applies_to_clin") or r.get("applies_to_clin") in {None, li.get("CLIN"), li.get("line_item_id")}]
        tech_items.extend(
            evaluate_line_technical_compliance(line=li, offered=offered, requirements=line_reqs or reqs, evidence=evidence)
        )

    project["technical_compliance_items"] = tech_items
    tech_summary = summarize_technical(tech_items)

    # Logistics signals from solicitation text
    blob = "\n".join((d.get("text") or "")[:5000] for d in (project.get("documents") or []))
    mil_pack = detect_military_packaging(blob)
    project["logistics_flags"] = {
        "military_packaging_required": mil_pack,
        "packaging": packaging_cost_state(military_packaging_required=mil_pack),
    }

    # Supplier quotes already on project — do not fabricate
    quotes = project.get("supplier_quotes") or []
    quoted_lines = {q.get("line_item_id") for q in quotes if q.get("line_item_id")}
    lines_needing_quote = [li["line_item_id"] for li in lines if li["line_item_id"] not in quoted_lines]

    # Per-line economics only when quote exists
    line_econ_rows = []
    for li in lines:
        q = next((x for x in quotes if x.get("line_item_id") == li.get("line_item_id")), None)
        if not q or not q.get("unit_price"):
            line_econ_rows.append(
                {
                    "line_item_id": li.get("line_item_id"),
                    "status": SUPPLIER_QUOTE_REQUIRED,
                    "profit_status": "UNKNOWN_PROFIT",
                }
            )
            continue
        if li.get("uom_block") == UOM_CONVERSION_BLOCKED:
            line_econ_rows.append(
                {
                    "line_item_id": li.get("line_item_id"),
                    "status": UOM_CONVERSION_BLOCKED,
                    "profit_status": "UNKNOWN_PROFIT",
                }
            )
            continue
        freight = classify_freight(
            amount=q.get("freight"),
            evidence_state=VERIFIED if q.get("freight") not in (None, "") else UNKNOWN,
        )
        pe = VERIFIED if q.get("supports_verified_acquisition") else UNKNOWN
        fe = freight.get("evidence_state") or UNKNOWN
        if freight.get("blocks_verified_profit"):
            fe = UNKNOWN
        pack = project["logistics_flags"]["packaging"]
        econ = line_economics(
            quantity=li.get("normalized_quantity") or li.get("quantity"),
            unit_acquisition=q.get("unit_price"),
            freight_total=freight.get("amount"),
            packaging_total=pack.get("amount"),
            product_evidence=pe,
            freight_evidence=fe if not freight.get("blocks_verified_profit") else UNKNOWN,
            packaging_evidence=pack.get("evidence_state") or ("NOT_APPLICABLE" if not mil_pack else UNKNOWN),
        )
        line_econ_rows.append({"line_item_id": li.get("line_item_id"), **econ})

    project["line_economics"] = line_econ_rows

    # Opportunity-level summary (no fabricated bid revenue)
    readiness, next_action, blockers, recommendation = _compute_readiness(
        project, tech_summary, lines_needing_quote, line_econ_rows
    )
    project["r2_readiness"] = readiness
    project["r2_next_action"] = next_action
    project["r2_blockers"] = blockers
    project["r2_recommendation"] = recommendation
    project["r2_technical_summary"] = tech_summary
    project["r2_quote_gaps"] = lines_needing_quote

    # Evidence gaps → L.22 questions
    gaps = []
    if lines_needing_quote:
        gaps.append("mpn")
    if any((project.get("logistics_flags") or {}).get("packaging", {}).get("blocks_verified_profit") for _ in [0]):
        gaps.append("freight")
    for li in lines:
        if li.get("uom_block"):
            gaps.append("pack_size")
            break
    project["r2_supplier_questions"] = build_supplier_questions_for_gaps(list(dict.fromkeys(gaps)))

    fw = firewall_report(project)
    project["r2_firewall"] = {"clean": fw.get("clean"), "leaks": fw.get("leaks")}

    if persist:
        save_project(project)
    return project


def create_pricing_scenario(
    project: dict[str, Any],
    *,
    total_bid_price: Any = None,
    target_profit: Any = None,
    target_margin: Any = None,
    scenario_type: str = "OWNER_OR_ENGINE",
) -> dict[str, Any]:
    """Scenario only — not final bid. Uses known execution cost if available."""
    # Sum known line execution costs
    total_cost = None
    from decimal import Decimal

    from response_engine.money import D, money

    costs = []
    for row in project.get("line_economics") or []:
        bd = (row.get("breakdown") or {}) if row.get("ok") else {}
        c = D(bd.get("total_execution_cost"))
        if c is not None:
            costs.append(c)
    if costs:
        total_cost = money(sum(costs, Decimal("0")))

    bid = total_bid_price
    if bid is None and target_profit is not None and total_cost is not None:
        bid = bid_price_for_target_profit(total_execution_cost=total_cost, target_profit=target_profit).get("bid_price")
    if bid is None and target_margin is not None and total_cost is not None:
        bid = bid_price_for_target_margin(total_execution_cost=total_cost, target_margin=target_margin).get("bid_price")

    profit = compute_profit(bid_revenue=bid, total_execution_cost=total_cost, evidence_quality="SCENARIO")
    max_buy = calculate_max_buy_decimal(revenue=bid) if bid else {"ok": False}
    scenario = new_bid_price_scenario(
        response_project_id=project["response_project_id"],
        pricing_basis="SCENARIO",
        total_bid_price=bid,
        acquisition_cost=str(total_cost) if total_cost is not None else None,
        expected_profit=profit.get("expected_profit"),
        margin=profit.get("margin_pct"),
        markup=profit.get("markup_pct"),
        internal_max_buy=(max_buy or {}).get("supplier_quote_target"),
        evidence_quality="SCENARIO",
        scenario_type=scenario_type,
        line_item_ids=[li.get("line_item_id") for li in (project.get("line_items") or [])],
    )
    project.setdefault("pricing_scenarios", []).append(scenario)
    # Internal max-buy stored only in scenario under INTERNAL namespace — not submission
    save_project(project)
    return scenario


def apply_quantity_amendment_to_r2(project: dict[str, Any], *, line_item_id: str | None, new_qty: str) -> dict[str, Any]:
    """Amendment qty change → invalidate quotes/economics for affected lines."""
    invalidated = []
    for li in project.get("line_items") or []:
        if line_item_id and li.get("line_item_id") != line_item_id:
            continue
        li["quantity"] = str(new_qty)
        li["quantity_state"] = "EXACT"
        conv = convert_quantity(buyer_qty=new_qty, buyer_uom=li.get("buyer_uom"))
        li["uom_conversion"] = conv
        li["normalized_quantity"] = conv.get("normalized_quantity") if conv.get("ok") else None
        invalidated.append(li.get("line_item_id"))
    for q in project.get("supplier_quotes") or []:
        if q.get("line_item_id") in invalidated:
            q["quantity_mismatch"] = True
            q["stale_reason"] = "AMENDMENT_QUANTITY_CHANGE"
            q["supports_verified_acquisition"] = False
            q["evidence_state"] = UNKNOWN
    # Clear stale economics; keep amended line quantities (do not re-extract over them)
    project["line_economics"] = []
    project["pricing_scenarios"] = [
        {**s, "scenario_status": "SUPERSEDED_BY_AMENDMENT"} for s in (project.get("pricing_scenarios") or [])
    ]
    project["line_items_owner_locked"] = True
    return run_r2_analysis(project, refresh_lines=False)


def r2_operator_card(project: dict[str, Any]) -> dict[str, Any]:
    """Plain-language Bid Prep R2 panel."""
    lines = project.get("line_items") or []
    tech = project.get("r2_technical_summary") or summarize_technical(project.get("technical_compliance_items") or [])
    quotes = project.get("supplier_quotes") or []
    quoted = len({q.get("line_item_id") for q in quotes if q.get("line_item_id")})
    econ_rows = project.get("line_economics") or []
    scenarios = [s for s in (project.get("pricing_scenarios") or []) if s.get("scenario_status") == "ACTIVE"]
    scenario = scenarios[-1] if scenarios else None
    counts = (tech.get("counts") or {}) if isinstance(tech, dict) else {}
    return {
        "build": BUILD,
        "response_project_id": project.get("response_project_id"),
        "lines": {
            "count": len(lines),
            "items": [
                {
                    "line": li.get("buyer_line_number") or li.get("CLIN"),
                    "qty": li.get("quantity"),
                    "uom": li.get("buyer_uom"),
                    "normalized": li.get("normalized_quantity"),
                    "required_mpn": li.get("required_mpn"),
                    "status": li.get("line_status"),
                    "uom_block": li.get("uom_block"),
                }
                for li in lines
            ],
        },
        "technical": {
            "pass": counts.get("PASS_VERIFIED", 0),
            "fail": counts.get("FAIL", 0),
            "needs_evidence": counts.get("REVIEW_REQUIRED", 0) + counts.get("UNKNOWN", 0),
            "hard_fail": tech.get("hard_fail"),
        },
        "supplier_pricing": {
            "quoted_lines": quoted,
            "needs_quote": len(project.get("r2_quote_gaps") or []),
        },
        "economics": {
            "scenario_bid": (scenario or {}).get("total_bid_price"),
            "expected_profit": (scenario or {}).get("expected_profit"),
            "margin": (scenario or {}).get("margin"),
            "evidence": (scenario or {}).get("evidence_quality") or "UNKNOWN",
            "note": "Scenarios are not final bid prices",
            # deliberately omit internal_max_buy from operator default card detail
        },
        "financing": financeability_summary(
            total_execution_cost=None,
            owner_cash_required=0,
            gates=evaluate_execution_gates({}),
        ),
        "blockers": project.get("r2_blockers") or [],
        "next_action": project.get("r2_next_action") or "REVIEW LINES",
        "readiness": project.get("r2_readiness"),
        "recommendation": project.get("r2_recommendation"),
        "supplier_questions": project.get("r2_supplier_questions") or [],
        "never_ready_to_submit": True,
    }


def get_r2_view(response_project_id: str) -> dict[str, Any]:
    project = load_project(response_project_id)
    if not project:
        return {"ok": False, "error": "not_found"}
    if not project.get("r2_analyzed_at"):
        run_r2_analysis(project)
        project = load_project(response_project_id) or project
    return {
        "ok": True,
        "line_items": project.get("line_items") or [],
        "offered_products": project.get("offered_products") or [],
        "technical_compliance": project.get("technical_compliance_items") or [],
        "supplier_quotes": project.get("supplier_quotes") or [],
        "line_economics": project.get("line_economics") or [],
        "pricing_scenarios": project.get("pricing_scenarios") or [],
        "readiness": project.get("r2_readiness"),
        "operator": r2_operator_card(project),
        "firewall": project.get("r2_firewall"),
        "sam_api_calls": 0,
    }


def _compute_readiness(
    project: dict[str, Any],
    tech_summary: dict[str, Any],
    lines_needing_quote: list[str],
    line_econ_rows: list[dict[str, Any]],
) -> tuple[str, str, list[str], str]:
    blockers: list[str] = []
    lines = project.get("line_items") or []
    if not lines or any((li.get("quantity_state") == "UNKNOWN" and not li.get("quantity")) for li in lines):
        if any(not li.get("quantity") for li in lines):
            blockers.append("Line quantity unknown")
            return CLIN_STRUCTURE_INCOMPLETE, "RESOLVE LINE QUANTITIES", blockers, TECHNICAL_REVIEW_REQUIRED

    if any(li.get("uom_block") == UOM_CONVERSION_BLOCKED for li in lines):
        blockers.append("UOM conversion blocked — pack size unknown")
        return UOM_CONVERSION_BLOCKED, "CONFIRM CASE PACK", blockers, SUPPLIER_QUOTE_REQUIRED

    if tech_summary.get("hard_fail"):
        blockers.append("Technical FAIL on one or more characteristics")
        return TECHNICAL_FAIL_STATUS, "FIND COMPLIANT PRODUCT", blockers, DO_NOT_BID

    products = project.get("offered_products") or []
    if not products:
        blockers.append("No offered product selected")
        return PRODUCT_SELECTION_REQUIRED, "IDENTIFY OFFERED PRODUCT", blockers, PRODUCT_SELECTION_REQUIRED

    if tech_summary.get("needs_review"):
        blockers.append("Technical evidence gaps")
        return TECHNICAL_REVIEW_REQUIRED, "GET OEM / TECHNICAL EVIDENCE", blockers, TECHNICAL_REVIEW_REQUIRED

    if lines_needing_quote:
        blockers.append(f"{len(lines_needing_quote)} line(s) need supplier quote")
        return SUPPLIER_QUOTE_REQUIRED, "GET SUPPLIER QUOTE", blockers, SUPPLIER_QUOTE_REQUIRED

    if any(r.get("status") == UOM_CONVERSION_BLOCKED for r in line_econ_rows):
        blockers.append("UOM blocks verified economics")
        return ECONOMICS_INCOMPLETE, "RESOLVE UOM", blockers, ECONOMICS_INCOMPLETE

    # Execution gates from quotes
    for q in project.get("supplier_quotes") or []:
        gates = evaluate_execution_gates(q)
        if gates.get("execution_status") == EXECUTION_FAIL:
            blockers.append("Supplier terms require PG / personal credit / prepay")
            return FINANCING_REVIEW_REQUIRED, "RESOLVE FINANCING PATH", blockers, EXECUTION_FAIL

    pack = (project.get("logistics_flags") or {}).get("packaging") or {}
    if pack.get("blocks_verified_profit"):
        blockers.append("Required packaging cost unknown")
        return ECONOMICS_INCOMPLETE, "GET PACKAGING COST", blockers, ECONOMICS_INCOMPLETE

    if any(r.get("profit", {}).get("economic_fail") for r in line_econ_rows if r.get("ok")):
        blockers.append("Negative profit on one or more lines")
        return ECONOMICS_INCOMPLETE, "REVIEW ECONOMICS", blockers, ECONOMIC_FAIL

    # Ready for response build only when technical pass + quotes present — still not submit-ready
    if not tech_summary.get("hard_fail") and not lines_needing_quote:
        return READY_FOR_RESPONSE_BUILD, "REVIEW ECONOMICS / BUILD RESPONSE", blockers, READY_FOR_RESPONSE_BUILD

    blockers.append("Economics incomplete")
    return ECONOMICS_INCOMPLETE, "COMPLETE SUPPLIER PRICING", blockers, ECONOMICS_INCOMPLETE


# Public helpers used by APIs
__all__ = [
    "run_r2_analysis",
    "create_pricing_scenario",
    "apply_quantity_amendment_to_r2",
    "r2_operator_card",
    "get_r2_view",
    "sanitize_supplier_facing_payload",
    "new_offered_product",
    "BUILD",
]

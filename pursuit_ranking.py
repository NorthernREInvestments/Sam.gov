"""Explainable PURSUIT_PRIORITY ranking — never award/win probability."""

from __future__ import annotations

from typing import Any

from national_discovery_constants import RANK_KIND
from pursuit_qualification_constants import (
    ECON_POSSIBLE_FLOOR,
    ECON_STRONG,
    ECON_UNKNOWN,
    PURSUIT_WORTHY,
    PURSUIT_WORTHY_UNCERTAIN,
)


def rank_components(opportunity: dict[str, Any]) -> dict[str, Any]:
    """
    Component scores with reasons. Higher = higher pursuit priority.
    Does NOT claim chance of winning.
    """
    comps: dict[str, dict[str, Any]] = {}

    fit = 0.0
    if opportunity.get("transactional_fit") or opportunity.get("stage2", {}).get("productish"):
        fit = 20.0
        comps["transactional_product_fit"] = {"points": fit, "reason": "plausible_tangible_product"}
    else:
        comps["transactional_product_fit"] = {"points": 0.0, "reason": "weak_or_unknown_fit"}

    econ = opportunity.get("economic_potential") or {}
    status = econ.get("status")
    profit_pts = 0.0
    if status == ECON_STRONG:
        profit_pts = 30.0
    elif status == ECON_POSSIBLE_FLOOR:
        profit_pts = 18.0
    elif status == ECON_UNKNOWN:
        profit_pts = 5.0
    comps["estimated_profit_potential"] = {
        "points": profit_pts,
        "reason": f"econ_status={status}",
        "base_profit": econ.get("base_profit"),
        "confidence": "HIGH" if status == ECON_STRONG else "LOW" if status == ECON_UNKNOWN else "MEDIUM",
    }

    cost = opportunity.get("cost_intelligence") or {}
    cov = float(cost.get("coverage_of_bom_qty_proxy") or 0)
    cost_pts = min(15.0, cov * 15.0)
    comps["cost_evidence_quality"] = {
        "points": cost_pts,
        "reason": f"coverage={cov}",
        "exact_count": (cost.get("exact_vs_comparable") or {}).get("exact_count"),
    }

    pkg = opportunity.get("package_readiness") or {}
    pkg_pts = 10.0 if pkg.get("preliminary_analysis_complete") else 3.0
    if pkg.get("formal_quote_complete"):
        pkg_pts += 5.0
    comps["package_completeness"] = {"points": pkg_pts, "reason": pkg.get("layered_status")}

    dl = opportunity.get("deadline_viability") or opportunity.get("deadline_status") or "UNKNOWN"
    dl_pts = 10.0 if dl in {"OPEN", "VIABLE", None} else 0.0
    if dl in {"DUE_TODAY", "DUE_WITHIN_24_HOURS"}:
        dl_pts = 15.0  # urgency, not win odds
    comps["deadline_viability"] = {"points": dl_pts, "reason": str(dl)}

    suppliers = opportunity.get("suppliers") or opportunity.get("supplier_candidates") or []
    sup_pts = min(10.0, 3.0 * len(suppliers))
    comps["supplier_availability"] = {"points": sup_pts, "reason": f"candidates={len(suppliers)}"}

    pursuit = (opportunity.get("pursuit_decision") or {}).get("state")
    pursuit_pts = 0.0
    if pursuit == PURSUIT_WORTHY:
        pursuit_pts = 20.0
    elif pursuit == PURSUIT_WORTHY_UNCERTAIN:
        pursuit_pts = 12.0
    comps["pursuit_qualification"] = {"points": pursuit_pts, "reason": pursuit}

    human = opportunity.get("remaining_human_effort") or "MEDIUM"
    human_pts = {"LOW": 8.0, "MEDIUM": 4.0, "HIGH": 0.0}.get(str(human).upper(), 4.0)
    comps["remaining_human_effort"] = {"points": human_pts, "reason": str(human)}

    # Evidence maturity: do not treat speculative profit equal to verified
    maturity = str(opportunity.get("evidence_maturity") or "PRELIMINARY").upper()
    if maturity in {"SPECULATIVE", "UNKNOWN"} and profit_pts >= 18:
        comps["evidence_maturity_adjustment"] = {
            "points": -10.0,
            "reason": "speculative_profit_discounted_vs_verified",
        }
    else:
        comps["evidence_maturity_adjustment"] = {"points": 0.0, "reason": maturity}

    # Funding: UNKNOWN lowers confidence modestly; EXHAUSTED is catastrophic; never equate them
    funding = opportunity.get("funding_gate") or opportunity.get("financing") or {}
    fund_state = str(funding.get("state") or opportunity.get("funding_status") or "UNKNOWN")
    if fund_state in {"TRANSACTION_FUNDING_EXHAUSTED", "FUNDING_INCOMPATIBLE"}:
        comps["financing_compatibility"] = {
            "points": -25.0,
            "reason": "verified_bad_or_exhausted_funding",
            "unknown_vs_verified_bad": "VERIFIED_BAD",
        }
    elif fund_state in {
        "FUNDING_VERIFICATION_REQUIRED",
        "FUNDING_PATH_IDENTIFIED",
        "FUNDING_RESEARCH_ONLY",
        "FUNDING_REQUIREMENT_UNKNOWN",
        "UNKNOWN",
    }:
        comps["financing_compatibility"] = {
            "points": -3.0,
            "reason": "funding_unknown_or_unverified_soft_penalty",
            "unknown_vs_verified_bad": "UNKNOWN",
        }
    elif fund_state in {"FUNDING_VERIFIED_FOR_TRANSACTION", "FUNDING_CONDITIONALLY_FEASIBLE", "FUNDING_NOT_REQUIRED"}:
        comps["financing_compatibility"] = {
            "points": 5.0,
            "reason": f"funding_state={fund_state}",
            "unknown_vs_verified_bad": "OK",
        }
    else:
        comps["financing_compatibility"] = {
            "points": 0.0,
            "reason": f"funding_state={fund_state}",
            "unknown_vs_verified_bad": "UNKNOWN",
        }

    total = sum(float(c["points"]) for c in comps.values())
    return {
        "kind": RANK_KIND,
        "total": round(total, 2),
        "components": comps,
        "not_award_probability": True,
        "disclaimer": "PURSUIT_PRIORITY under M3 criteria — not chance of government award",
    }


def rank_opportunities(opportunities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for opp in opportunities:
        r = rank_components(opp)
        ranked.append({**opp, "pursuit_priority": r})
    ranked.sort(key=lambda o: -float(o["pursuit_priority"]["total"]))
    for i, row in enumerate(ranked, start=1):
        row["rank"] = i
    return ranked


def explain_rank_delta(higher: dict[str, Any], lower: dict[str, Any]) -> dict[str, Any]:
    h = higher.get("pursuit_priority") or rank_components(higher)
    l = lower.get("pursuit_priority") or rank_components(lower)
    deltas = []
    for key in h.get("components") or {}:
        hp = float((h["components"][key] or {}).get("points") or 0)
        lp = float(((l.get("components") or {}).get(key) or {}).get("points") or 0)
        if abs(hp - lp) >= 0.5:
            deltas.append({"component": key, "higher_points": hp, "lower_points": lp, "delta": round(hp - lp, 2)})
    deltas.sort(key=lambda d: -abs(d["delta"]))
    return {
        "kind": "RankExplanation",
        "higher_id": higher.get("deal_id") or higher.get("identity_key"),
        "lower_id": lower.get("deal_id") or lower.get("identity_key"),
        "higher_total": h.get("total"),
        "lower_total": l.get("total"),
        "top_component_deltas": deltas[:8],
        "not_award_probability": True,
    }


SORT_VIEWS = (
    "OVERALL_PURSUIT_PRIORITY",
    "PROFIT_POTENTIAL",
    "PROFIT_CONFIDENCE",
    "MOST_READY",
    "LOWEST_HUMAN_EFFORT",
    "DEADLINE",
    "NEWEST",
    "MOST_RECENTLY_CHANGED",
    "PRODUCT_CATEGORY",
    "BUYER",
    "STATE_JURISDICTION",
    "PACKAGE_COMPLETENESS",
    "ECONOMIC_EVIDENCE_QUALITY",
    "FINANCING_COMPATIBILITY",
    "COMPETITION_EVIDENCE",
)


def sort_view(rows: list[dict[str, Any]], view: str) -> list[dict[str, Any]]:
    view = view.upper()
    out = list(rows)
    if view == "PROFIT_POTENTIAL":
        out.sort(key=lambda r: -float(((r.get("economic_potential") or {}).get("base_profit") or -1e18)))
    elif view == "DEADLINE":
        out.sort(key=lambda r: str(r.get("deadline") or "9999"))
    elif view == "NEWEST":
        out.sort(key=lambda r: str(r.get("first_seen_at") or ""), reverse=True)
    elif view == "MOST_RECENTLY_CHANGED":
        out.sort(key=lambda r: str(r.get("last_seen_at") or ""), reverse=True)
    elif view == "LOWEST_HUMAN_EFFORT":
        order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        out.sort(key=lambda r: order.get(str(r.get("remaining_human_effort") or "MEDIUM").upper(), 1))
    else:
        out = rank_opportunities(out)
    return out

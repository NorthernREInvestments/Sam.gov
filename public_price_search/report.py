"""Completion report for public price search v2."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from public_price_search.models import (
    BUILD,
    NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH,
    PRICE_SEARCH_BUDGET_EXHAUSTED,
    PUBLIC_PRICE_FOUND,
    PUBLIC_PRICE_PARTIAL,
)
from scale_evidence_profit.models import LENDER_READY, NEAR_READY_24H


def _cummins_regression(store: dict[str, Any]) -> dict[str, Any]:
    row = None
    for k, v in (store.get("by_line") or {}).items():
        pn = str((v.get("identity") or {}).get("part_number") or "").upper()
        if "5579409PX" in pn or "5579409PX" in k.upper():
            row = v
            break
    if row is None:
        # try resolve live for report
        try:
            from public_price_search.resolver import resolve_public_price

            out = resolve_public_price(
                {
                    "part_number": "5579409PX",
                    "manufacturer": "Cummins",
                    "raw_description": "Cummins Injector Kit",
                    "confidence_grade": "A",
                },
                opportunity_id="regression:cummins:5579409PX",
                use_budget=False,
            )
            row = {
                "status": out.get("status"),
                "evidence": out.get("evidence"),
                "identity": {"part_number": "5579409PX", "manufacturer": "Cummins"},
            }
        except Exception as exc:
            return {
                "Found": False,
                "Pass/fail": "FAIL",
                "error": type(exc).__name__,
            }
    ev = row.get("evidence") or {}
    found = row.get("status") in {PUBLIC_PRICE_FOUND, PUBLIC_PRICE_PARTIAL} and bool(ev.get("unit_price"))
    # Accept distributor/reseller recovery when manufacturer JS-hides price
    price_ok = False
    try:
        p = float(ev.get("displayed_price") or ev.get("unit_price") or 0)
        price_ok = 1500 <= p <= 3500
    except (TypeError, ValueError):
        price_ok = False
    cond = str(ev.get("condition") or "")
    cond_ok = cond in {"RECONDITIONED", "REMANUFACTURED", "NEW", "UNKNOWN"}
    passed = found and price_ok and cond_ok
    return {
        "Found": found,
        "Seller": ev.get("seller"),
        "Displayed price": ev.get("displayed_price") or ev.get("unit_price"),
        "Condition": ev.get("condition"),
        "Core charge": ev.get("core_charge"),
        "Shipping": (ev.get("shipping") or {}).get("shipping_amount"),
        "Selected cost basis": ev.get("basis"),
        "Pass/fail": "PASS" if passed else "FAIL",
        "source_url": ev.get("source_url"),
        "via": ev.get("retrieved_via"),
    }


def compose_report(
    *,
    store: dict[str, Any],
    handoff: dict[str, Any],
    by_grade_att: dict[str, int],
    by_grade_found: dict[str, int],
    blocked_recovered: int,
    stage_limit: int,
    grades: tuple[str, ...],
    run_id: str,
    started: str,
) -> dict[str, Any]:
    stats = store.get("stats") or {}
    att = int(stats.get("attempted") or 0)
    found = int(stats.get("found") or 0)

    def _yield(g: str) -> str:
        a = by_grade_att.get(g) or 0
        f = by_grade_found.get(g) or 0
        if a <= 0:
            return "n/a"
        return f"{f}/{a} ({100.0 * f / a:.1f}%)"

    gm_b = (handoff or {}).get("go_metro_before") or {}
    gm_a = (handoff or {}).get("go_metro_after") or {}

    # Economics from SEP opportunities after handoff
    ops = (handoff or {}).get("opportunities") or {}
    opp_both = sum(1 for o in ops.values() if int(o.get("both_sides_lines") or 0) > 0)
    cov = {"25": 0, "50": 0, "75": 0, "90": 0}
    basket_ready = 0
    econ_ready = 0
    lender = near = profitable = ge5 = ge10 = 0
    for o in ops.values():
        mat = float(o.get("material_coverage_pct") or 0)
        if mat >= 25:
            cov["25"] += 1
        if mat >= 50:
            cov["50"] += 1
        if mat >= 75:
            cov["75"] += 1
        if mat >= 90:
            cov["90"] += 1
        if o.get("basket_ready"):
            basket_ready += 1
        if int(o.get("both_sides_lines") or 0) > 0:
            econ_ready += 1
        pipe = o.get("pipeline") or {}
        if pipe.get("readiness") == LENDER_READY or pipe.get("lender_ready"):
            lender += 1
        if pipe.get("readiness") == NEAR_READY_24H or pipe.get("near_ready_24h"):
            near += 1
        profit = (o.get("economics") or {}).get("expected_profit")
        if profit is not None and float(profit) > 0:
            profitable += 1
            if float(profit) >= 5000:
                ge5 += 1
            if float(profit) >= 10000:
                ge10 += 1

    reg = _cummins_regression(store)
    budget_n = int(stats.get("budget_exhausted") or 0)
    true_no = int(stats.get("true_no_price") or 0)

    return {
        "kind": "PublicPriceSearchCompletionReport",
        "build": BUILD,
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "stage_limit": stage_limit,
        "grades": list(grades),
        "REGRESSION": {"Cummins 5579409PX": reg},
        "PRICE_SEARCH": {
            "Exact identities attempted": att,
            "Public prices found": found,
            "Manufacturer-direct": int(stats.get("channel_manufacturer") or 0),
            "Distributor": int(stats.get("channel_distributor") or 0),
            "Reseller": int(stats.get("channel_reseller") or 0),
            "Snippet-only": int(stats.get("channel_snippet") or 0),
            "Blocked source recovered elsewhere": blocked_recovered,
            "Budget exhausted": budget_n,
            "True no-price": true_no,
            "Condition mismatch": int(stats.get("condition_mismatch") or 0),
            "UOM mismatch": int(stats.get("uom_mismatch") or 0),
        },
        "YIELD": {
            "A-confidence price yield": _yield("A"),
            "B-confidence price yield": _yield("B"),
            "C-confidence price yield": _yield("C"),
        },
        "GO_METRO": {
            "Coverage before": gm_b.get("material_coverage_pct"),
            "Coverage after": gm_a.get("material_coverage_pct"),
            "New priced lines": (handoff or {}).get("updated_lines"),
            "Both-sides before": gm_b.get("both_sides_lines"),
            "Both-sides after": gm_a.get("both_sides_lines"),
            "Expected cost": gm_a.get("expected_cost"),
            "Expected profit": gm_a.get("expected_profit"),
            "Status": gm_a.get("status") or gm_a.get("readiness"),
            "research_next": gm_a.get("research_next"),
        },
        "ECONOMICS_IMPACT": {
            "Opportunities with both sides": opp_both,
            ">=25% basket coverage": cov["25"],
            ">=50%": cov["50"],
            ">=75%": cov["75"],
            ">=90%": cov["90"],
            "Basket-ready": basket_ready,
            "Economics-ready": econ_ready,
            "LENDER_READY": lender,
            "NEAR_READY_24H": near,
            "Profitable": profitable,
            ">= $5K": ge5,
            ">= $10K": ge10,
        },
        "MOST_IMPORTANT_ANSWERS": {
            "1_why_missed_cummins": (
                "Prior resolver preferred OpenGov vendor-bid prices and skipped/short-circuited "
                "web crawl when any bid-tab hit existed; manufacturer store prices are often "
                "JS-rendered (no $-amount in HTML) and 401 on some CDN paths; without SERP "
                "snippet + distributor JSON (productPrice) fallback, 5579409PX became NO_PUBLIC_PRICE."
            ),
            "2_can_find_human_price": bool(reg.get("Pass/fail") == "PASS"),
            "3_a_confidence_with_public_prices": by_grade_found.get("A", 0),
            "4_blocked_or_budget_not_true_no_price": budget_n
            + int(stats.get("blocked_retryable") or 0)
            + blocked_recovered,
            "5_basket_coverage_delta": {
                "before": gm_b.get("material_coverage_pct"),
                "after": gm_a.get("material_coverage_pct"),
            },
            "6_go_metro_closer_to_lender": bool(
                (float(gm_a.get("material_coverage_pct") or 0) > float(gm_b.get("material_coverage_pct") or 0))
                or (
                    (gm_a.get("expected_profit") or 0) > (gm_b.get("expected_profit") or 0)
                )
            ),
            "7_next_largest_blocker": (
                NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH
                if true_no >= budget_n
                else PRICE_SEARCH_BUDGET_EXHAUSTED
            ),
        },
        "budget": store.get("budget"),
    }

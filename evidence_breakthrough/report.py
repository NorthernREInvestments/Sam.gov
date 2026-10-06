"""Funnel + completion report for evidence breakthrough."""

from __future__ import annotations

from collections import Counter
from typing import Any

from evidence_breakthrough.models import (
    BUILD,
    FOUND,
    GOV_BASKET_HISTORY,
    GOV_CLOSE_PRODUCT_HISTORY,
    GOV_CURRENT_BUDGET,
    GOV_CURRENT_ESTIMATE,
    GOV_EXACT_LINE_HISTORY,
    GOV_EXACT_PRODUCT_HISTORY,
    GOV_EXACT_SOLICITATION_HISTORY,
    GOV_NO_USABLE_HISTORY,
    NO_PUBLIC_PRICE,
    PUBLIC_ALLOWED_EQUAL,
    PUBLIC_DISTRIBUTOR_EXACT,
    PUBLIC_RESELLER_EXACT,
    PUBLIC_RETAIL_EXACT,
    SUPPLIER_QUOTE,
)


def summarize_funnel(store: dict[str, Any], identities: list[dict[str, Any]]) -> dict[str, Any]:
    by_line = store.get("by_line") or {}
    by_opp = store.get("by_opportunity") or {}

    grade_c = Counter(str(i.get("confidence_grade")) for i in identities)
    gov_types = Counter()
    cost_types = Counter()
    failures = Counter()

    gov_found = 0
    cost_found = 0
    both_lines = 0
    retry_gov = 0
    retry_cost = 0
    insuff = 0

    for em in by_line.values():
        gov = em.get("government_value") or {}
        cost = em.get("public_cost") or {}
        mt = gov.get("match_type") or GOV_NO_USABLE_HISTORY
        gov_types[mt] += 1
        if gov.get("status") == FOUND:
            gov_found += 1
        elif gov.get("status") == "RETRYABLE_FAILURE":
            retry_gov += 1
        elif gov.get("status") == "INSUFFICIENT_IDENTITY":
            insuff += 1
        if gov.get("failure_reason"):
            failures[str(gov["failure_reason"])] += 1

        cmt = cost.get("match_type") or NO_PUBLIC_PRICE
        cost_types[cmt] += 1
        if cost.get("status") == FOUND:
            cost_found += 1
        elif cost.get("status") == "RETRYABLE_FAILURE":
            retry_cost += 1
        if cost.get("failure_reason"):
            failures[str(cost["failure_reason"])] += 1

        if (em.get("confidence") or {}).get("line_ready"):
            both_lines += 1

    basket_ready = sum(1 for o in by_opp.values() if o.get("basket_status") in {"BOTH_SIDES_BASKET_READY", "COMPLETE"})
    complete = sum(1 for o in by_opp.values() if o.get("basket_status") == "COMPLETE")
    opp_both = sum(1 for o in by_opp.values() if int(o.get("lines_with_both_sides") or 0) >= 1)
    cov50 = sum(1 for o in by_opp.values() if float(o.get("coverage_pct") or 0) >= 50)
    cov80 = sum(1 for o in by_opp.values() if float(o.get("coverage_pct") or 0) >= 80)

    profit_buckets = Counter()
    economics_ready = 0
    for o in by_opp.values():
        p = o.get("profit") or {}
        if not p.get("ok"):
            continue
        economics_ready += 1
        label = str(p.get("proof_label") or p.get("profit_bucket") or "UNKNOWN")
        profit_buckets[label] += 1
        profit = p.get("post_financing_profit")
        try:
            pf = float(profit) if profit is not None else None
        except (TypeError, ValueError):
            pf = None
        if pf is not None:
            if pf > 0:
                profit_buckets[">$0"] += 1
            for thr, key in [
                (1000, ">=$1K"),
                (2500, ">=$2.5K"),
                (5000, ">=$5K"),
                (10000, ">=$10K"),
                (25000, ">=$25K"),
                (50000, ">=$50K"),
                (75000, ">=$75K"),
            ]:
                if pf >= thr:
                    profit_buckets[key] += 1

    return {
        "usable_identities": {
            "A": grade_c.get("A", 0),
            "B": grade_c.get("B", 0),
            "C": grade_c.get("C", 0),
            "Total": sum(grade_c.get(g, 0) for g in ("A", "B", "C")),
        },
        "gov_value": {
            "Attempted": len(by_line),
            "Exact line history": gov_types.get(GOV_EXACT_LINE_HISTORY, 0),
            "Exact product history": gov_types.get(GOV_EXACT_PRODUCT_HISTORY, 0),
            "Exact solicitation history": gov_types.get(GOV_EXACT_SOLICITATION_HISTORY, 0),
            "Close product history": gov_types.get(GOV_CLOSE_PRODUCT_HISTORY, 0),
            "Basket history": gov_types.get(GOV_BASKET_HISTORY, 0),
            "Current estimate": gov_types.get(GOV_CURRENT_ESTIMATE, 0),
            "Current budget": gov_types.get(GOV_CURRENT_BUDGET, 0),
            "No usable history": gov_types.get(GOV_NO_USABLE_HISTORY, 0),
            "Retryable": retry_gov,
            "Insufficient identity": insuff,
            "Gov-value-known": gov_found,
        },
        "public_cost": {
            "Attempted": len(by_line),
            "Public exact": cost_types.get(PUBLIC_RETAIL_EXACT, 0),
            "Public distributor": cost_types.get(PUBLIC_DISTRIBUTOR_EXACT, 0),
            "Public reseller": cost_types.get(PUBLIC_RESELLER_EXACT, 0),
            "Allowed equal": cost_types.get(PUBLIC_ALLOWED_EQUAL, 0),
            "Supplier quote": cost_types.get(SUPPLIER_QUOTE, 0),
            "No public price": cost_types.get(NO_PUBLIC_PRICE, 0),
            "Retryable": retry_cost,
            "Acquisition-cost-known": cost_found,
        },
        "both_sides": {
            "Line-ready": both_lines,
            "Basket-ready": basket_ready,
            "Opportunities with >=1 both-side line": opp_both,
            "Opportunities with >=50% basket coverage": cov50,
            "Opportunities with >=80% basket coverage": cov80,
            "Complete baskets": complete,
        },
        "profit": {
            "Economics-ready": economics_ready,
            "buckets": dict(profit_buckets),
        },
        "failures": dict(failures.most_common(20)),
    }


def build_completion_report(
    *,
    stats: dict[str, Any],
    funnel: dict[str, Any],
    opp_summaries: list[dict[str, Any]],
    tdpud: dict[str, Any],
    stage_limit: int,
    run_id: str,
    started: str,
) -> dict[str, Any]:
    from application_clock import now_utc

    gov = funnel.get("gov_value") or {}
    cost = funnel.get("public_cost") or {}
    both = funnel.get("both_sides") or {}
    profit = funnel.get("profit") or {}
    buckets = profit.get("buckets") or {}

    owner_candidates = []
    for o in opp_summaries:
        p = o.get("profit") or {}
        if not p.get("ok"):
            continue
        label = str(p.get("proof_label") or p.get("profit_bucket") or "")
        try:
            profit_amt = float(p.get("post_financing_profit")) if p.get("post_financing_profit") is not None else None
        except (TypeError, ValueError):
            profit_amt = None
        strong = any(x in label.upper() for x in ("PROVEN", "LIKELY"))
        # Also surface economics-ready opps with positive public-price spread during engineering validation
        if strong or (profit_amt is not None and profit_amt > 0):
            owner_candidates.append(
                {
                    "opportunity_id": o.get("opportunity_id"),
                    "coverage_pct": o.get("coverage_pct"),
                    "both_sides_lines": o.get("lines_with_both_sides") or o.get("lines_with_both_sides"),
                    "profit": p.get("owner_summary") or p,
                    "proof_label": label,
                    "expected_profit": profit_amt,
                    "gov_value": p.get("TOTAL_KNOWN_HISTORICAL_VALUE"),
                    "acquisition_cost": p.get("TOTAL_KNOWN_RETAIL_COST"),
                    "spread": p.get("TOTAL_KNOWN_RETAIL_SPREAD"),
                }
            )
    owner_candidates.sort(key=lambda x: -(x.get("expected_profit") or 0))

    # Root-cause answer for prior zero yield
    why_zero = (
        "Prior handoff called analyze_line_item_economics without retail_by_line / "
        "historical_by_line maps, and never scanned OpenGov closed-project public bid "
        "tabulations (isPublicBidPricingResult / bidResults.bidTabulations). "
        "USAspending-only / single-portal misses also returned NO_GOV_VALUE before "
        "buyer-specific history exhaustion."
    )

    return {
        "kind": "EvidenceBreakthroughCompletionReport",
        "build": BUILD,
        "run_id": run_id,
        "stage_limit": stage_limit,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "GOVERNMENT_VALUE": {
            "Identities attempted": stats.get("identities_attempted"),
            "Opportunities attempted": len({o.get("opportunity_id") for o in opp_summaries}),
            "Exact line history": gov.get("Exact line history"),
            "Exact product history": gov.get("Exact product history"),
            "Exact solicitation history": gov.get("Exact solicitation history"),
            "Close history": gov.get("Close product history"),
            "Basket history": gov.get("Basket history"),
            "Current estimate/budget": (gov.get("Current estimate") or 0) + (gov.get("Current budget") or 0),
            "Gov-value-known": gov.get("Gov-value-known") or stats.get("gov_found"),
            "No usable history": gov.get("No usable history"),
            "Retryable": gov.get("Retryable") or stats.get("retryable_gov"),
        },
        "PUBLIC_ACQUISITION_COST": {
            "Identities attempted": stats.get("identities_attempted"),
            "Exact public prices": cost.get("Public exact"),
            "Distributor prices": cost.get("Public distributor"),
            "Reseller prices": cost.get("Public reseller"),
            "Allowed equal prices": cost.get("Allowed equal"),
            "Supplier quotes": cost.get("Supplier quote"),
            "Acquisition-cost-known": cost.get("Acquisition-cost-known") or stats.get("cost_found"),
            "No public price": cost.get("No public price"),
            "Retryable": cost.get("Retryable") or stats.get("retryable_cost"),
        },
        "BOTH_SIDES": {
            "Lines both-sides-known": both.get("Line-ready") or stats.get("both_sides_lines"),
            "Opportunities with both sides": both.get("Opportunities with >=1 both-side line"),
            "Basket-ready": both.get("Basket-ready"),
            "Complete baskets": both.get("Complete baskets"),
        },
        "ECONOMICS": {
            "Economics-ready": profit.get("Economics-ready"),
            "PROVEN_PROFITABLE": buckets.get("RETAIL_PROFIT_PROVEN") or buckets.get("PROVEN_PROFITABLE") or 0,
            "LIKELY_PROFITABLE": buckets.get("RETAIL_PROFIT_LIKELY") or buckets.get("LIKELY_PROFITABLE") or 0,
            "POSSIBLE_PROFIT": buckets.get("POSSIBLE_PROFIT") or buckets.get("RETAIL_PROFIT_UNKNOWN") or 0,
            "UNPROFITABLE": buckets.get("RETAIL_NEGATIVE") or buckets.get("UNPROFITABLE_AT_PUBLIC_PRICE") or 0,
            "PROFITABLE_AT_PUBLIC_RETAIL": (
                (buckets.get("RETAIL_PROFIT_PROVEN") or 0)
                + (buckets.get("RETAIL_PROFIT_LIKELY") or 0)
                + (buckets.get("PROFITABLE_AT_PUBLIC_RETAIL") or 0)
            ),
        },
        "PROFIT_BUCKETS": {
            ">$0": buckets.get(">$0", 0),
            ">=$1K": buckets.get(">=$1K", 0),
            ">=$2.5K": buckets.get(">=$2.5K", 0),
            ">=$5K": buckets.get(">=$5K", 0),
            ">=$10K": buckets.get(">=$10K", 0),
            ">=$25K": buckets.get(">=$25K", 0),
            ">=$50K": buckets.get(">=$50K", 0),
            ">=$75K": buckets.get(">=$75K", 0),
        },
        "TDPUD_REGRESSION": {
            "History found": tdpud.get("history_found"),
            "Public prices found": tdpud.get("public_prices_found"),
            "Both sides": tdpud.get("both_sides"),
            "Profit result": tdpud.get("profit_result"),
            "Remaining blocker": tdpud.get("remaining_blocker"),
            "lines": tdpud.get("lines"),
            "site_search": {
                "pages_ok": (tdpud.get("site_search") or {}).get("pages_ok"),
                "award_candidate_links": (tdpud.get("site_search") or {}).get("award_candidate_links"),
            },
        },
        "TOP_FAILURE_REASONS": funnel.get("failures") or {},
        "TOP_OWNER_CANDIDATES": owner_candidates,
        "FUNNEL": funnel,
        "MOST_IMPORTANT_ANSWERS": {
            "1_why_prior_zero_gov_value": why_zero,
            "2_gov_value_known_now": gov.get("Gov-value-known") or stats.get("gov_found"),
            "3_public_cost_known_now": cost.get("Acquisition-cost-known") or stats.get("cost_found"),
            "4_lines_both_sides": both.get("Line-ready") or stats.get("both_sides_lines"),
            "5_opportunities_both_sides": both.get("Opportunities with >=1 both-side line"),
            "6_baskets_economics_ready": both.get("Basket-ready"),
            "7_profitable_at_public_retail": (
                (buckets.get("RETAIL_PROFIT_PROVEN") or 0)
                + (buckets.get("RETAIL_PROFIT_LIKELY") or 0)
            ),
            "8_ge_5k_profit": buckets.get(">=$5K", 0),
            "9_ge_10k_profit": buckets.get(">=$10K", 0),
            "10_buyer_history_improved_yield": bool((gov.get("Gov-value-known") or 0) > 0),
            "11_acquisition_search_improved_yield": bool((cost.get("Acquisition-cost-known") or 0) > 0),
            "12_largest_bottleneck": _largest_bottleneck(stats, funnel),
            "13_repeatable_end_of_funnel_pool": bool(owner_candidates) or bool((both.get("Line-ready") or 0) > 0),
        },
    }


def _largest_bottleneck(stats: dict[str, Any], funnel: dict[str, Any]) -> str:
    failures = funnel.get("failures") or {}
    if failures:
        top = max(failures.items(), key=lambda kv: kv[1])
        return f"{top[0]} ({top[1]})"
    gov = stats.get("gov_found") or 0
    cost = stats.get("cost_found") or 0
    if gov == 0 and cost == 0:
        return "BOTH_GOV_AND_PUBLIC_COST"
    if gov == 0:
        return "NO_GOV_VALUE"
    if cost == 0:
        return "NO_PUBLIC_PRICE"
    if (stats.get("both_sides_lines") or 0) == 0:
        return "UOM_OR_MATCH_JOIN"
    return "BASKET_INCOMPLETE"

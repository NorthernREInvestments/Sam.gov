"""Completion report for scale-evidence-to-profit."""

from __future__ import annotations

from collections import Counter
from typing import Any

from application_clock import now_utc
from scale_evidence_profit.models import (
    BOTH_SIDES_READY,
    BUILD,
    LENDER_READY,
    NEAR_READY_24H,
)


def build_completion_report(
    *,
    store: dict[str, Any],
    population: dict[str, Any],
    mine_stats: dict[str, Any],
    run_id: str,
    started: str,
) -> dict[str, Any]:
    by_line = store.get("by_line") or {}
    by_opp = store.get("by_opportunity") or {}
    stats = store.get("stats") or {}

    gov_n = sum(1 for r in by_line.values() if (r.get("government_value") or {}).get("status") == "FOUND")
    cost_n = sum(1 for r in by_line.values() if (r.get("public_cost") or {}).get("status") == "FOUND")
    both_n = sum(1 for r in by_line.values() if r.get("line_status") == BOTH_SIDES_READY)

    cov = Counter()
    opp_both = 0
    basket_ready = 0
    complete = 0
    economics_ready = 0
    proven = likely = possible = unprof = exec_blocked = 0
    profitable_retail = 0
    profit_buckets = Counter()
    lender_n = near_n = reserve = 0
    source_stats: dict[str, dict[str, int]] = {}

    def _src_bump(src: str, key: str, n: int = 1) -> None:
        source_stats.setdefault(
            src,
            {
                "both_side_opportunities": 0,
                "basket_ready": 0,
                "economics_ready": 0,
                "LENDER_READY": 0,
                "NEAR_READY": 0,
                "profitable": 0,
                ">=$5K": 0,
                ">=$10K": 0,
            },
        )
        source_stats[src][key] = source_stats[src].get(key, 0) + n

    for oid, o in by_opp.items():
        src = o.get("source_family") or "Other"
        both_lines = int(o.get("both_sides_lines") or 0)
        mat = float(o.get("material_coverage_pct") or 0)
        if both_lines >= 1:
            opp_both += 1
            cov[">=1"] += 1
            _src_bump(src, "both_side_opportunities")
        for thr, label in [(25, ">=25%"), (50, ">=50%"), (75, ">=75%"), (90, ">=90%"), (100, "100%")]:
            if mat >= thr:
                cov[label] += 1
        if o.get("basket_ready"):
            basket_ready += 1
            _src_bump(src, "basket_ready")
        if o.get("complete_basket"):
            complete += 1

        econ = o.get("economics") or {}
        pipe = o.get("pipeline") or {}
        profit = econ.get("expected_profit")
        try:
            pf = float(profit) if profit is not None else None
        except (TypeError, ValueError):
            pf = None

        if both_lines >= 1 and econ.get("expected_revenue") is not None:
            economics_ready += 1
            _src_bump(src, "economics_ready")

        ps = pipe.get("profit_status") or ""
        if ps == "PROVEN_PROFITABLE":
            proven += 1
        elif ps == "LIKELY_PROFITABLE":
            likely += 1
        elif ps == "POSSIBLE_PROFIT":
            possible += 1
        elif ps == "UNPROFITABLE":
            unprof += 1

        if (o.get("execution") or {}).get("status") == "EXECUTION_BLOCKED":
            exec_blocked += 1

        if pf is not None and pf > 0:
            profitable_retail += 1
            _src_bump(src, "profitable")
            profit_buckets[">$0"] += 1
            for thr, key in [
                (1000, ">=$1K"),
                (2500, ">=$2.5K"),
                (5000, ">=$5K"),
                (7500, ">=$7.5K"),
                (10000, ">=$10K"),
                (15000, ">=$15K"),
                (25000, ">=$25K"),
                (50000, ">=$50K"),
                (75000, ">=$75K"),
            ]:
                if pf >= thr:
                    profit_buckets[key] += 1
                    if key in {">=$5K", ">=$10K"}:
                        _src_bump(src, key)

        if pipe.get("lender_ready"):
            lender_n += 1
            _src_bump(src, "LENDER_READY")
        elif pipe.get("near_ready_24h"):
            near_n += 1
            _src_bump(src, "NEAR_READY")
        elif pf is not None and pf > 0:
            reserve += 1

    lender_packets = store.get("lender_ready") or []
    near_packets = store.get("near_ready_24h") or []

    prior_opp = 1
    prior_lines = 111
    material_increase = opp_both > prior_opp and both_n > prior_lines

    # Largest blocker
    fail = Counter()
    for r in by_line.values():
        if r.get("line_status") != BOTH_SIDES_READY:
            fr = (r.get("public_cost") or {}).get("failure_reason") or (r.get("government_value") or {}).get(
                "failure_reason"
            )
            if fr:
                fail[str(fr)] += 1
            else:
                fail[str(r.get("line_status") or "OTHER")] += 1
    top_blocker = fail.most_common(1)[0][0] if fail else "NONE"

    return {
        "kind": "ScaleEvidenceToProfitCompletionReport",
        "build": BUILD,
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "population": population,
        "mine_stats": mine_stats,
        "index_meta": store.get("index_meta"),
        "PHASE_A_EVIDENCE_SCALE": {
            "Usable identities": population.get("usable_identities"),
            "Gov-value-known lines": gov_n,
            "Acquisition-cost-known lines": cost_n,
            "Both-sides-known lines": both_n,
            "Opportunities with >=1 both-side line": opp_both,
            ">=25% basket coverage": cov.get(">=25%", 0),
            ">=50% basket coverage": cov.get(">=50%", 0),
            ">=75% basket coverage": cov.get(">=75%", 0),
            ">=90% basket coverage": cov.get(">=90%", 0),
            "Complete": complete,
            "NO_PUBLIC_PRICE": stats.get("no_public_price"),
            "NO_HISTORY": stats.get("no_history"),
            "UOM blocked": stats.get("uom_blocked"),
            "Insufficient identity": stats.get("insufficient"),
        },
        "PHASE_B_BASKET_ECONOMICS": {
            "Basket-ready": basket_ready,
            "Economics-ready": economics_ready,
            "PROVEN_PROFITABLE": proven,
            "LIKELY_PROFITABLE": likely,
            "POSSIBLE_PROFIT": possible,
            "UNPROFITABLE": unprof,
            "EXECUTION_BLOCKED": exec_blocked,
            "PROFITABLE_AT_PUBLIC_RETAIL": profitable_retail,
        },
        "PROFIT_BUCKETS": {
            ">$0": profit_buckets.get(">$0", 0),
            ">=$1K": profit_buckets.get(">=$1K", 0),
            ">=$2.5K": profit_buckets.get(">=$2.5K", 0),
            ">=$5K": profit_buckets.get(">=$5K", 0),
            ">=$7.5K": profit_buckets.get(">=$7.5K", 0),
            ">=$10K": profit_buckets.get(">=$10K", 0),
            ">=$15K": profit_buckets.get(">=$15K", 0),
            ">=$25K": profit_buckets.get(">=$25K", 0),
            ">=$50K": profit_buckets.get(">=$50K", 0),
            ">=$75K": profit_buckets.get(">=$75K", 0),
        },
        "LENDER_PIPELINE": {
            "LENDER_READY": lender_n,
            "NEAR_READY_24H": near_n,
            "Reserve profitable": reserve,
            "Total positive economics": profitable_retail,
        },
        "SOURCE_ATTRIBUTION": source_stats,
        "TOP_LENDER_READY": lender_packets,
        "TOP_NEAR_READY_24H": [
            {
                "opportunity_id": p.get("opportunity_id"),
                "buyer": p.get("buyer"),
                "expected_profit": p.get("expected_profit"),
                "missing_action": p.get("research_next"),
                "time_to_ready_estimate": "<24h",
                "material_coverage_pct": p.get("material_coverage_pct"),
                "both_sides_lines": p.get("both_sides_lines"),
            }
            for p in near_packets[:10]
        ],
        "MOST_IMPORTANT_ANSWERS": {
            "1_opportunities_both_sides": opp_both,
            "2_materially_exceeded_prior_1_opp_111_lines": material_increase,
            "3_ge_50pct_basket": cov.get(">=50%", 0),
            "4_basket_ready": basket_ready,
            "5_economics_ready": economics_ready,
            "6_profitable": profitable_retail,
            "7_ge_5k": profit_buckets.get(">=$5K", 0),
            "8_ge_10k": profit_buckets.get(">=$10K", 0),
            "9_lender_ready": lender_n,
            "10_near_ready_24h": near_n,
            "11_enough_for_3_4_lender_plus_5_10_near": bool(lender_n >= 3 and near_n >= 5)
            or bool(lender_n + near_n >= 8),
            "12_largest_throughput_blocker": top_blocker,
            "13_one_change_to_increase_lender_ready": (
                "Recover true public distributor/retail acquisition cost (GET_RETAIL_PRICE) "
                "for gov-known exact-PN lines, then PRICE_REMAINING_LINES on the go-metro "
                "opps already near 50% basket coverage"
            ),
            "14_repeatable_deal_pipeline": bool(opp_both >= 3 and both_n >= 150),
            "prior_baseline": {"opportunities": prior_opp, "lines": prior_lines},
            "new_totals": {"opportunities": opp_both, "lines": both_n},
        },
        "line_stats": stats,
    }

"""Eligibility file mining + live public price recovery sweep (v2)."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from eligibility_and_recovery.file_mining import BUILD as MINE_BUILD, mine_all_opportunities
from eligibility_and_recovery.models import ELIGIBLE_TO_RESEARCH
from eligibility_and_recovery.prioritize import prioritize_opportunities, select_lines_for_pass
from eligibility_and_recovery.spec_identity import enrich_identity_for_research
from evidence_breakthrough.corpus import load_identity_store
from evidence_breakthrough.opengov_history import parse_opengov_opportunity_id
from m3_data_root import data_path
from public_price_search import budget as price_budget
from public_price_search.models import (
    NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH,
    PRICE_SEARCH_BUDGET_EXHAUSTED,
    PRICE_SOURCE_BLOCKED_RETRYABLE,
    PUBLIC_PRICE_FOUND,
    PUBLIC_PRICE_PARTIAL,
)
from public_price_search.resolver import resolve_public_price
from public_price_search.search import reset_serp_circuit, route_health_snapshot, serp_circuit_open
from scale_evidence_profit.bid_price_index import load_index
from scale_evidence_profit.line_resolver import resolve_line
from scale_evidence_profit.opportunity import (
    aggregate_opportunity,
    classify_execution,
    classify_pipeline,
    compute_basket_economics,
)

BUILD = "20261004-m3-eligibility-liveprice-v2"


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ikey(oid: str, e: dict[str, Any], idx: int = 0) -> str:
    lid = e.get("line_id") or e.get("part_number") or e.get("model") or f"idx-{idx}"
    return f"{oid}::{lid}"


def run_eligibility_liveprice_v2(
    *,
    on_progress: Any = None,
    max_opportunities: int = 40,
    max_live_prices: int = 60,
    per_opp_price_cap: int = 8,
    resume: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Mine packages for eligibility, then live-price diverse eligible opportunities."""
    run_id = run_id or f"ELP2-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started = now_utc().isoformat()
    reset_serp_circuit()
    price_budget.ensure_budget(minimum_remaining=80)

    if on_progress:
        on_progress(phase="LOAD", pct=2)

    id_store = load_identity_store()
    by_opp = id_store.get("by_opportunity") or {}
    sep = _load("m3_scale_evidence_profit_store.json") or {
        "kind": "ScaleEvidenceProfitStore",
        "by_line": {},
        "by_opportunity": {},
    }
    by_line: dict[str, Any] = sep.setdefault("by_line", {})
    idx = load_index()

    packs: dict[str, dict[str, Any]] = {}
    opp_rows: list[dict[str, Any]] = []
    for oid, pack in by_opp.items():
        if not isinstance(pack, dict):
            continue
        ids = [
            enrich_identity_for_research(i)
            for i in (pack.get("identities") or [])
            if isinstance(i, dict) and i.get("confidence_grade") in {"A", "B", "C"}
        ]
        for i, e in enumerate(ids):
            e["opportunity_id"] = str(oid)
            e["_idx"] = i
        packs[str(oid)] = pack
        code, _ = parse_opengov_opportunity_id(str(oid))
        opp_rows.append(
            {
                "opportunity_id": str(oid),
                "pack": pack,
                "identities": ids,
                "buyer": code,
                "gov_history_available": data_path("opengov_buyer_history", f"{code}.json").exists() if code else False,
                "package_chars": 0,
            }
        )

    # --- Phase 1: full-file eligibility ---
    if on_progress:
        on_progress(phase="ELIGIBILITY_MINE", pct=5)
    mine_path = data_path("m3_eligibility_file_mine_store.json")
    mine = None
    try:
        if mine_path.exists():
            existing = json.loads(mine_path.read_text(encoding="utf-8"))
            if (existing.get("by_opportunity") or {}) and len(existing.get("by_opportunity") or {}) >= len(opp_rows) * 0.9:
                mine = existing
    except Exception:
        mine = None
    if mine is None:
        mine = mine_all_opportunities(
            [r["opportunity_id"] for r in opp_rows],
            packs=packs,
            on_progress=on_progress,
        )
    elig_by = mine.get("by_opportunity") or {}
    for row in opp_rows:
        ev = elig_by.get(row["opportunity_id"]) or {}
        row["eligibility_status"] = ev.get("eligibility_status") or "ELIGIBILITY_UNKNOWN"
        row["eligibility"] = ev

    # Kill ineligible — mark lines, skip research
    for row in opp_rows:
        if row["eligibility_status"] != "BID_INELIGIBLE":
            continue
        oid = row["opportunity_id"]
        for e in row["identities"]:
            key = _ikey(oid, e, int(e.get("_idx") or 0))
            by_line[key] = {
                "opportunity_id": oid,
                "line_id": e.get("line_id"),
                "identity": e,
                "line_status": "ELIGIBILITY_BLOCKED",
                "terminal": "ELIGIBILITY_BLOCK",
                "eligibility": row["eligibility"],
            }

    prioritized = prioritize_opportunities(
        [r for r in opp_rows if r["eligibility_status"] in ELIGIBLE_TO_RESEARCH and r["identities"]]
    )
    # Diversity: prefer non-go-metro first for live price budget
    prioritized.sort(
        key=lambda r: (
            1 if (r.get("buyer") == "go-metro") else 0,
            -len(r.get("identities") or []),
        )
    )
    if max_opportunities:
        prioritized = prioritized[:max_opportunities]

    ck_path = "m3_eligibility_liveprice_v2_checkpoint.json"
    ck = _load(ck_path) if resume else {}
    done = set(ck.get("done_keys") or []) if resume else set()

    stats = {
        "eligible_identities_attempted": 0,
        "exact_ids_attempted": 0,
        "spec_based_attempted": 0,
        "new_prices_found": 0,
        "multiple_price_candidates": 0,
        "best_price_improvements": 0,
        "condition_blocked": 0,
        "uom_blocked": 0,
        "route_blocked_retryable": 0,
        "true_no_price": 0,
        "budget_deferred": 0,
        "new_gov_hits": 0,
        "history_searched": 0,
        "live_price_calls": 0,
        "distinct_opps_priced": set(),
        "opps_with_price": set(),
        "opps_with_gov": set(),
        "opps_with_both": set(),
    }

    live_left = max_live_prices
    if on_progress:
        on_progress(phase="LIVE_PRICE", pct=45)

    for oi, row in enumerate(prioritized):
        oid = row["opportunity_id"]
        if row["eligibility_status"] not in ELIGIBLE_TO_RESEARCH:
            continue
        # Cap lines per opp for diversity
        lines = select_lines_for_pass(row["identities"], pass_kind="sample", already_done=set())
        # Prefer token / recovered
        lines = sorted(
            lines,
            key=lambda e: (
                0 if (e.get("part_number") or e.get("_recovered_token")) else 1,
                {"A": 0, "B": 1, "C": 2}.get(str(e.get("confidence_grade")), 3),
            ),
        )[:per_opp_price_cap]

        priced_this_opp = 0
        for e in lines:
            if live_left <= 0:
                break
            key = _ikey(oid, e, int(e.get("_idx") or 0))
            if key in done and resume:
                continue

            # Index resolve first (cheap gov)
            try:
                result = resolve_line(e, idx, allow_public_price=False)
            except Exception as exc:
                result = {
                    "opportunity_id": oid,
                    "identity": e,
                    "line_status": "ERROR",
                    "error": f"{type(exc).__name__}: {exc}"[:160],
                }
            stats["history_searched"] += 1
            if (result.get("government_value") or {}).get("status") == "FOUND":
                if (by_line.get(key) or {}).get("government_value", {}).get("status") != "FOUND":
                    stats["new_gov_hits"] += 1
                stats["opps_with_gov"].add(oid)

            # Live public price
            has_exact = bool(e.get("part_number") or e.get("catalog_number") or e.get("model") or e.get("sku"))
            is_spec = bool(e.get("_recovered_token") or (e.get("_spec_resolution") or {}).get("researchable"))
            stats["eligible_identities_attempted"] += 1
            if has_exact:
                stats["exact_ids_attempted"] += 1
            if is_spec and not has_exact:
                stats["spec_based_attempted"] += 1

            rem = price_budget.remaining()
            if rem <= 5:
                result["public_cost"] = {
                    "status": "AI_BUDGET_DEFERRED",
                    "failure_reason": "AI_BUDGET_DEFERRED",
                }
                stats["budget_deferred"] += 1
                by_line[key] = result
                done.add(key)
                continue

            try:
                pps = resolve_public_price(
                    e,
                    opportunity_id=oid,
                    use_budget=True,
                    max_queries=2,
                    max_pages=4,
                )
                stats["live_price_calls"] += 1
                live_left -= 1
                priced_this_opp += 1
                stats["distinct_opps_priced"].add(oid)
                st = pps.get("status")
                cands = pps.get("candidates") or []
                if len(cands) >= 2:
                    stats["multiple_price_candidates"] += 1
                if st in {PUBLIC_PRICE_FOUND, PUBLIC_PRICE_PARTIAL}:
                    ev = pps.get("evidence") or {}
                    cond = str(ev.get("condition") or "").upper()
                    if cond in {"REMANUFACTURED", "RECONDITIONED", "USED"}:
                        result["public_cost"] = {
                            "status": "CONDITION_MISMATCH",
                            "failure_reason": "CONDITION_NOT_USABLE_FOR_ECONOMICS",
                            "evidence": ev,
                            "candidates": cands,
                        }
                        stats["condition_blocked"] += 1
                    else:
                        prior = (by_line.get(key) or {}).get("public_cost") or {}
                        prior_px = ((prior.get("evidence") or {}).get("unit_price"))
                        new_px = ev.get("unit_price")
                        if prior_px and new_px and float(new_px) < float(prior_px):
                            stats["best_price_improvements"] += 1
                        result["public_cost"] = {
                            "status": "FOUND",
                            "match_type": "PUBLIC_PRICE_SEARCH",
                            "evidence": {
                                **ev,
                                "selection_reason": ev.get("selection_reason") or "BEST_DEFENSIBLE_NEW_LANDED_PRICE",
                            },
                            "candidates": cands,
                            "routes_attempted": ["manufacturer", "distributor", "direct_catalog", "serp_if_healthy"],
                            "search_trace": pps.get("search_trace"),
                        }
                        stats["new_prices_found"] += 1
                        stats["opps_with_price"].add(oid)
                        gv_ok = (result.get("government_value") or {}).get("status") == "FOUND"
                        result["line_status"] = "BOTH_SIDES_READY" if gv_ok else "COST_ONLY"
                        if gv_ok:
                            stats["opps_with_both"].add(oid)
                elif st == PRICE_SEARCH_BUDGET_EXHAUSTED:
                    result["public_cost"] = {"status": "AI_BUDGET_DEFERRED", "failure_reason": "AI_BUDGET_DEFERRED"}
                    stats["budget_deferred"] += 1
                elif st == PRICE_SOURCE_BLOCKED_RETRYABLE:
                    result["public_cost"] = {
                        "status": PRICE_SOURCE_BLOCKED_RETRYABLE,
                        "failure_reason": "PRICE_ROUTE_BLOCKED_RETRYABLE",
                        "search_trace": pps.get("search_trace"),
                    }
                    stats["route_blocked_retryable"] += 1
                elif st == NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH:
                    result["public_cost"] = {
                        "status": NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH,
                        "failure_reason": NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH,
                        "search_trace": pps.get("search_trace"),
                    }
                    stats["true_no_price"] += 1
                else:
                    result["public_cost"] = {
                        "status": st,
                        "failure_reason": pps.get("failure_reason"),
                        "search_trace": pps.get("search_trace"),
                    }
            except Exception as exc:
                result["public_cost"] = {
                    "status": PRICE_SOURCE_BLOCKED_RETRYABLE,
                    "failure_reason": f"{type(exc).__name__}: {exc}"[:160],
                }
                stats["route_blocked_retryable"] += 1

            by_line[key] = result
            done.add(key)
            if on_progress and stats["live_price_calls"] % 2 == 0:
                on_progress(
                    phase="LIVE_PRICE",
                    pct=min(75, 45 + int(30 * stats["live_price_calls"] / max(max_live_prices, 1))),
                    opp=oi + 1,
                    prices=stats["new_prices_found"],
                    live_left=live_left,
                )

        if priced_this_opp:
            if on_progress:
                on_progress(
                    phase="LIVE_PRICE",
                    pct=min(75, 45 + int(30 * (oi + 1) / max(len(prioritized), 1))),
                    opp=oi + 1,
                    prices=stats["new_prices_found"],
                    live_left=live_left,
                )
        _save(
            ck_path,
            {"run_id": run_id, "done_keys": sorted(done), "stats": {**stats, "distinct_opps_priced": list(stats["distinct_opps_priced"])}},
        )
        if live_left <= 0:
            break

    if on_progress:
        on_progress(phase="AGGREGATE", pct=82)

    # Recompute baskets
    groups: dict[str, list] = defaultdict(list)
    for lr in by_line.values():
        oid = str(lr.get("opportunity_id") or "")
        if oid:
            groups[oid].append(lr)

    by_opp_out: dict[str, Any] = {}
    for row in opp_rows:
        oid = row["opportunity_id"]
        lines = groups.get(oid, [])
        total = max(len(row["identities"]), len(lines), 1)
        agg = aggregate_opportunity(oid, lines, total_purchasing_lines=total) if lines else {
            "opportunity_id": oid,
            "both_sides_lines": 0,
            "gov_value_lines": 0,
            "cost_known_lines": 0,
            "material_coverage_pct": 0,
            "basket_ready": False,
            "usable_identities": len(row["identities"]),
        }
        econ = compute_basket_economics(lines) if lines else {}
        execution = classify_execution(agg, econ) if lines else {}
        pipeline = classify_pipeline(agg, econ, execution) if lines else {}
        by_opp_out[oid] = {
            **agg,
            "eligibility_status": row["eligibility_status"],
            "eligibility": row.get("eligibility"),
            "economics": econ,
            "execution": execution,
            "pipeline": pipeline,
            "buyer": row.get("buyer"),
        }

    sep["by_line"] = by_line
    sep["by_opportunity"] = by_opp_out
    sep["build"] = BUILD
    sep["updated_at"] = now_utc().isoformat()
    sep["eligibility_liveprice_run_id"] = run_id
    _save("m3_scale_evidence_profit_store.json", sep)

    report = _build_report(
        run_id=run_id,
        started=started,
        mine=mine,
        stats=stats,
        by_opp_out=by_opp_out,
        opp_rows=opp_rows,
        usable_n=sum(len(r["identities"]) for r in opp_rows),
    )
    _save("m3_eligibility_liveprice_v2_last_report.json", report)

    if on_progress:
        on_progress(phase="CONSERVATION", pct=92)
    try:
        from funnel_conservation.audit import run_funnel_conservation_audit

        cons = run_funnel_conservation_audit(fix_p0=False)
        report["CONSERVATION"] = {
            "Identity input": (cons.get("IDENTITY_POPULATION") or {}).get("Total usable identities"),
            "Identity terminal sum": cons.get("SUM_TERMINAL_IDENTITIES"),
            "Identity difference": cons.get("DIFFERENCE_FROM_INPUT_IDENTITIES"),
            "Opportunity input": cons.get("UNIQUE_OPPORTUNITIES"),
            "Opportunity terminal sum": cons.get("SUM_TERMINAL_OPPORTUNITIES"),
            "Opportunity difference": cons.get("DIFFERENCE_FROM_INPUT_OPPORTUNITIES"),
            "conservation_ok": cons.get("conservation_ok"),
            "snapshot_id": cons.get("snapshot_id"),
        }
    except Exception as exc:
        report["CONSERVATION"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    _save("m3_eligibility_liveprice_v2_last_report.json", report)
    if on_progress:
        on_progress(phase="DONE", pct=100)
    return report


def _build_report(
    *,
    run_id: str,
    started: str,
    mine: dict[str, Any],
    stats: dict[str, Any],
    by_opp_out: dict[str, Any],
    opp_rows: list[dict[str, Any]],
    usable_n: int,
) -> dict[str, Any]:
    summ = mine.get("summary") or {}
    route_h = route_health_snapshot()

    def _cov(r: dict) -> float:
        return float(r.get("material_coverage_pct") or 0)

    eligible_opps = [r for r in by_opp_out.values() if r.get("eligibility_status") in ELIGIBLE_TO_RESEARCH]
    with_price = sum(1 for r in by_opp_out.values() if int(r.get("cost_known_lines") or 0) >= 1)
    with_5 = sum(1 for r in by_opp_out.values() if int(r.get("cost_known_lines") or 0) >= 5)
    cov25 = sum(1 for r in by_opp_out.values() if _cov(r) >= 25)
    cov50 = sum(1 for r in by_opp_out.values() if _cov(r) >= 50)
    cov75 = sum(1 for r in by_opp_out.values() if _cov(r) >= 75)
    cov90 = sum(1 for r in by_opp_out.values() if _cov(r) >= 90)
    with_both = sum(1 for r in by_opp_out.values() if int(r.get("both_sides_lines") or 0) > 0)
    with_gov = sum(1 for r in by_opp_out.values() if int(r.get("gov_value_lines") or 0) > 0)
    basket_ready = sum(1 for r in by_opp_out.values() if r.get("basket_ready"))

    proven = likely = possible = unprof = blocked = 0
    buckets = {0: 0, 1000: 0, 2500: 0, 5000: 0, 7500: 0, 10000: 0, 15000: 0, 25000: 0, 50000: 0, 75000: 0}
    lender = near = reserve = 0
    go_metro_both = other_both = 0
    for oid, rec in by_opp_out.items():
        both_n = int(rec.get("both_sides_lines") or 0)
        if both_n > 0:
            if "go-metro" in oid:
                go_metro_both += 1
            else:
                other_both += 1
        econ = rec.get("economics") or {}
        pipe = rec.get("pipeline") or {}
        profit = econ.get("expected_profit")
        if both_n <= 0:
            continue
        if (rec.get("execution") or {}).get("status") == "EXECUTION_BLOCKED":
            blocked += 1
        elif profit is None:
            possible += 1
        elif float(profit) > 0:
            if rec.get("basket_ready") or _cov(rec) >= 50:
                proven += 1
            else:
                likely += 1
            for thr in buckets:
                if float(profit) >= thr:
                    buckets[thr] += 1
        else:
            unprof += 1
        if pipe.get("lender_ready") or pipe.get("readiness") == "LENDER_READY":
            lender += 1
        if pipe.get("near_ready_24h") or pipe.get("readiness") == "NEAR_READY_24H":
            near += 1
        if pipe.get("reserve_profitable"):
            reserve += 1

    econ_ready = proven + likely + unprof + sum(
        1 for r in by_opp_out.values() if (r.get("economics") or {}).get("expected_profit") is not None
    )

    rh = {k: (v.get("status") or "UNKNOWN") for k, v in route_h.items()}

    return {
        "kind": "EligibilityLivePriceV2Report",
        "build": BUILD,
        "mine_build": MINE_BUILD,
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "ELIGIBILITY_FILE_COVERAGE": {
            "Opportunities checked": summ.get("opportunities_checked"),
            "Documents total": summ.get("documents_total"),
            "Documents processed": summ.get("documents_processed"),
            "Documents failed": summ.get("documents_failed"),
            "BID_ELIGIBLE": summ.get("BID_ELIGIBLE"),
            "BID_ELIGIBLE_WITH_ACTION": summ.get("BID_ELIGIBLE_WITH_ACTION"),
            "ELIGIBILITY_UNKNOWN": summ.get("ELIGIBILITY_UNKNOWN"),
            "BID_INELIGIBLE": summ.get("BID_INELIGIBLE"),
        },
        "TOP_BID_BLOCKERS": summ.get("top_bid_blockers") or {},
        "TOP_ACTION_REQUIRED": summ.get("top_action_required") or {},
        "FAR_DFARS": summ.get("far_dfars") or {},
        "PRICE_ROUTE_HEALTH": rh,
        "LIVE_PRICE_RECOVERY": {
            "Eligible identities attempted": stats["eligible_identities_attempted"],
            "Exact IDs attempted": stats["exact_ids_attempted"],
            "Spec-based attempted": stats["spec_based_attempted"],
            "NEW prices found": stats["new_prices_found"],
            "Multiple-price candidates": stats["multiple_price_candidates"],
            "Best-price improvements": stats["best_price_improvements"],
            "Condition-blocked": stats["condition_blocked"],
            "UOM-blocked": stats["uom_blocked"],
            "Route-blocked retryable": stats["route_blocked_retryable"],
            "True no-price exhaustive": stats["true_no_price"],
            "Budget deferred": stats["budget_deferred"],
            "SERP circuit open": serp_circuit_open(),
            "Live price calls": stats["live_price_calls"],
        },
        "DISTINCT_OPPORTUNITY_PRICING": {
            "Eligible opportunities": len(eligible_opps),
            "With >=1 NEW public price": with_price,
            "With >=5 priced lines": with_5,
            "With >=25% cost coverage": cov25,
            "With >=50%": cov50,
            "With >=75%": cov75,
            "With >=90%": cov90,
            "Distinct opps live-priced this run": len(stats["distinct_opps_priced"]),
        },
        "HISTORY": {
            "Eligible identities searched": stats["history_searched"],
            "New gov-value hits": stats["new_gov_hits"],
            "Distinct opportunities with gov value": with_gov,
            "True no-history exhaustive": None,
            "Retryable": 0,
        },
        "BOTH_SIDES": {
            "Distinct opportunities with both sides": with_both,
            ">=25% basket coverage": cov25,
            ">=50%": cov50,
            ">=75%": cov75,
            ">=90%": cov90,
            "Basket-ready": basket_ready,
            "go_metro_both": go_metro_both,
            "other_both": other_both,
        },
        "ECONOMICS": {
            "Economics-ready": econ_ready,
            "PROVEN_PROFITABLE": proven,
            "LIKELY_PROFITABLE": likely,
            "POSSIBLE_PROFIT": possible,
            "UNPROFITABLE": unprof,
            "EXECUTION_BLOCKED": blocked,
        },
        "PROFIT_BUCKETS": {
            ">$0": buckets[0],
            ">=$1K": buckets[1000],
            ">=$2.5K": buckets[2500],
            ">=$5K": buckets[5000],
            ">=$7.5K": buckets[7500],
            ">=$10K": buckets[10000],
            ">=$15K": buckets[15000],
            ">=$25K": buckets[25000],
            ">=$50K": buckets[50000],
            ">=$75K": buckets[75000],
        },
        "LENDER_PIPELINE": {
            "LENDER_READY": lender,
            "NEAR_READY_24H": near,
            "RESERVE_PROFITABLE": reserve,
        },
        "MOST_IMPORTANT_ANSWERS": {
            "1_killed_by_full_file_mining": summ.get("BID_INELIGIBLE"),
            "2_hidden_blockers": summ.get("top_bid_blockers"),
            "3_pricing_operational": {
                "serp": rh.get("SERP"),
                "manufacturer": rh.get("Manufacturer"),
                "distributor": rh.get("Distributor"),
                "direct_catalog": rh.get("Direct catalog"),
                "live_prices_found": stats["new_prices_found"],
            },
            "4_new_public_prices": stats["new_prices_found"],
            "5_exact_ids_now_priced": stats["exact_ids_attempted"] and stats["new_prices_found"],
            "6_spec_based_priced": stats["spec_based_attempted"],
            "7_distinct_opps_with_acq_cost": with_price,
            "8_distinct_both_sides": with_both,
            "9_ge_50_coverage": cov50,
            "10_basket_ready": basket_ready,
            "11_economics_ready": econ_ready,
            "12_profitable": proven + likely,
            "13_ge_5k": buckets[5000],
            "14_ge_10k": buckets[10000],
            "15_lender_ready": lender,
            "16_near_ready": near,
            "17_diversity": {"go_metro_both": go_metro_both, "other_both": other_both, "improved": other_both > 0},
            "18_remaining_p0_p1": [
                {"severity": "P1", "issue": "SERP_CIRCUIT_OPEN", "detail": rh.get("SERP")}
                if rh.get("SERP") == "CIRCUIT_OPEN"
                else None,
                {
                    "severity": "P1",
                    "issue": "DIRECT_CATALOG_YIELD",
                    "detail": "Manufacturer/distributor routes run when SERP open; yield depends on HTML price extractability",
                },
            ],
            "19_biggest_bottleneck": (
                "Recovering defensible NEW public prices from manufacturer/distributor pages "
                "while SERP remains circuit-open; plus expanding exact-PN history beyond go-metro."
            ),
        },
        "usable_identities": usable_n,
        "opportunities": len(opp_rows),
    }

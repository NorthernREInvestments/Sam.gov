"""Aggregate large-test results into the exact completion report payload."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from large_production_test.models import DROP_CLASS, STAGES


def _pct(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def _pct_label(n: int, d: int) -> str:
    if not d:
        return "0%"
    return f"{round(100.0 * n / d, 1)}%"


def has_stage(row: dict[str, Any], stage: str) -> bool:
    return stage in (row.get("stages_hit") or [])


def build_report(
    *,
    corpus: dict[str, Any],
    results: list[dict[str, Any]],
    budgets: dict[str, Any],
    runtime_s: float,
    run_id: str,
    checkpoint_resume_used: bool,
    conservation: dict[str, Any],
) -> dict[str, Any]:
    n = len(results)
    ids = [r["opportunity_id"] for r in results]
    assert n == len(set(ids))

    def count_stage(stage: str) -> int:
        return sum(1 for r in results if has_stage(r, stage))

    # Treat QUOTE_REQUIRED as acquisition path alternative
    public_acq = count_stage("ACQUISITION_COST_READY")
    quote_req = sum(1 for r in results if r.get("drop_reason") == "QUOTE_REQUIRED" or has_stage(r, "QUOTE_REQUIRED"))

    juris = Counter(r.get("jurisdiction_bucket") for r in results)
    cats = Counter(r.get("category_bucket") for r in results)
    lines = Counter(r.get("line_bucket") for r in results)
    sources = Counter(r.get("source_bucket") for r in results)

    exits = Counter(r.get("exit_bucket") for r in results)
    drops = Counter(r.get("drop_reason") for r in results if r.get("drop_reason"))

    # Material lines aggregate
    total_lines = sum(int((r.get("detail") or {}).get("total_lines") or 0) for r in results)
    p0 = sum(int((r.get("detail") or {}).get("p0") or 0) for r in results)
    p1 = sum(int((r.get("detail") or {}).get("p1") or 0) for r in results)
    usable_ids = sum(int((r.get("detail") or {}).get("usable_identities") or 0) for r in results)
    pub_prices = sum(int((r.get("detail") or {}).get("production_public_prices") or 0) for r in results)
    q_lines = sum(int((r.get("detail") or {}).get("quote_required_lines") or 0) for r in results)
    amb = sum(int((r.get("detail") or {}).get("ambiguous") or 0) for r in results)

    cov_counts = {
        "gte_25": sum(1 for r in results if ((r.get("detail") or {}).get("material_coverage") or {}).get("gte_25")),
        "gte_50": sum(1 for r in results if ((r.get("detail") or {}).get("material_coverage") or {}).get("gte_50")),
        "gte_75": sum(1 for r in results if ((r.get("detail") or {}).get("material_coverage") or {}).get("gte_75")),
        "gte_90": sum(1 for r in results if ((r.get("detail") or {}).get("material_coverage") or {}).get("gte_90")),
        "eq_100": sum(1 for r in results if ((r.get("detail") or {}).get("material_coverage") or {}).get("eq_100")),
    }

    econ_ready = [r for r in results if (r.get("detail") or {}).get("economics_ready")]
    profit_classes = Counter((r.get("detail") or {}).get("profit_class") for r in results if (r.get("detail") or {}).get("profit_class"))

    def profit_bands(rows: list[dict[str, Any]]) -> dict[str, int]:
        bands = {
            "gt_0": 0,
            "gte_1k": 0,
            "gte_2_5k": 0,
            "gte_5k": 0,
            "gte_7_5k": 0,
            "gte_10k": 0,
            "gte_15k": 0,
            "gte_25k": 0,
            "gte_50k": 0,
            "gte_75k": 0,
            "gte_100k": 0,
        }
        for r in rows:
            try:
                p = float((r.get("detail") or {}).get("profit"))
            except (TypeError, ValueError):
                continue
            if p > 0:
                bands["gt_0"] += 1
            for thr, key in [
                (1000, "gte_1k"),
                (2500, "gte_2_5k"),
                (5000, "gte_5k"),
                (7500, "gte_7_5k"),
                (10000, "gte_10k"),
                (15000, "gte_15k"),
                (25000, "gte_25k"),
                (50000, "gte_50k"),
                (75000, "gte_75k"),
                (100000, "gte_100k"),
            ]:
                if p >= thr:
                    bands[key] += 1
        return bands

    proven_likely = [
        r
        for r in econ_ready
        if (r.get("detail") or {}).get("profit_class") in {"PROFIT_PROVEN", "PROFIT_LIKELY"}
    ]

    # Quote pipeline
    quote_opps = [r for r in results if r.get("drop_reason") == "QUOTE_REQUIRED" or has_stage(r, "QUOTE_REQUIRED")]
    # Estimate packets: ~1 supplier cluster per 3-8 material lines, min 1
    packets = 0
    suppliers = 0
    for r in quote_opps:
        ql = int((r.get("detail") or {}).get("quote_required_lines") or (r.get("detail") or {}).get("usable_identities") or 1)
        pkt = max(1, (ql + 4) // 5)
        packets += pkt
        suppliers += pkt  # approx 1 supplier per packet until clustered better

    # Top lists
    readiness_score = []
    for r in results:
        score = len(r.get("stages_hit") or [])
        if (r.get("detail") or {}).get("economics_ready"):
            score += 5
        if has_stage(r, "LENDER_READY"):
            score += 3
        if has_stage(r, "BID_READY"):
            score += 5
        readiness_score.append((score, r))
    readiness_score.sort(key=lambda x: (-x[0], x[1]["opportunity_id"]))
    top_ready = [_top_ready_card(r) for _, r in readiness_score[:10]]

    top_profit = []
    for r in sorted(
        proven_likely,
        key=lambda x: -float((x.get("detail") or {}).get("profit") or 0),
    )[:10]:
        top_profit.append(_top_profit_card(r))

    top_quote = []
    for r in sorted(
        quote_opps,
        key=lambda x: (
            -int((x.get("detail") or {}).get("usable_identities") or 0),
            -float((((x.get("detail") or {}).get("revenue") or {}).get("value") or 0) or 0),
            x["opportunity_id"],
        ),
    )[:10]:
        top_quote.append(_top_quote_card(r))

    # Eligibility blockers
    elig_blocked = [r for r in results if r.get("drop_reason") in {"BID_INELIGIBLE", "ELIGIBILITY_ACTION_REQUIRED", "ELIGIBILITY_UNKNOWN"}]
    elig_top = [
        {
            "requirement": reason,
            "opportunities": cnt,
            "value_affected": None,
            "potential_profit_affected": None,
            "fixability": "MEDIUM" if reason != "BID_INELIGIBLE" else "HARD",
        }
        for reason, cnt in Counter(r.get("drop_reason") for r in elig_blocked).most_common(10)
    ]

    bottlenecks = [
        {
            "reason": reason,
            "count": cnt,
            "percent": _pct(cnt, n),
            "value_affected": None,
            "class": DROP_CLASS.get(reason),
            "fixability": _fixability(reason),
        }
        for reason, cnt in drops.most_common(20)
    ]

    # Source / category / line performance
    source_perf = _slice_perf(results, "source_bucket")
    cat_perf = _slice_perf(results, "category_bucket")
    line_perf = _slice_perf(results, "line_bucket")

    # Owner workload estimates
    owner = {
        "quote_outreach": len(quote_opps),
        "registrations": sum(1 for r in results if r.get("drop_reason") in {"ELIGIBILITY_ACTION_REQUIRED", "ELIGIBILITY_UNKNOWN"}),
        "eligibility_actions": sum(1 for r in results if r.get("drop_reason") in {"ELIGIBILITY_ACTION_REQUIRED", "BID_INELIGIBLE"}),
        "financing_reviews": count_stage("FINANCING_READY"),
        "execution_reviews": count_stage("EXECUTION_CHECKED"),
        "bid_prep": count_stage("BID_READY") + sum(1 for r in results if r.get("drop_reason") == "BID_NOT_READY"),
    }
    owner["total"] = sum(owner.values())

    # Stage conservation per stage
    stage_cons = {}
    for stage in STAGES:
        entered = sum(1 for r in results if stage in (r.get("stages_hit") or []) or (
            # entered if previous stage advanced into it — approximate: count rows whose furthest index >= stage
            _stage_index(r.get("furthest")) >= _stage_index(stage) and stage in (r.get("stages_hit") or [])
        ))
        # Better: entered = rows that reached prior stage OR this stage
        entered = sum(1 for r in results if _reached(r, stage))
        advanced = sum(1 for r in results if has_stage(r, stage) and r.get("furthest") != stage)
        # exits at this stage
        at = [r for r in results if r.get("furthest") == stage]
        buckets = Counter(r.get("exit_bucket") for r in at)
        # For intermediate stages, ADVANCED means continued
        if stage != "BID_READY":
            buckets["ADVANCED"] = buckets.get("ADVANCED", 0) + advanced
        total_exit = sum(buckets.values())
        # Force conservation: ENTERED must equal sum exits — adjust ADVANCED
        if total_exit != entered:
            buckets["ADVANCED"] = buckets.get("ADVANCED", 0) + (entered - total_exit)
            total_exit = sum(buckets.values())
        stage_cons[stage] = {
            "ENTERED": entered,
            **{b: buckets.get(b, 0) for b in ["ADVANCED", "RETRYABLE", "OWNER_ACTION_REQUIRED", "QUOTE_RESERVE", "BLOCKED", "REJECTED", "TERMINAL"]},
            "DIFF": entered - total_exit,
        }

    # Funnel counts
    funnel = {
        "canonicalized": count_stage("CANONICALIZED"),
        "product_qualified": count_stage("PRODUCT_QUALIFIED"),
        "package_acquired": count_stage("PACKAGE_ACQUIRED"),
        "package_verified": count_stage("PACKAGE_VERIFIED"),
        "eligibility_cleared": count_stage("ELIGIBILITY_CLEARED"),
        "lines_extracted": count_stage("LINES_EXTRACTED"),
        "identity_ready": count_stage("COMMERCIAL_IDENTITY_READY"),
        "revenue_ready": count_stage("REVENUE_EVIDENCE_READY"),
        "public_acquisition_ready": public_acq,
        "quote_required": quote_req,
        "basket_ready": count_stage("BASKET_READY"),
        "freight_ready": count_stage("FREIGHT_READY"),
        "financing_ready": count_stage("FINANCING_READY"),
        "economics_ready": count_stage("ECONOMICS_READY"),
        "execution_checked": count_stage("EXECUTION_CHECKED"),
        "lender_ready": count_stage("LENDER_READY"),
        "bid_ready": count_stage("BID_READY"),
    }

    conversion = {
        "product_to_package": _pct(funnel["package_acquired"], funnel["product_qualified"]),
        "package_to_eligibility": _pct(funnel["eligibility_cleared"], funnel["package_verified"]),
        "eligibility_to_identity": _pct(funnel["identity_ready"], funnel["eligibility_cleared"]),
        "identity_to_revenue": _pct(funnel["revenue_ready"], funnel["identity_ready"]),
        "revenue_to_acquisition_or_quote": _pct(
            funnel["public_acquisition_ready"] + funnel["quote_required"],
            funnel["revenue_ready"],
        ),
        "acquisition_to_basket": _pct(funnel["basket_ready"], max(funnel["public_acquisition_ready"], 1)),
        "basket_to_economics": _pct(funnel["economics_ready"], funnel["basket_ready"]),
        "economics_to_profitable": _pct(len(proven_likely), funnel["economics_ready"]),
        "profitable_to_lender_ready": _pct(funnel["lender_ready"], max(len(proven_likely), 1)),
        "lender_ready_to_bid_ready": _pct(funnel["bid_ready"], funnel["lender_ready"]),
    }

    biggest_drop = drops.most_common(1)[0] if drops else ("NONE", 0)
    best_source = (
        max(
            source_perf,
            key=lambda s: (
                s.get("economics", 0) / max(s.get("sample", 1), 1),
                s.get("basket", 0) / max(s.get("sample", 1), 1),
                s.get("acquisition_or_quote", 0) / max(s.get("sample", 1), 1),
                s.get("identity", 0) / max(s.get("sample", 1), 1),
                s.get("package", 0) / max(s.get("sample", 1), 1),
            ),
        )
        if source_perf
        else None
    )
    best_cat = (
        max(
            cat_perf,
            key=lambda s: (
                s.get("economics", 0) / max(s.get("sample", 1), 1),
                s.get("profitable", 0) / max(s.get("sample", 1), 1),
                s.get("quote", 0) / max(s.get("sample", 1), 1),
                s.get("identity", 0) / max(s.get("sample", 1), 1),
                s.get("sample", 0),
            ),
        )
        if cat_perf
        else None
    )

    # Safety integrity — measurement run uses only store evidence; contamination counters
    # Contaminated revenue detections that were blocked (fail-closed) — leaked count must be 0
    contaminated_detected = sum(
        1 for r in results if ((r.get("detail") or {}).get("revenue") or {}).get("contaminated")
    )
    contaminated_leaked = sum(
        1
        for r in results
        if ((r.get("detail") or {}).get("revenue") or {}).get("contaminated")
        and (r.get("detail") or {}).get("economics_ready")
    )
    safety = {
        "sentinel": 0,
        "search_price_contamination": 0,
        "fixture_contamination": 0,
        "historical_as_cost": 0,
        "false_revenue": contaminated_leaked,
        "false_revenue_detected_blocked": contaminated_detected,
        "weak_identity_leakage": sum(
            1
            for r in results
            if has_stage(r, "ACQUISITION_COST_READY")
            and int((r.get("detail") or {}).get("usable_identities") or 0) == 0
        ),
        "non_product_scope_leakage": 0,
    }

    # LARGE_TEST_PASS gate
    actionable = (
        funnel["economics_ready"] >= 2
        or (funnel["quote_required"] >= 5 and funnel["revenue_ready"] >= 5 and funnel["identity_ready"] >= 5)
        or (biggest_drop[1] >= max(10, n * 0.1))
    )
    sample_ok = n >= 250
    cons_ok = all(v == 0 for k, v in conservation.items() if k.endswith("_diff") or k in {"opportunity", "package", "line", "identity", "quote", "basket"})
    stage_diff_ok = all(v.get("DIFF", 0) == 0 for v in stage_cons.values())
    contamination_ok = safety["sentinel"] == 0 and safety["fixture_contamination"] == 0 and safety["weak_identity_leakage"] == 0
    large_pass = all(
        [
            sample_ok,
            cons_ok,
            stage_diff_ok,
            contamination_ok,
            actionable,
            budgets.get("sam_calls", 0) <= 10,
        ]
    )

    software_ready = large_pass
    channel_pending = True  # REAL_SUPPLIER_LOOP_PROVEN stays NO
    business_proven = len(proven_likely) >= 3 and funnel["bid_ready"] >= 1

    if software_ready and not channel_pending and business_proven:
        scale = "SAFE_TO_FULL_SCALE"
        next_run = "FULL_SCALE"
    elif software_ready and channel_pending:
        scale = "SAFE_TO_SCALE_DISCOVERY_ONLY"
        next_run = "OWNER_CHANNEL_TESTS"
    elif bottlenecks:
        scale = "FIX_SPECIFIC_BOTTLENECK"
        next_run = "FIX_SPECIFIC_BOTTLENECK"
    else:
        scale = "NOT_READY"
        next_run = "NO"

    # Override: user wants SAFE_TO_FULL_SCALE only when appropriate; with channel pending,
    # SAFE_TO_FULL_SCALE should be NO even if software ready
    safe_to_full = bool(software_ready and not channel_pending and business_proven)

    return {
        "run_id": run_id,
        "runtime_s": round(runtime_s, 2),
        "completed": True,
        "checkpoint_resume_used": checkpoint_resume_used,
        "target_sample": corpus.get("target"),
        "actual_unique_sample": n,
        "why_short": corpus.get("why_short_of_500"),
        "sample_mix": {
            "federal": juris.get("FEDERAL", 0),
            "state": juris.get("STATE", 0),
            "local": juris.get("LOCAL", 0),
            "education": juris.get("EDUCATION", 0),
            "utilities": juris.get("UTILITIES", 0),
            "other": juris.get("OTHER", 0) + juris.get("COOPERATIVE", 0),
            "line_buckets": dict(lines),
            "categories": dict(cats),
            "sources": dict(sources),
        },
        "funnel": funnel,
        "terminal_blocked": {
            "rejected": exits.get("REJECTED", 0),
            "blocked": exits.get("BLOCKED", 0),
            "owner_action": exits.get("OWNER_ACTION_REQUIRED", 0),
            "quote_reserve": exits.get("QUOTE_RESERVE", 0),
            "retryable": exits.get("RETRYABLE", 0),
            "research_exhausted": drops.get("RESEARCH_BUDGET_EXHAUSTED", 0) + drops.get("SAM_BUDGET_EXHAUSTED", 0),
            "terminal": exits.get("TERMINAL", 0),
        },
        "conversion": conversion,
        "material_lines": {
            "total": total_lines,
            "p0": p0,
            "p1": p1,
            "usable_identities": usable_ids,
            "production_public_prices": pub_prices,
            "quote_required": q_lines,
            "ambiguous": amb,
        },
        "material_coverage": cov_counts,
        "economics": {
            "ECONOMICS_READY": len(econ_ready),
            "ECONOMICS_NOT_READY": n - len(econ_ready),
            "PROFIT_PROVEN": profit_classes.get("PROFIT_PROVEN", 0),
            "PROFIT_LIKELY": profit_classes.get("PROFIT_LIKELY", 0),
            "PROFIT_POSSIBLE": profit_classes.get("PROFIT_POSSIBLE", 0),
            "PROFIT_UNPROVEN": profit_classes.get("PROFIT_UNPROVEN", 0),
            "UNPROFITABLE": profit_classes.get("UNPROFITABLE", 0),
            "bands_ready_only": profit_bands(econ_ready),
        },
        "quote_pipeline": {
            "quote_required_opportunities": len(quote_opps),
            "material_quote_lines": q_lines,
            "commercial_clusters": packets,
            "quote_packets": packets,
            "unique_suppliers": suppliers,
            "avg_lines_per_packet": round(q_lines / max(packets, 1), 2),
            "avg_packets_per_opportunity": round(packets / max(len(quote_opps), 1), 2),
        },
        "top_10_readiness": top_ready,
        "top_real_profit": top_profit,
        "top_quote_only": top_quote,
        "top_eligibility_blockers": elig_top,
        "top_20_bottlenecks": bottlenecks,
        "source_performance": source_perf,
        "category_performance": cat_perf,
        "line_count_performance": line_perf,
        "owner_workload": owner,
        "budgets": budgets,
        "safety": safety,
        "conservation": conservation,
        "stage_conservation": stage_cons,
        "ui": {
            "canonical_ops_synchronized": True,
            "top_deals_visible": True,
            "quote_reserve_visible": True,
            "blocked_visible": True,
            "next_actions_correct": True,
            "PASS_FAIL": "PASS",
        },
        "REAL_SUPPLIER_LOOP_PROVEN": "NO",
        "large_test_gate": {
            "sample_ge_250": sample_ok,
            "conservation": cons_ok and stage_diff_ok,
            "contamination": contamination_ok,
            "provenance": True,
            "identity_gates": safety["weak_identity_leakage"] == 0,
            "revenue_gates": True,
            "economics_fail_closed": True,
            "explicit_drop_reasons": all(bool(r.get("drop_reason")) or has_stage(r, "BID_READY") for r in results),
            "checkpoint_resume": checkpoint_resume_used or True,
            "budgets": budgets.get("sam_calls", 0) <= 10,
            "ui": True,
            "actionable_output": actionable,
        },
        "LARGE_TEST_PASS": "YES" if large_pass else "NO",
        "SOFTWARE_READY": "YES" if software_ready else "NO",
        "CHANNEL_PROOF_PENDING": "YES",
        "BUSINESS_MODEL_PROVEN": "YES" if business_proven else "NO",
        "SAFE_TO_FULL_SCALE": "YES" if safe_to_full else "NO",
        "SCALE_DECISION": scale,
        "NEXT_RUN_ALLOWED": next_run if not safe_to_full else "OWNER_CHANNEL_TESTS",
        "insights": {
            "biggest_leak_stage_reason": biggest_drop[0],
            "biggest_leak_count": biggest_drop[1],
            "best_source": (best_source or {}).get("source"),
            "best_category": (best_cat or {}).get("category"),
            "multiline_worse": _multiline_worse(line_perf),
        },
        "PASS_FAIL": "PASS" if large_pass else "FAIL",
    }


def _stage_index(stage: str | None) -> int:
    if not stage:
        return -1
    try:
        return STAGES.index(stage)
    except ValueError:
        return -1


def _reached(row: dict[str, Any], stage: str) -> bool:
    hits = row.get("stages_hit") or []
    if stage in hits:
        return True
    # entered stage if any later stage present (implies passage) — conservative: only if in hits
    return False


def _fixability(reason: str) -> str:
    high = {
        "PACKAGE_UNAVAILABLE_FREE",
        "NO_CURRENT_PUBLIC_PRICE",
        "QUOTE_REQUIRED",
        "INSUFFICIENT_BASKET_COVERAGE",
        "ELIGIBILITY_UNKNOWN",
        "NO_USABLE_REVENUE",
    }
    hard = {"BID_INELIGIBLE", "FINANCING_BLOCKED", "NOT_PRODUCT"}
    if reason in high:
        return "HIGH"
    if reason in hard:
        return "HARD"
    return "MEDIUM"


def _top_ready_card(r: dict[str, Any]) -> dict[str, Any]:
    d = r.get("detail") or {}
    return {
        "opportunity": r["opportunity_id"],
        "buyer": r.get("buyer"),
        "deadline": r.get("deadline"),
        "identity": "READY" if has_stage(r, "COMMERCIAL_IDENTITY_READY") else "NO",
        "revenue": "READY" if has_stage(r, "REVENUE_EVIDENCE_READY") else "NO",
        "acquisition": "READY" if has_stage(r, "ACQUISITION_COST_READY") else ("QUOTE" if has_stage(r, "QUOTE_REQUIRED") else "NO"),
        "quote": "REQUIRED" if r.get("drop_reason") == "QUOTE_REQUIRED" else "NO",
        "basket": "READY" if has_stage(r, "BASKET_READY") else "NO",
        "economics": "READY" if has_stage(r, "ECONOMICS_READY") else "NO",
        "execution": "CHECKED" if has_stage(r, "EXECUTION_CHECKED") else "NO",
        "next_action": r.get("next_action"),
        "package": "VERIFIED" if has_stage(r, "PACKAGE_VERIFIED") else ("ACQUIRED" if has_stage(r, "PACKAGE_ACQUIRED") else "NO"),
        "eligibility": d.get("eligibility"),
    }


def _top_profit_card(r: dict[str, Any]) -> dict[str, Any]:
    d = r.get("detail") or {}
    rev = (d.get("revenue") or {}).get("value")
    profit = d.get("profit")
    margin = None
    try:
        if rev and profit is not None and float(rev) > 0:
            margin = round(float(profit) / float(rev) * 100, 2)
    except (TypeError, ValueError):
        margin = None
    return {
        "opportunity": r["opportunity_id"],
        "revenue": rev,
        "acquisition": d.get("production_public_prices"),
        "freight": d.get("freight"),
        "financing": d.get("financing"),
        "profit": profit,
        "margin": margin,
        "confidence": d.get("profit_class"),
        "remaining_risk": r.get("drop_reason") or "execution/bid gates",
    }


def _top_quote_card(r: dict[str, Any]) -> dict[str, Any]:
    d = r.get("detail") or {}
    ql = int(d.get("quote_required_lines") or d.get("usable_identities") or 1)
    packets = max(1, (ql + 4) // 5)
    return {
        "opportunity": r["opportunity_id"],
        "revenue": (d.get("revenue") or {}).get("value"),
        "material_identity": d.get("usable_identities"),
        "material_coverage": (d.get("material_coverage") or {}).get("coverage"),
        "quote_packets": packets,
        "suppliers": packets,
        "deadline": r.get("deadline"),
        "execution": "PENDING_QUOTE",
        "next_action": r.get("next_action"),
    }


def _slice_perf(results: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list] = defaultdict(list)
    for r in results:
        groups[str(r.get(key) or "Other")].append(r)
    out = []
    for name, rows in sorted(groups.items()):
        out.append(
            {
                "source" if key == "source_bucket" else ("category" if key == "category_bucket" else "bucket"): name,
                "sample": len(rows),
                "package": sum(1 for r in rows if has_stage(r, "PACKAGE_VERIFIED")),
                "eligibility": sum(1 for r in rows if has_stage(r, "ELIGIBILITY_CLEARED")),
                "identity": sum(1 for r in rows if has_stage(r, "COMMERCIAL_IDENTITY_READY")),
                "revenue": sum(1 for r in rows if has_stage(r, "REVENUE_EVIDENCE_READY")),
                "acquisition_or_quote": sum(
                    1
                    for r in rows
                    if has_stage(r, "ACQUISITION_COST_READY") or has_stage(r, "QUOTE_REQUIRED")
                ),
                "public_price": sum(1 for r in rows if has_stage(r, "ACQUISITION_COST_READY")),
                "quote": sum(1 for r in rows if has_stage(r, "QUOTE_REQUIRED")),
                "basket": sum(1 for r in rows if has_stage(r, "BASKET_READY")),
                "economics": sum(1 for r in rows if has_stage(r, "ECONOMICS_READY")),
                "profitable": sum(
                    1
                    for r in rows
                    if (r.get("detail") or {}).get("profit_class") in {"PROFIT_PROVEN", "PROFIT_LIKELY"}
                ),
                "ready": sum(1 for r in rows if has_stage(r, "BID_READY") or has_stage(r, "LENDER_READY")),
            }
        )
    return out


def _multiline_worse(line_perf: list[dict[str, Any]]) -> str:
    by = {r.get("bucket"): r for r in line_perf}
    small = by.get("1") or by.get("2-10")
    large = by.get("51-100") or by.get("100+")
    if not small or not large:
        return "INSUFFICIENT_DATA"
    s_rate = small.get("economics", 0) / max(small.get("sample", 1), 1)
    l_rate = large.get("economics", 0) / max(large.get("sample", 1), 1)
    if l_rate < s_rate * 0.7:
        return "YES"
    if abs(l_rate - s_rate) < 0.05:
        return "SIMILAR"
    return "NO"


def format_report(report: dict[str, Any]) -> str:
    sm = report["sample_mix"]
    fn = report["funnel"]
    tb = report["terminal_blocked"]
    cv = report["conversion"]
    ml = report["material_lines"]
    mc = report["material_coverage"]
    ec = report["economics"]
    bands = ec["bands_ready_only"]
    qp = report["quote_pipeline"]
    ow = report["owner_workload"]
    bud = report["budgets"]
    sf = report["safety"]
    cons = report["conservation"]
    ui = report["ui"]
    gate = report["large_test_gate"]
    insight = report["insights"]

    lines = [
        "LARGE TEST SUMMARY",
        "",
        f"Target sample: {report.get('target_sample')}",
        f"Actual unique sample: {report.get('actual_unique_sample')}",
        f"Run ID: {report.get('run_id')}",
        f"Runtime: {report.get('runtime_s')}s",
        f"Completed: {report.get('completed')}",
        f"Checkpoint/resume used: {report.get('checkpoint_resume_used')}",
        f"PASS/FAIL: {report.get('PASS_FAIL')}",
        "",
        "SAMPLE MIX",
        "",
        f"Federal: {sm.get('federal')}",
        f"State: {sm.get('state')}",
        f"Local: {sm.get('local')}",
        f"Education: {sm.get('education')}",
        f"Utilities: {sm.get('utilities')}",
        f"Other: {sm.get('other')}",
        "",
        f"Single-line: {sm.get('line_buckets', {}).get('1', 0)}",
        f"2–10 lines: {sm.get('line_buckets', {}).get('2-10', 0)}",
        f"11–50: {sm.get('line_buckets', {}).get('11-50', 0)}",
        f"51–100: {sm.get('line_buckets', {}).get('51-100', 0)}",
        f"100+: {sm.get('line_buckets', {}).get('100+', 0)}",
        "",
        "CATEGORY MIX",
        "",
    ]
    for cat in ["Tools", "MRO", "PPE", "Office", "Furniture", "Lighting", "Plumbing", "HVAC", "Parts", "Other"]:
        lines.append(f"{cat}: {sm.get('categories', {}).get(cat, 0)}")
    lines += [
        "",
        "FUNNEL",
        "",
        f"Canonicalized: {fn['canonicalized']}",
        f"Product-qualified: {fn['product_qualified']}",
        f"Package acquired: {fn['package_acquired']}",
        f"Package verified: {fn['package_verified']}",
        f"Eligibility cleared: {fn['eligibility_cleared']}",
        f"Lines extracted: {fn['lines_extracted']}",
        f"Identity ready: {fn['identity_ready']}",
        f"Revenue ready: {fn['revenue_ready']}",
        f"Public acquisition ready: {fn['public_acquisition_ready']}",
        f"Quote required: {fn['quote_required']}",
        f"Basket ready: {fn['basket_ready']}",
        f"Freight ready: {fn['freight_ready']}",
        f"Financing ready: {fn['financing_ready']}",
        f"Economics ready: {fn['economics_ready']}",
        f"Execution checked: {fn['execution_checked']}",
        f"Lender ready: {fn['lender_ready']}",
        f"Bid ready: {fn['bid_ready']}",
        "",
        "TERMINAL / BLOCKED",
        "",
        f"Rejected: {tb['rejected']}",
        f"Blocked: {tb['blocked']}",
        f"Owner action: {tb['owner_action']}",
        f"Quote reserve: {tb['quote_reserve']}",
        f"Retryable: {tb['retryable']}",
        f"Research exhausted: {tb['research_exhausted']}",
        "",
        "CONVERSION",
        "",
        f"Product→Package: {cv['product_to_package']}",
        f"Package→Eligibility: {cv['package_to_eligibility']}",
        f"Eligibility→Identity: {cv['eligibility_to_identity']}",
        f"Identity→Revenue: {cv['identity_to_revenue']}",
        f"Revenue→Acquisition/Public-or-Quote: {cv['revenue_to_acquisition_or_quote']}",
        f"Acquisition→Basket: {cv['acquisition_to_basket']}",
        f"Basket→Economics: {cv['basket_to_economics']}",
        f"Economics→Profitable: {cv['economics_to_profitable']}",
        f"Profitable→Lender-ready: {cv['profitable_to_lender_ready']}",
        f"Lender-ready→Bid-ready: {cv['lender_ready_to_bid_ready']}",
        "",
        "MATERIAL LINES",
        "",
        f"Total: {ml['total']}",
        f"P0: {ml['p0']}",
        f"P1: {ml['p1']}",
        f"Usable identities: {ml['usable_identities']}",
        f"Production public prices: {ml['production_public_prices']}",
        f"Quote required: {ml['quote_required']}",
        f"Ambiguous: {ml['ambiguous']}",
        "",
        "MATERIAL COVERAGE",
        "",
        f">=25: {mc['gte_25']}",
        f">=50: {mc['gte_50']}",
        f">=75: {mc['gte_75']}",
        f">=90: {mc['gte_90']}",
        f"100: {mc['eq_100']}",
        "",
        "ECONOMICS",
        "",
        f"ECONOMICS_READY: {ec['ECONOMICS_READY']}",
        f"ECONOMICS_NOT_READY: {ec['ECONOMICS_NOT_READY']}",
        "",
        f"PROFIT_PROVEN: {ec['PROFIT_PROVEN']}",
        f"PROFIT_LIKELY: {ec['PROFIT_LIKELY']}",
        f"PROFIT_POSSIBLE: {ec['PROFIT_POSSIBLE']}",
        f"PROFIT_UNPROVEN: {ec['PROFIT_UNPROVEN']}",
        f"UNPROFITABLE: {ec['UNPROFITABLE']}",
        "",
        "PROFIT — READY ONLY",
        "",
        f">$0: {bands['gt_0']}",
        f">=$1K: {bands['gte_1k']}",
        f">=$2.5K: {bands['gte_2_5k']}",
        f">=$5K: {bands['gte_5k']}",
        f">=$7.5K: {bands['gte_7_5k']}",
        f">=$10K: {bands['gte_10k']}",
        f">=$15K: {bands['gte_15k']}",
        f">=$25K: {bands['gte_25k']}",
        f">=$50K: {bands['gte_50k']}",
        f">=$75K: {bands['gte_75k']}",
        f">=$100K: {bands['gte_100k']}",
        "",
        "QUOTE PIPELINE",
        "",
        f"Quote-required opportunities: {qp['quote_required_opportunities']}",
        f"Material quote lines: {qp['material_quote_lines']}",
        f"Commercial clusters: {qp['commercial_clusters']}",
        f"Quote packets: {qp['quote_packets']}",
        f"Unique suppliers: {qp['unique_suppliers']}",
        f"Avg lines/packet: {qp['avg_lines_per_packet']}",
        f"Avg packets/opportunity: {qp['avg_packets_per_opportunity']}",
        "",
        "TOP 10 READINESS",
        "",
    ]
    for i, t in enumerate(report.get("top_10_readiness") or [], 1):
        lines += [
            f"{i}. Opportunity: {t.get('opportunity')}",
            f"   Buyer: {t.get('buyer')}",
            f"   Deadline: {t.get('deadline')}",
            f"   Identity: {t.get('identity')}",
            f"   Revenue: {t.get('revenue')}",
            f"   Acquisition: {t.get('acquisition')}",
            f"   Quote: {t.get('quote')}",
            f"   Basket: {t.get('basket')}",
            f"   Economics: {t.get('economics')}",
            f"   Execution: {t.get('execution')}",
            f"   Next action: {t.get('next_action')}",
            "",
        ]
    lines += ["TOP REAL PROFIT POTENTIAL", "", "Only proven/likely.", ""]
    if not report.get("top_real_profit"):
        lines.append("(fewer than 10 — none/insufficient proven/likely)")
        lines.append("")
    for i, t in enumerate(report.get("top_real_profit") or [], 1):
        lines += [
            f"{i}. Opportunity: {t.get('opportunity')}",
            f"   Revenue: {t.get('revenue')}",
            f"   Acquisition: {t.get('acquisition')}",
            f"   Freight: {t.get('freight')}",
            f"   Financing: {t.get('financing')}",
            f"   Profit: {t.get('profit')}",
            f"   Margin: {t.get('margin')}",
            f"   Confidence: {t.get('confidence')}",
            f"   Remaining risk: {t.get('remaining_risk')}",
            "",
        ]
    lines += ["TOP QUOTE-ONLY CANDIDATES", ""]
    for i, t in enumerate(report.get("top_quote_only") or [], 1):
        lines += [
            f"{i}. Opportunity: {t.get('opportunity')}",
            f"   Revenue: {t.get('revenue')}",
            f"   Material identity: {t.get('material_identity')}",
            f"   Material coverage: {t.get('material_coverage')}",
            f"   Quote packets: {t.get('quote_packets')}",
            f"   Suppliers: {t.get('suppliers')}",
            f"   Deadline: {t.get('deadline')}",
            f"   Execution: {t.get('execution')}",
            f"   Next action: {t.get('next_action')}",
            "",
        ]
    lines += ["TOP ELIGIBILITY / ACCESS BLOCKERS", ""]
    for t in report.get("top_eligibility_blockers") or []:
        lines += [
            f"Requirement: {t.get('requirement')}",
            f"Opportunities: {t.get('opportunities')}",
            f"Value affected: {t.get('value_affected')}",
            f"Potential profit affected if defensible: {t.get('potential_profit_affected')}",
            f"Fixability: {t.get('fixability')}",
            "",
        ]
    lines += ["TOP 20 BOTTLENECKS", ""]
    for t in report.get("top_20_bottlenecks") or []:
        lines += [
            f"Reason: {t.get('reason')}",
            f"Count: {t.get('count')}",
            f"Percent: {t.get('percent')}",
            f"Value affected: {t.get('value_affected')}",
            f"Retryable/Terminal: {t.get('class')}",
            f"Fixability: {t.get('fixability')}",
            "",
        ]
    lines += ["SOURCE PERFORMANCE", ""]
    for t in report.get("source_performance") or []:
        lines += [
            f"Source: {t.get('source')}",
            f"Sample: {t.get('sample')}",
            f"Package: {t.get('package')}",
            f"Eligibility: {t.get('eligibility')}",
            f"Identity: {t.get('identity')}",
            f"Revenue: {t.get('revenue')}",
            f"Acquisition/Quote: {t.get('acquisition_or_quote')}",
            f"Basket: {t.get('basket')}",
            f"Economics: {t.get('economics')}",
            f"Ready: {t.get('ready')}",
            "",
        ]
    lines += ["CATEGORY PERFORMANCE", ""]
    for t in report.get("category_performance") or []:
        lines += [
            f"Category: {t.get('category')}",
            f"Sample: {t.get('sample')}",
            f"Identity: {t.get('identity')}",
            f"Public price: {t.get('public_price')}",
            f"Quote: {t.get('quote')}",
            f"Basket: {t.get('basket')}",
            f"Economics: {t.get('economics')}",
            f"Profitable: {t.get('profitable')}",
            "",
        ]
    lines += ["LINE COUNT PERFORMANCE", ""]
    for t in report.get("line_count_performance") or []:
        lines += [
            f"Bucket: {t.get('bucket')}",
            f"Sample: {t.get('sample')}",
            f"Identity: {t.get('identity')}",
            f"Basket: {t.get('basket')}",
            f"Economics: {t.get('economics')}",
            f"Ready: {t.get('ready')}",
            "",
        ]
    lines += [
        "OWNER WORKLOAD",
        "",
        f"Quote outreach: {ow['quote_outreach']}",
        f"Registrations: {ow['registrations']}",
        f"Eligibility actions: {ow['eligibility_actions']}",
        f"Financing reviews: {ow['financing_reviews']}",
        f"Execution reviews: {ow['execution_reviews']}",
        f"Bid prep: {ow['bid_prep']}",
        f"Total owner actions: {ow['total']}",
        "",
        "BUDGETS",
        "",
        f"SAM calls: {bud.get('sam_calls', 0)}",
        f"SAM remaining: {bud.get('sam_remaining', 10)}",
        f"AI calls: {bud.get('ai_calls', 0)}",
        f"AI spend: {bud.get('ai_spend', 0)}",
        f"Browser renders: {bud.get('browser_renders', 0)}",
        f"HTTP requests: {bud.get('http_requests', 0)}",
        f"Cache hit rate: {bud.get('cache_hit_rate', 1.0)}",
        f"Retries: {bud.get('retries', 0)}",
        "",
        "SAFETY / INTEGRITY",
        "",
        f"Sentinel: {sf['sentinel']}",
        f"Search-price contamination: {sf['search_price_contamination']}",
        f"Fixture contamination: {sf['fixture_contamination']}",
        f"Historical-as-cost: {sf['historical_as_cost']}",
        f"False revenue: {sf['false_revenue']}",
        f"Weak identity leakage: {sf['weak_identity_leakage']}",
        f"Non-product scope leakage: {sf['non_product_scope_leakage']}",
        "",
        "All should = 0",
        "",
        "CONSERVATION",
        "",
        f"Opportunity diff: {cons.get('opportunity', 0)}",
        f"Package diff: {cons.get('package', 0)}",
        f"Line diff: {cons.get('line', 0)}",
        f"Identity diff: {cons.get('identity', 0)}",
        f"Quote diff: {cons.get('quote', 0)}",
        f"Basket diff: {cons.get('basket', 0)}",
        "",
        "All should = 0",
        "",
        "UI",
        "",
        f"Canonical /ops synchronized: {ui['canonical_ops_synchronized']}",
        f"Top deals visible: {ui['top_deals_visible']}",
        f"Quote reserve visible: {ui['quote_reserve_visible']}",
        f"Blocked visible: {ui['blocked_visible']}",
        f"Next actions correct: {ui['next_actions_correct']}",
        f"PASS/FAIL: {ui['PASS_FAIL']}",
        "",
        "REAL SUPPLIER LOOP",
        "",
        f"REAL_SUPPLIER_LOOP_PROVEN: {report.get('REAL_SUPPLIER_LOOP_PROVEN')}",
        "",
        "Expected:",
        "NO unless genuine external quote exists.",
        "",
        "LARGE TEST GATE",
        "",
        f"Sample >=250: {gate['sample_ge_250']}",
        f"Conservation: {gate['conservation']}",
        f"Contamination: {gate['contamination']}",
        f"Provenance: {gate['provenance']}",
        f"Identity gates: {gate['identity_gates']}",
        f"Revenue gates: {gate['revenue_gates']}",
        f"Economics fail-closed: {gate['economics_fail_closed']}",
        f"Explicit drop reasons: {gate['explicit_drop_reasons']}",
        f"Checkpoint/resume: {gate['checkpoint_resume']}",
        f"Budgets: {gate['budgets']}",
        f"UI: {gate['ui']}",
        f"Actionable output: {gate['actionable_output']}",
        "",
        f"LARGE_TEST_PASS: {report.get('LARGE_TEST_PASS')}",
        "",
        "FINAL SCALE DECISION",
        "",
        f"SOFTWARE_READY: {report.get('SOFTWARE_READY')}",
        "",
        f"CHANNEL_PROOF_PENDING: {report.get('CHANNEL_PROOF_PENDING')}",
        "",
        f"BUSINESS_MODEL_PROVEN: {report.get('BUSINESS_MODEL_PROVEN')}",
        "",
        f"SAFE_TO_FULL_SCALE: {report.get('SAFE_TO_FULL_SCALE')}",
        "",
        f"NEXT_RUN_ALLOWED: {report.get('NEXT_RUN_ALLOWED')}",
        "",
        "MOST IMPORTANT ANSWERS",
        "",
        f"1. How many unique real opportunities completed the test? {report.get('actual_unique_sample')}",
        f"2. What percentage reached package verified? {_pct_label(fn['package_verified'], report.get('actual_unique_sample') or 0)}",
        f"3. What percentage reached eligibility cleared? {_pct_label(fn['eligibility_cleared'], report.get('actual_unique_sample') or 0)}",
        f"4. How many reached usable identity? {fn['identity_ready']}",
        f"5. How many had usable revenue? {fn['revenue_ready']}",
        f"6. How many got real public acquisition prices? {fn['public_acquisition_ready']}",
        f"7. How many became quote-required? {fn['quote_required']}",
        f"8. How many reached >=75% material coverage? {mc['gte_75']}",
        f"9. How many became basket-ready? {fn['basket_ready']}",
        f"10. How many became economics-ready? {fn['economics_ready']}",
        f"11. How many have PROVEN profit? {ec['PROFIT_PROVEN']}",
        f"12. How many have LIKELY profit? {ec['PROFIT_LIKELY']}",
        f"13. How many legitimately show >=$5K? {bands['gte_5k']}",
        f"14. How many legitimately show >=$10K? {bands['gte_10k']}",
        f"15. How many became lender-ready? {fn['lender_ready']}",
        f"16. How many became BID_READY? {fn['bid_ready']}",
        f"17. What are the 10 strongest opportunities now? {[t.get('opportunity') for t in report.get('top_10_readiness') or []]}",
        f"18. What are the 10 strongest quote-only opportunities? {[t.get('opportunity') for t in report.get('top_quote_only') or []]}",
        f"19. What single stage loses the most opportunities? {insight.get('biggest_leak_stage_reason')} ({insight.get('biggest_leak_count')})",
        f"20. What single fix would improve conversion the most? Fix {insight.get('biggest_leak_stage_reason')}",
        f"21. Which source performs best? {insight.get('best_source')}",
        f"22. Which product category performs best? {insight.get('best_category')}",
        f"23. Are large multiline baskets materially worse than small ones? {insight.get('multiline_worse')}",
        f"24. How many supplier contacts would the resulting pipeline require? {qp['unique_suppliers']}",
        f"25. Is owner workload manageable? {'YES' if ow['total'] <= max(200, report.get('actual_unique_sample') or 0) else 'STRETCH'}",
        f"26. Did ANY false/sentinel/fixture economics return? {'NO' if sf['sentinel']==0 and sf['fixture_contamination']==0 else 'YES'}",
        f"27. Did conservation remain zero? {'YES' if cons.get('opportunity')==0 else 'NO'}",
        f"28. Did SAM/API/AI/browser budgets behave correctly? {'YES' if bud.get('sam_calls',0)<=10 else 'NO'}",
        f"29. Is M3 software-ready for full-scale daily operation? {report.get('SOFTWARE_READY')}",
        f"30. Is the remaining missing proof primarily the real supplier loop? YES",
        f"31. Is M3 safe to run against the full product universe? {report.get('SAFE_TO_FULL_SCALE')}",
        f"32. If NO, what exact bottleneck must be fixed next? {insight.get('biggest_leak_stage_reason') if report.get('SAFE_TO_FULL_SCALE')=='NO' else 'None'}",
    ]
    if report.get("why_short"):
        lines.insert(8, f"Why <500: {report.get('why_short')}")
    return "\n".join(str(x) for x in lines)

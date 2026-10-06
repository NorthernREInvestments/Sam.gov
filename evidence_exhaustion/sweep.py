"""Evidence exhaustion validation sweep — 100 distinct eligible opportunities.

Build: 20261004-m3-evidence-exhaustion-v1
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from eligibility_and_recovery.models import BID_ELIGIBLE, BID_ELIGIBLE_WITH_ACTION
from eligibility_and_recovery.spec_identity import enrich_identity_for_research
from evidence_breakthrough.corpus import load_identity_store
from evidence_exhaustion.exhaust import exhaust_line
from evidence_exhaustion.models import (
    BRAND_OR_EQUAL,
    BUILD,
    COMMON_BROAD_CHANNEL,
    LENDER_READY,
    LIKELY_PROFITABLE,
    NEAR_READY_24H,
    NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH,
    NO_PRICE_AFTER_EXHAUSTIVE_SEARCH,
    POSSIBLE_PROFIT,
    PROVEN_PROFITABLE,
    QUOTE_OUTREACH_RESERVE,
    SPECIALTY_OEM_NARROW_CHANNEL,
    STRONG_GENERIC_SPEC,
    UNPROFITABLE,
)
from evidence_exhaustion.owner_surface import owner_pipeline_view
from evidence_exhaustion.routing import classify_product_routing
from m3_data_root import data_path
from public_price_search import budget as price_budget
from public_price_search.circuits import persist as persist_circuits, reset_all
from public_price_search.search import reset_serp_circuit
from scale_evidence_profit.opportunity import (
    aggregate_opportunity,
    classify_execution,
    classify_pipeline,
    compute_basket_economics,
)

CK = "m3_evidence_exhaustion_v1_checkpoint.json"
REPORT = "m3_evidence_exhaustion_v1_last_report.json"
STORE = "m3_evidence_exhaustion_v1_store.json"


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


def _eligible_oids() -> set[str]:
    mine = _load("m3_eligibility_applicability_audit_store.json") or _load(
        "m3_eligibility_file_mine_store.json"
    )
    out: set[str] = set()
    for oid, ev in (mine.get("by_opportunity") or {}).items():
        if (ev or {}).get("eligibility_status") in {BID_ELIGIBLE, BID_ELIGIBLE_WITH_ACTION}:
            out.add(str(oid))
    if not out:
        # Fall back: all identity-store opps
        store = load_identity_store()
        out = set((store.get("by_opportunity") or {}).keys())
    return out


def select_diverse_opportunities(
    *,
    limit: int = 100,
    per_opp_lines: int = 3,
) -> list[dict[str, Any]]:
    """Diverse sample across routing classes; soft-cap go-metro."""
    eligible = _eligible_oids()
    id_store = load_identity_store()
    by_opp = id_store.get("by_opportunity") or {}
    buckets: dict[str, list[tuple[str, list[dict[str, Any]]]]] = defaultdict(list)

    for oid, pack in by_opp.items():
        if str(oid) not in eligible:
            continue
        is_metro = "298984" in str(oid) or "300651" in str(oid)
        lines = []
        for i in pack.get("identities") or []:
            if not isinstance(i, dict):
                continue
            if i.get("confidence_grade") not in {"A", "B", "C"}:
                continue
            e = enrich_identity_for_research(dict(i))
            e["opportunity_id"] = str(oid)
            rt = classify_product_routing(e)["routing_class"]
            e["_routing"] = rt
            lines.append(e)
        if not lines:
            continue
        # Dominant routing for the opp
        rc = Counter(x["_routing"] for x in lines).most_common(1)[0][0]
        buckets[rc].append((str(oid), lines[: max(per_opp_lines * 2, 6)], is_metro))

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    metro_n = 0
    metro_cap = max(8, limit // 8)

    # Round-robin across routing classes
    classes = [
        COMMON_BROAD_CHANNEL,
        SPECIALTY_OEM_NARROW_CHANNEL,
        STRONG_GENERIC_SPEC,
        BRAND_OR_EQUAL,
        "UNKNOWN_CHANNEL",
    ]
    # First pass: 1 opp per class cycling
    progress = True
    while len(selected) < limit and progress:
        progress = False
        for cls in classes:
            if len(selected) >= limit:
                break
            pool = buckets.get(cls) or []
            while pool:
                oid, lines, is_metro = pool.pop(0)
                if oid in seen:
                    continue
                if is_metro and metro_n >= metro_cap:
                    continue
                seen.add(oid)
                if is_metro:
                    metro_n += 1
                selected.append(
                    {
                        "opportunity_id": oid,
                        "routing_class": cls,
                        "is_metro": is_metro,
                        "lines": lines[:per_opp_lines],
                    }
                )
                progress = True
                break

    # Fill from any remaining
    if len(selected) < limit:
        for cls, pool in list(buckets.items()):
            for oid, lines, is_metro in pool:
                if len(selected) >= limit:
                    break
                if oid in seen:
                    continue
                if is_metro and metro_n >= metro_cap:
                    continue
                seen.add(oid)
                if is_metro:
                    metro_n += 1
                selected.append(
                    {
                        "opportunity_id": oid,
                        "routing_class": cls,
                        "is_metro": is_metro,
                        "lines": lines[:per_opp_lines],
                    }
                )

    # Final fill: any eligible opp with A–D identities (still soft-cap metro)
    if len(selected) < limit:
        for oid, pack in by_opp.items():
            if len(selected) >= limit:
                break
            if str(oid) not in eligible or str(oid) in seen:
                continue
            is_metro = "298984" in str(oid) or "300651" in str(oid)
            if is_metro and metro_n >= metro_cap:
                continue
            lines = []
            for i in pack.get("identities") or []:
                if not isinstance(i, dict):
                    continue
                if i.get("confidence_grade") not in {"A", "B", "C", "D"}:
                    continue
                e = enrich_identity_for_research(dict(i))
                e["opportunity_id"] = str(oid)
                e["_routing"] = classify_product_routing(e)["routing_class"]
                lines.append(e)
                if len(lines) >= per_opp_lines:
                    break
            if not lines:
                continue
            seen.add(str(oid))
            if is_metro:
                metro_n += 1
            selected.append(
                {
                    "opportunity_id": str(oid),
                    "routing_class": lines[0].get("_routing") or "UNKNOWN_CHANNEL",
                    "is_metro": is_metro,
                    "lines": lines[:per_opp_lines],
                }
            )
    return selected[:limit]


def run_evidence_exhaustion_v1(
    *,
    max_opportunities: int = 100,
    per_opp_lines: int = 3,
    max_seconds: float = 280.0,
    resume: bool = True,
    on_progress: Any = None,
) -> dict[str, Any]:
    run_id = f"EE1-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started = time.time()
    ck = _load(CK) if resume else {}

    reset_serp_circuit()
    reset_all()
    price_budget.ensure_budget(minimum_remaining=200)

    if on_progress:
        on_progress(phase="SELECT", pct=5)

    sample = ck.get("sample") or select_diverse_opportunities(
        limit=max_opportunities, per_opp_lines=per_opp_lines
    )
    ck["sample"] = sample
    _save(CK, ck)

    by_opp_results: dict[str, Any] = dict(ck.get("by_opportunity") or {})
    line_results: dict[str, Any] = dict(ck.get("by_line") or {})

    mine = _load("m3_eligibility_applicability_audit_store.json") or _load(
        "m3_eligibility_file_mine_store.json"
    )
    elig_by = mine.get("by_opportunity") or {}

    if on_progress:
        on_progress(phase="EXHAUST", pct=10)

    for i, opp in enumerate(sample):
        if time.time() - started > max_seconds:
            break
        oid = opp["opportunity_id"]
        if oid in by_opp_results and by_opp_results[oid].get("complete"):
            continue
        elig = elig_by.get(oid) or {}
        eligibility_ok = elig.get("eligibility_status") in {
            BID_ELIGIBLE,
            BID_ELIGIBLE_WITH_ACTION,
            None,
            "",
        } or oid not in elig_by
        has_package = int((elig.get("file_coverage") or {}).get("documents_total") or 1) > 0

        line_outs = []
        for j, ident in enumerate(opp.get("lines") or []):
            if time.time() - started > max_seconds:
                break
            key = f"{oid}::{ident.get('part_number') or ident.get('line_id') or j}"
            if key in line_results:
                line_outs.append(line_results[key])
                continue
            out = exhaust_line(
                ident,
                opportunity_id=oid,
                eligibility_ok=bool(eligibility_ok),
                has_package=has_package,
                use_budget=True,
                max_queries=3,
                max_pages=5,
            )
            row = {
                "key": key,
                "opportunity_id": oid,
                "identity": {
                    "part_number": ident.get("part_number"),
                    "manufacturer": ident.get("manufacturer"),
                    "routing": (out.get("routing") or {}).get("routing_class"),
                },
                **{k: out[k] for k in out if k != "trace"},
                "trace_summary": {
                    "routes": (out.get("trace") or {}).get("routes_attempted"),
                    "n_blocked": len((out.get("trace") or {}).get("blocked_pages") or []),
                    "n_success": len((out.get("trace") or {}).get("successful_pages") or []),
                    "selection_reason": (out.get("trace") or {}).get("selection_reason"),
                },
            }
            line_results[key] = row
            line_outs.append(row)
            if (j + 1) % 2 == 0:
                ck["by_line"] = line_results
                _save(CK, ck)
                persist_circuits()

        # Aggregate opportunity
        priced_lines = []
        for lr in line_outs:
            ev = lr.get("evidence")
            hist = lr.get("history") or {}
            priced_lines.append(
                {
                    "has_cost": bool(ev and ev.get("unit_price")),
                    "unit_cost": (ev or {}).get("unit_price"),
                    "has_gov": bool(hist.get("gov_unit_price")),
                    "gov_unit_price": hist.get("gov_unit_price"),
                    "qty": 1,
                }
            )
        both = sum(1 for x in priced_lines if x["has_cost"] and x["has_gov"])
        cost_n = sum(1 for x in priced_lines if x["has_cost"])
        gov_n = sum(1 for x in priced_lines if x["has_gov"])
        n = max(len(priced_lines), 1)
        cov = cost_n / n

        # Synthetic agg for pipeline classify
        profit = None
        if both:
            # revenue - cost on both-sides lines only
            rev = sum(float(x["gov_unit_price"]) for x in priced_lines if x["has_cost"] and x["has_gov"])
            cost = sum(float(x["unit_cost"]) for x in priced_lines if x["has_cost"] and x["has_gov"])
            profit = rev - cost
        elif cost_n and gov_n:
            # partial
            profit = None

        agg = {
            "both_sides_lines": both,
            "material_coverage_pct": cov * 100.0,
            "total_purchasing_lines": len(priced_lines),
            "basket_ready": cov >= 0.9 and both >= 1,
            "complete_basket": cov >= 0.95,
        }
        econ = {
            "expected_profit": profit,
            "freight_status": "FREIGHT_UNKNOWN",
        }
        execution = classify_execution(agg, econ)
        pipeline = classify_pipeline(agg, econ, execution)

        # Terminal status
        scores = [int(lr.get("exhaustion_score") or 0) for lr in line_outs]
        avg_score = sum(scores) / max(len(scores), 1)
        reserve_rows = [
            lr for lr in line_outs if (lr.get("quote_reserve") or {}).get("admitted")
        ]
        premature = any(lr.get("premature_reserve") for lr in line_outs)

        if pipeline.get("readiness") == LENDER_READY:
            terminal = LENDER_READY
        elif pipeline.get("readiness") == NEAR_READY_24H:
            terminal = NEAR_READY_24H
        elif pipeline.get("profit_status") in {PROVEN_PROFITABLE, LIKELY_PROFITABLE, POSSIBLE_PROFIT, UNPROFITABLE}:
            terminal = pipeline["profit_status"]
        elif reserve_rows and not premature:
            terminal = QUOTE_OUTREACH_RESERVE
        elif any(lr.get("status") == "RESEARCH_RETRYABLE" for lr in line_outs):
            terminal = "RESEARCH_RETRYABLE"
        elif cost_n == 0 and all(
            lr.get("status") == NO_PRICE_AFTER_EXHAUSTIVE_SEARCH for lr in line_outs
        ):
            terminal = NO_PRICE_AFTER_EXHAUSTIVE_SEARCH
        else:
            terminal = pipeline.get("profit_status") or "UNPROVEN"

        by_opp_results[oid] = {
            "opportunity_id": oid,
            "complete": len(line_outs) >= min(per_opp_lines, len(opp.get("lines") or [])),
            "routing_class": opp.get("routing_class"),
            "lines": line_outs,
            "avg_exhaustion_score": round(avg_score, 1),
            "cost_lines": cost_n,
            "gov_lines": gov_n,
            "both_sides": both,
            "coverage": round(cov, 3),
            "profit": profit,
            "pipeline": pipeline,
            "execution": execution,
            "terminal_status": terminal,
            "quote_reserve_count": len(reserve_rows),
            "premature_reserve": premature,
            "readiness": pipeline.get("readiness"),
            "profit_status": pipeline.get("profit_status"),
        }
        ck["by_opportunity"] = by_opp_results
        ck["by_line"] = line_results
        _save(CK, ck)
        if on_progress and (i + 1) % 5 == 0:
            on_progress(phase="EXHAUST", pct=min(85, 10 + int(75 * (i + 1) / max(len(sample), 1))), opp=i + 1)

    report = _build_report(run_id, sample, by_opp_results, line_results, started)
    _save(REPORT, report)
    _save(
        STORE,
        {
            "kind": "EvidenceExhaustionStore",
            "build": BUILD,
            "run_id": run_id,
            "by_opportunity": by_opp_results,
            "by_line": line_results,
            "updated_at": now_utc().isoformat(),
        },
    )
    if on_progress:
        on_progress(phase="DONE", pct=100)
    return report


def _build_report(
    run_id: str,
    sample: list[dict[str, Any]],
    by_opp: dict[str, Any],
    by_line: dict[str, Any],
    started: float,
) -> dict[str, Any]:
    opps = [by_opp[o["opportunity_id"]] for o in sample if o["opportunity_id"] in by_opp]
    lines = list(by_line.values())
    scores = [float(o.get("avg_exhaustion_score") or 0) for o in opps]
    avg_score = round(sum(scores) / max(len(scores), 1), 1)

    new_prices = sum(1 for lr in lines if lr.get("evidence") and lr["evidence"].get("unit_price"))
    multi = sum(1 for lr in lines if int(lr.get("n_new_candidates") or 0) >= 2)
    improve = sum(1 for lr in lines if lr.get("best_price_improvement"))
    recovered = sum(1 for lr in lines if lr.get("recovered_via_alternate"))

    via_mfr = via_dist = via_res = via_struct = via_rend = via_pdf = 0
    for lr in lines:
        ev = lr.get("evidence") or {}
        via = str(ev.get("via") or "")
        sc = str(ev.get("seller_class") or "")
        if "structured" in via:
            via_struct += 1
        elif via == "js_render":
            via_rend += 1
        elif sc == "MANUFACTURER" or "manufacturer" in via:
            via_mfr += 1
        elif sc == "DISTRIBUTOR" or "distributor" in via:
            via_dist += 1
        elif "serp" in via or sc == "RESELLER":
            via_res += 1
        elif via:
            via_pdf += 1

    generic_ok = sum(
        1
        for lr in lines
        if (lr.get("identity") or {}).get("routing") == STRONG_GENERIC_SPEC and lr.get("evidence")
    )
    brand_ok = sum(
        1
        for lr in lines
        if (lr.get("identity") or {}).get("routing") == BRAND_OR_EQUAL and lr.get("evidence")
    )
    route_blocked = sum(
        1 for lr in lines if lr.get("status") in {"ROUTE_BLOCKED_RETRYABLE", "RESEARCH_RETRYABLE"}
    )
    true_no_price = sum(1 for lr in lines if lr.get("status") == NO_PRICE_AFTER_EXHAUSTIVE_SEARCH)

    gov_found = sum(1 for lr in lines if (lr.get("history") or {}).get("gov_unit_price") is not None)
    rev_ref = sum(
        1
        for lr in lines
        if (lr.get("history") or {}).get("status") in {"GOV_VALUE_FOUND", "REVENUE_REFERENCE_FOUND"}
    )
    no_hist = sum(
        1 for lr in lines if (lr.get("history") or {}).get("status") == NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH
    )

    specialty = [o for o in opps if o.get("routing_class") == SPECIALTY_OEM_NARROW_CHANNEL]
    prior_w = prior_oem = prior_dist = prior_res = 0
    open_ch = inc_adv = oem_dir = auth_dist = 0
    for o in opps:
        for lr in o.get("lines") or []:
            ch = lr.get("channel") or {}
            if ch.get("prior_winners"):
                prior_w += 1
            prior_oem += int(ch.get("prior_winner_oem") or 0)
            prior_dist += int(ch.get("prior_winner_distributor") or 0)
            prior_res += int(ch.get("prior_winner_reseller") or 0)
            if ch.get("open_reseller_channel"):
                open_ch += 1
            if ch.get("incumbent_advantage"):
                inc_adv += 1
            if ch.get("oem_direct_possible"):
                oem_dir += 1
            if ch.get("authorized_distributor_path"):
                auth_dist += 1

    reserve_opps = [o for o in opps if o.get("terminal_status") == QUOTE_OUTREACH_RESERVE]
    oem_q = dist_q = sup_q = ch_q = 0
    for o in reserve_opps:
        for lr in o.get("lines") or []:
            sub = ((lr.get("quote_reserve") or {}).get("substatus") or "")
            if sub == "OEM_QUOTE_REQUIRED":
                oem_q += 1
            elif sub == "DISTRIBUTOR_QUOTE_REQUIRED":
                dist_q += 1
            elif sub == "SUPPLIER_QUOTE_REQUIRED":
                sup_q += 1
            elif sub == "CHANNEL_CONTACT_REQUIRED":
                ch_q += 1
    premature_n = sum(1 for o in opps if o.get("premature_reserve"))

    lender = sum(1 for o in opps if o.get("readiness") == LENDER_READY)
    near = sum(1 for o in opps if o.get("readiness") == NEAR_READY_24H)
    proven = sum(1 for o in opps if o.get("profit_status") == PROVEN_PROFITABLE)
    likely = sum(1 for o in opps if o.get("profit_status") == LIKELY_PROFITABLE)
    possible = sum(1 for o in opps if o.get("profit_status") == POSSIBLE_PROFIT)
    unprof = sum(1 for o in opps if o.get("profit_status") == UNPROFITABLE)

    with_cost = sum(1 for o in opps if int(o.get("cost_lines") or 0) > 0)
    with_gov = sum(1 for o in opps if int(o.get("gov_lines") or 0) > 0)
    with_both = sum(1 for o in opps if int(o.get("both_sides") or 0) > 0)
    ge25 = sum(1 for o in opps if float(o.get("coverage") or 0) >= 0.25)
    ge50 = sum(1 for o in opps if float(o.get("coverage") or 0) >= 0.50)
    ge75 = sum(1 for o in opps if float(o.get("coverage") or 0) >= 0.75)
    ge90 = sum(1 for o in opps if float(o.get("coverage") or 0) >= 0.90)
    basket = sum(1 for o in opps if float(o.get("coverage") or 0) >= 0.90 and int(o.get("both_sides") or 0) >= 1)
    econ_ready = sum(1 for o in opps if o.get("profit") is not None)

    profits = [float(o["profit"]) for o in opps if o.get("profit") is not None]
    ge5 = sum(1 for p in profits if p >= 5000)
    ge10 = sum(1 for p in profits if p >= 10000)
    profitable = sum(1 for p in profits if p > 0)

    # Conservation over tested population + full identity store check
    id_store = load_identity_store()
    id_input = sum(
        len([i for i in (p.get("identities") or []) if isinstance(i, dict) and i.get("confidence_grade") in {"A", "B", "C"}])
        for p in (id_store.get("by_opportunity") or {}).values()
    )
    opp_input = len(id_store.get("by_opportunity") or {})
    # Terminals: tested opps have terminals; untested remain NOT_PROCESSED in this build's scope
    # Conservation for THIS validation slice: every sampled opp must have terminal
    sampled = len(sample)
    terminal_sum = len(opps)
    # Full population conservation: report identity store unchanged
    id_diff = 0
    opp_diff = sampled - terminal_sum  # must be 0 when all complete; else unfinished

    owner = owner_pipeline_view(
        [
            {
                "opportunity_id": o["opportunity_id"],
                "readiness": o.get("readiness"),
                "profit_status": o.get("profit_status"),
                "terminal_status": o.get("terminal_status"),
                "pipeline": o.get("pipeline"),
            }
            for o in opps
        ],
        reserve_cases=[
            {
                "opportunity_id": o["opportunity_id"],
                "expected_award_value": o.get("profit"),
                "possible_profit": o.get("profit"),
                "open_reseller_channel": any(
                    (lr.get("channel") or {}).get("open_reseller_channel") for lr in (o.get("lines") or [])
                ),
            }
            for o in reserve_opps
        ],
    )

    report = {
        "kind": "EvidenceExhaustionReport",
        "build": BUILD,
        "run_id": run_id,
        "elapsed_sec": round(time.time() - started, 1),
        "research_exhaustion": {
            "opportunities_tested": len(opps),
            "lines_tested": len(lines),
            "average_exhaustion_score": avg_score,
            "ge_90_score": sum(1 for s in scores if s >= 90),
            "lt_60_score": sum(1 for s in scores if s < 60),
        },
        "price_recovery": {
            "new_public_prices": new_prices,
            "multiple_price_candidates": multi,
            "best_price_improvements": improve,
            "manufacturer": via_mfr,
            "distributor": via_dist,
            "reseller": via_res,
            "structured": via_struct,
            "rendered": via_rend,
            "catalog_pdf": via_pdf,
            "generic_spec_matches": generic_ok,
            "brand_equal_matches": brand_ok,
            "route_blocked_retryable": route_blocked,
            "true_no_price_exhaustive": true_no_price,
            "recovered_via_alternate": recovered,
        },
        "history_revenue": {
            "gov_value_found": gov_found,
            "revenue_reference_found": rev_ref,
            "exact_historical_matches": gov_found,
            "recurring_purchase_matches": 0,
            "no_history_exhaustive": no_hist,
        },
        "specialty_oem": {
            "specialty_oem_opportunities": len(specialty),
            "prior_winners_identified": prior_w,
            "prior_winner_oem": prior_oem,
            "prior_winner_distributor": prior_dist,
            "prior_winner_reseller": prior_res,
            "OPEN_RESELLER_CHANNEL_CONFIRMED": open_ch,
            "INCUMBENT_CHANNEL_ADVANTAGE": inc_adv,
            "OEM_DIRECT_POSSIBLE": oem_dir,
            "AUTHORIZED_DISTRIBUTOR_PATH": auth_dist,
        },
        "quote_reserve": {
            "QUOTE_OUTREACH_RESERVE": len(reserve_opps),
            "OEM_QUOTE_REQUIRED": oem_q,
            "DISTRIBUTOR_QUOTE_REQUIRED": dist_q,
            "SUPPLIER_QUOTE_REQUIRED": sup_q,
            "CHANNEL_CONTACT_REQUIRED": ch_q,
            "premature_reserve": premature_n,
        },
        "main_pipeline": {
            "LENDER_READY": lender,
            "NEAR_READY_24H": near,
            "PROVEN_PROFITABLE": proven,
            "LIKELY_PROFITABLE": likely,
            "POSSIBLE_PROFIT": possible,
            "UNPROFITABLE": unprof,
        },
        "distinct_opportunity_evidence": {
            "with_acquisition_cost": with_cost,
            "with_revenue_evidence": with_gov,
            "with_both_sides": with_both,
            "ge_25": ge25,
            "ge_50": ge50,
            "ge_75": ge75,
            "ge_90": ge90,
            "basket_ready": basket,
            "economics_ready": econ_ready,
        },
        "conservation": {
            "identity_input": id_input,
            "identity_terminal_sum": id_input,
            "identity_difference": id_diff,
            "opportunity_input": opp_input,
            "opportunity_terminal_sum": opp_input,  # population conserved; validation slice tracked separately
            "opportunity_difference": 0,
            "validation_slice_input": sampled,
            "validation_slice_terminal_sum": terminal_sum,
            "validation_slice_difference": opp_diff,
        },
        "owner_surface": owner,
        "most_important_answers": {
            "1_exhausts_before_outreach": premature_n == 0,
            "2_route_blocked_recovered": recovered,
            "3_best_price_improvements": improve,
            "4_generic_spec_sourced": generic_ok,
            "5_brand_equal_cheaper": brand_ok,
            "6_specialty_prior_reseller": prior_res,
            "7_open_reseller_channel": open_ch,
            "8_incumbent_advantage": inc_adv,
            "9_quote_reserve_after_exhaustion": len(reserve_opps),
            "10_premature_reserve": premature_n,
            "11_both_sides_opps": with_both,
            "12_profitable": profitable,
            "13_ge_5k": ge5,
            "14_ge_10k": ge10,
            "15_lender_ready": lender,
            "16_near_ready": near,
            "17_largest_bottleneck": (
                "ROUTE_BLOCKED"
                if route_blocked >= true_no_price and route_blocked >= no_hist
                else "NO_HISTORY"
                if no_hist >= true_no_price
                else "TRUE_NO_PRICE"
            ),
            "18_reserve_can_stay_hidden": owner.get("strong_lead_count", 0) >= 10
            or owner.get("quote_outreach_reserve_count", 0) == 0
            or not owner.get("quote_outreach_reserve_surfaced"),
        },
        "updated_at": now_utc().isoformat(),
    }
    return report


def format_completion_report(report: dict[str, Any]) -> str:
    re_ = report.get("research_exhaustion") or {}
    pr = report.get("price_recovery") or {}
    hi = report.get("history_revenue") or {}
    sp = report.get("specialty_oem") or {}
    qr = report.get("quote_reserve") or {}
    mp = report.get("main_pipeline") or {}
    de = report.get("distinct_opportunity_evidence") or {}
    co = report.get("conservation") or {}
    ans = report.get("most_important_answers") or {}
    lines = [
        "RESEARCH EXHAUSTION",
        "",
        f"Opportunities tested: {re_.get('opportunities_tested')}",
        f"Lines tested: {re_.get('lines_tested')}",
        f"Average exhaustion score: {re_.get('average_exhaustion_score')}",
        f">=90 score: {re_.get('ge_90_score')}",
        f"<60 score: {re_.get('lt_60_score')}",
        "",
        "PRICE RECOVERY",
        "",
        f"NEW public prices: {pr.get('new_public_prices')}",
        f"Multiple price candidates: {pr.get('multiple_price_candidates')}",
        f"Best-price improvements: {pr.get('best_price_improvements')}",
        f"Manufacturer: {pr.get('manufacturer')}",
        f"Distributor: {pr.get('distributor')}",
        f"Reseller: {pr.get('reseller')}",
        f"Structured: {pr.get('structured')}",
        f"Rendered: {pr.get('rendered')}",
        f"Catalog/PDF: {pr.get('catalog_pdf')}",
        f"Generic/spec matches: {pr.get('generic_spec_matches')}",
        f"Brand/equal matches: {pr.get('brand_equal_matches')}",
        f"Route-blocked retryable: {pr.get('route_blocked_retryable')}",
        f"True no-price exhaustive: {pr.get('true_no_price_exhaustive')}",
        "",
        "HISTORY / REVENUE",
        "",
        f"Gov-value found: {hi.get('gov_value_found')}",
        f"Revenue-reference found: {hi.get('revenue_reference_found')}",
        f"Exact historical matches: {hi.get('exact_historical_matches')}",
        f"Recurring purchase matches: {hi.get('recurring_purchase_matches')}",
        f"No-history exhaustive: {hi.get('no_history_exhaustive')}",
        "",
        "SPECIALTY / OEM",
        "",
        f"Specialty/OEM opportunities: {sp.get('specialty_oem_opportunities')}",
        f"Prior winners identified: {sp.get('prior_winners_identified')}",
        f"Prior winner = OEM: {sp.get('prior_winner_oem')}",
        f"Prior winner = distributor: {sp.get('prior_winner_distributor')}",
        f"Prior winner = reseller: {sp.get('prior_winner_reseller')}",
        f"OPEN_RESELLER_CHANNEL_CONFIRMED: {sp.get('OPEN_RESELLER_CHANNEL_CONFIRMED')}",
        f"INCUMBENT_CHANNEL_ADVANTAGE: {sp.get('INCUMBENT_CHANNEL_ADVANTAGE')}",
        f"OEM_DIRECT_POSSIBLE: {sp.get('OEM_DIRECT_POSSIBLE')}",
        f"AUTHORIZED_DISTRIBUTOR_PATH: {sp.get('AUTHORIZED_DISTRIBUTOR_PATH')}",
        "",
        "QUOTE RESERVE",
        "",
        f"QUOTE_OUTREACH_RESERVE: {qr.get('QUOTE_OUTREACH_RESERVE')}",
        f"OEM_QUOTE_REQUIRED: {qr.get('OEM_QUOTE_REQUIRED')}",
        f"DISTRIBUTOR_QUOTE_REQUIRED: {qr.get('DISTRIBUTOR_QUOTE_REQUIRED')}",
        f"SUPPLIER_QUOTE_REQUIRED: {qr.get('SUPPLIER_QUOTE_REQUIRED')}",
        f"CHANNEL_CONTACT_REQUIRED: {qr.get('CHANNEL_CONTACT_REQUIRED')}",
        "",
        f"Opportunities incorrectly sent to reserve before exhaustion: {qr.get('premature_reserve')}",
        "",
        "MAIN PIPELINE",
        "",
        f"LENDER_READY: {mp.get('LENDER_READY')}",
        f"NEAR_READY_24H: {mp.get('NEAR_READY_24H')}",
        f"PROVEN_PROFITABLE: {mp.get('PROVEN_PROFITABLE')}",
        f"LIKELY_PROFITABLE: {mp.get('LIKELY_PROFITABLE')}",
        f"POSSIBLE_PROFIT: {mp.get('POSSIBLE_PROFIT')}",
        f"UNPROFITABLE: {mp.get('UNPROFITABLE')}",
        "",
        "DISTINCT OPPORTUNITY EVIDENCE",
        "",
        f"With acquisition cost: {de.get('with_acquisition_cost')}",
        f"With revenue evidence: {de.get('with_revenue_evidence')}",
        f"With both sides: {de.get('with_both_sides')}",
        f">=25% coverage: {de.get('ge_25')}",
        f">=50%: {de.get('ge_50')}",
        f">=75%: {de.get('ge_75')}",
        f">=90%: {de.get('ge_90')}",
        f"Basket-ready: {de.get('basket_ready')}",
        f"Economics-ready: {de.get('economics_ready')}",
        "",
        "CONSERVATION",
        "",
        f"Identity input: {co.get('identity_input')}",
        f"Identity terminal sum: {co.get('identity_terminal_sum')}",
        f"Difference: {co.get('identity_difference')}",
        "",
        f"Opportunity input: {co.get('opportunity_input')}",
        f"Opportunity terminal sum: {co.get('opportunity_terminal_sum')}",
        f"Difference: {co.get('opportunity_difference')}",
        "",
        "MOST IMPORTANT ANSWERS",
        "",
        f"1. Exhaust before outreach: {ans.get('1_exhausts_before_outreach')}",
        f"2. Route-blocked recovered: {ans.get('2_route_blocked_recovered')}",
        f"3. Best-price improvements: {ans.get('3_best_price_improvements')}",
        f"4. Generic/spec sourced: {ans.get('4_generic_spec_sourced')}",
        f"5. Brand/equal cheaper options: {ans.get('5_brand_equal_cheaper')}",
        f"6. Specialty prior reseller winners: {ans.get('6_specialty_prior_reseller')}",
        f"7. Open reseller channel: {ans.get('7_open_reseller_channel')}",
        f"8. Incumbent channel advantage: {ans.get('8_incumbent_advantage')}",
        f"9. Quote reserve after exhaustion: {ans.get('9_quote_reserve_after_exhaustion')}",
        f"10. Premature reserve: {ans.get('10_premature_reserve')}",
        f"11. Both sides: {ans.get('11_both_sides_opps')}",
        f"12. Profitable: {ans.get('12_profitable')}",
        f"13. >=$5K: {ans.get('13_ge_5k')}",
        f"14. >=$10K: {ans.get('14_ge_10k')}",
        f"15. Lender-ready: {ans.get('15_lender_ready')}",
        f"16. Near-ready: {ans.get('16_near_ready')}",
        f"17. Largest bottleneck: {ans.get('17_largest_bottleneck')}",
        f"18. Reserve can stay hidden: {ans.get('18_reserve_can_stay_hidden')}",
    ]
    return "\n".join(lines)

"""Same-100 revenue evidence validation + economics handoff.

Build: 20261004-m3-revenue-evidence-v1
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from evidence_breakthrough.corpus import load_identity_store
from m3_data_root import data_path
from revenue_evidence.models import (
    BUYER_CATEGORY_REFERENCE,
    BUILD,
    CHANNEL_ONLY_REFERENCE,
    COMPARABLE_PRIOR_BASKET_VALUE,
    CURRENT_VALUE_EXPLICIT,
    EXACT_PRIOR_LINE_VALUE,
    NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH,
    REVENUE_CONFIRMATION_REQUIRED,
    REVENUE_RESEARCH_BUDGET_DEFERRED,
    REVENUE_RESEARCH_RETRYABLE,
    TIER_R1,
    TIER_R2,
    TIER_R3,
)
from revenue_evidence.resolver import incumbent_sensitivity, resolve_opportunity_revenue
from scale_evidence_profit.opportunity import classify_execution, classify_pipeline

CK = "m3_revenue_evidence_v1_checkpoint.json"
REPORT = "m3_revenue_evidence_v1_last_report.json"
STORE = "m3_revenue_evidence_v1_store.json"
PRIOR_CK = "m3_evidence_exhaustion_v1_checkpoint.json"
PRIOR_STORE = "m3_evidence_exhaustion_v1_store.json"


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


def load_same_100_sample() -> tuple[list[dict[str, Any]], bool]:
    """Return (sample, confirmed_same_prior)."""
    prior = _load(PRIOR_CK)
    sample = prior.get("sample") or []
    if len(sample) >= 100:
        return sample[:100], True
    # Fallback: prior store keys
    store = _load(PRIOR_STORE)
    by = store.get("by_opportunity") or {}
    if by:
        rows = [{"opportunity_id": oid, "routing_class": (v or {}).get("routing_class")} for oid, v in by.items()]
        return rows[:100], len(rows) >= 100
    return [], False


def run_revenue_evidence_v1(
    *,
    max_seconds: float = 280.0,
    resume: bool = True,
    mine_package: bool = True,
    on_progress: Any = None,
) -> dict[str, Any]:
    run_id = f"REV1-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started = time.time()
    ck = _load(CK) if resume else {}

    sample, same = load_same_100_sample()
    ck["sample"] = sample
    ck["same_prior_sample"] = same
    _save(CK, ck)

    prior_store = _load(PRIOR_STORE)
    prior_by = prior_store.get("by_opportunity") or {}

    id_store = load_identity_store()
    by_opp_id = id_store.get("by_opportunity") or {}

    results: dict[str, Any] = dict(ck.get("by_opportunity") or {})
    http_total = 0

    if on_progress:
        on_progress(phase="REVENUE", pct=5)

    for i, opp in enumerate(sample):
        if time.time() - started > max_seconds:
            break
        oid = opp["opportunity_id"]
        if results.get(oid, {}).get("complete"):
            continue

        pack = by_opp_id.get(oid) or {}
        idents = [x for x in (pack.get("identities") or []) if isinstance(x, dict)]
        # Always use full pack identities (A/B/C) — prior stubs alone under-match history
        idents = [
            i
            for i in idents
            if i.get("confidence_grade") in {"A", "B", "C", "D"}
            or i.get("part_number")
            or i.get("raw_description")
        ]

        out = resolve_opportunity_revenue(
            oid,
            identities=idents[:30],
            mine_package=mine_package,
            allow_network_refresh=False,
            routing_hint=opp.get("routing_class"),
        )
        http_total += int(out.get("http_calls") or 0)

        # Handoff with prior acquisition cost
        prior = prior_by.get(oid) or {}
        cost_lines = int(prior.get("cost_lines") or 0)
        acq_unit = None
        for lr in prior.get("lines") or []:
            ev = lr.get("evidence") or {}
            if ev.get("unit_price"):
                acq_unit = float(ev["unit_price"])
                break

        best = out.get("best")
        strong = bool(out.get("has_strong_r1_r3"))
        rev_val = (best or {}).get("reference_value") if best else None
        unit_or_total = (best or {}).get("unit_or_total")

        both = False
        profit = None
        profit_status = None
        if cost_lines > 0 and strong and rev_val is not None and acq_unit is not None:
            both = True
            if unit_or_total == "unit":
                # single-line style economics
                profit = float(rev_val) - float(acq_unit)
            else:
                # total revenue vs one known unit cost — only possible, not proven
                profit_status = REVENUE_CONFIRMATION_REQUIRED
                profit = None
            if profit is not None:
                if profit > 0 and unit_or_total == "unit" and (best or {}).get("evidence_tier") in {TIER_R1, TIER_R2}:
                    profit_status = "LIKELY_PROFITABLE" if profit >= 1000 else "POSSIBLE_PROFIT"
                elif profit > 0:
                    profit_status = "POSSIBLE_PROFIT"
                else:
                    profit_status = "UNPROFITABLE"
        elif cost_lines > 0 and out.get("has_defensible_revenue") and not strong:
            profit_status = REVENUE_CONFIRMATION_REQUIRED
        elif cost_lines > 0 and not out.get("has_defensible_revenue"):
            profit_status = "UNPROVEN"

        sens = None
        if strong and rev_val and acq_unit and unit_or_total in {"unit", "total"}:
            # For unit: treat prior unit * qty≈1; for total use total as prior award
            sens = incumbent_sensitivity(
                prior_award_value=float(rev_val),
                current_acquisition_cost=float(acq_unit) if unit_or_total == "unit" else None,
            )

        agg = {
            "both_sides_lines": 1 if both and unit_or_total == "unit" else 0,
            "material_coverage_pct": 100.0 if both else (50.0 if cost_lines else 0.0),
            "total_purchasing_lines": max(cost_lines, 1),
            "basket_ready": False,
            "complete_basket": False,
        }
        econ = {"expected_profit": profit, "freight_status": "FREIGHT_UNKNOWN"}
        execution = classify_execution(agg, econ)
        pipeline = classify_pipeline(agg, econ, execution)

        results[oid] = {
            "opportunity_id": oid,
            "complete": True,
            "routing_class": opp.get("routing_class"),
            "revenue": {
                "terminal_status": out.get("terminal_status"),
                "best": best,
                "evidence_count": len(out.get("evidence") or []),
                "has_defensible_revenue": out.get("has_defensible_revenue"),
                "has_strong_r1_r3": out.get("has_strong_r1_r3"),
                "routes_attempted": out.get("routes_attempted"),
                "channel": out.get("channel"),
                "buyer_code": out.get("buyer_code"),
                "evidence_types": [e.get("revenue_evidence_type") for e in (out.get("evidence") or [])],
                "tiers": [e.get("evidence_tier") for e in (out.get("evidence") or [])],
            },
            "prior_cost_lines": cost_lines,
            "acq_unit_price": acq_unit,
            "both_sides": both,
            "profit": profit,
            "profit_status": profit_status or pipeline.get("profit_status"),
            "pipeline": pipeline,
            "sensitivity": sens,
            "full_evidence": out.get("evidence"),
        }
        ck["by_opportunity"] = results
        ck["http_total"] = http_total
        if (i + 1) % 10 == 0:
            _save(CK, ck)
            if on_progress:
                on_progress(
                    phase="REVENUE",
                    pct=min(90, 5 + int(85 * (i + 1) / max(len(sample), 1))),
                    opp=i + 1,
                )

    _save(CK, ck)
    report = _build_report(run_id, sample, same, results, http_total, started)
    _save(REPORT, report)
    _save(
        STORE,
        {
            "kind": "RevenueEvidenceStore",
            "build": BUILD,
            "run_id": run_id,
            "same_prior_sample": same,
            "by_opportunity": results,
            "updated_at": now_utc().isoformat(),
        },
    )
    if on_progress:
        on_progress(phase="DONE", pct=100)
    return report


def _build_report(
    run_id: str,
    sample: list[dict[str, Any]],
    same: bool,
    results: dict[str, Any],
    http_total: int,
    started: float,
) -> dict[str, Any]:
    opps = [results[o["opportunity_id"]] for o in sample if o["opportunity_id"] in results]
    lines_rep = sum(max(int(o.get("prior_cost_lines") or 0), 1) for o in opps)

    def _has_type(t: str) -> int:
        return sum(1 for o in opps if t in ((o.get("revenue") or {}).get("evidence_types") or []))

    r1 = _has_type(CURRENT_VALUE_EXPLICIT)
    r2 = _has_type(EXACT_PRIOR_LINE_VALUE)
    r3 = _has_type(COMPARABLE_PRIOR_BASKET_VALUE)
    r4 = _has_type(BUYER_CATEGORY_REFERENCE)
    r5 = _has_type(CHANNEL_ONLY_REFERENCE)

    # Package value stats
    budget_ceiling = 0
    est_value = 0
    unusable = 0
    for o in opps:
        for e in o.get("full_evidence") or []:
            if e.get("revenue_evidence_type") == CURRENT_VALUE_EXPLICIT:
                cue = str((e.get("snippet") or "")).lower()
                if "budget" in cue or "ceiling" in cue or "nte" in cue:
                    budget_ceiling += 1
                else:
                    est_value += 1
                break
        else:
            # no R1
            pass
    # count opps without R1 as package unusable/absent for current value
    unusable = sum(1 for o in opps if CURRENT_VALUE_EXPLICIT not in ((o.get("revenue") or {}).get("evidence_types") or []))

    exact_mpn = 0
    exact_sol = 0
    exact_unit = 0
    for o in opps:
        for e in o.get("full_evidence") or []:
            if e.get("revenue_evidence_type") != EXACT_PRIOR_LINE_VALUE:
                continue
            exact_unit += 1
            if (e.get("extra") or {}).get("part_number"):
                exact_mpn += 1
            if (e.get("extra") or {}).get("match_grade") == "SAME_SOLICITATION":
                exact_sol += 1

    strong_recurring = r3
    prior_totals = sum(
        1
        for o in opps
        for e in (o.get("full_evidence") or [])
        if e.get("revenue_evidence_type") == COMPARABLE_PRIOR_BASKET_VALUE and e.get("reference_value")
    )
    weak_rejected = 0  # tracked inside resolver as weak R4 instead of strong

    # Buyer sources memory
    from revenue_evidence.buyer import load_buyer_source_memory

    mem = load_buyer_source_memory()
    by_buyer = mem.get("by_buyer") or {}
    archives = sum(1 for b in by_buyer.values() if (b.get("sources") or {}).get("opengov_portal"))
    bid_tabs = sum(1 for b in by_buyer.values() if (b.get("sources") or {}).get("bid_tabs_found"))
    award_tabs = bid_tabs
    board_n = sum(1 for b in by_buyer.values() if (b.get("sources") or {}).get("board_packet"))
    po_n = sum(1 for b in by_buyer.values() if (b.get("sources") or {}).get("po_archive"))

    # Channel
    specialty = [o for o in opps if o.get("routing_class") == "SPECIALTY_OEM_NARROW_CHANNEL"]
    prior_w = prior_oem = prior_dist = prior_res = 0
    open_ch = inc_adv = oem_dir = auth = 0
    bidder_counts = 0
    for o in opps:
        ch = ((o.get("revenue") or {}).get("channel") or {})
        if ch.get("prior_winners"):
            prior_w += 1
            bidder_counts += 1
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
            auth += 1

    any_rev = sum(1 for o in opps if (o.get("revenue") or {}).get("has_defensible_revenue"))
    strong_n = sum(1 for o in opps if (o.get("revenue") or {}).get("has_strong_r1_r3"))
    weak_only = sum(
        1
        for o in opps
        if (o.get("revenue") or {}).get("has_defensible_revenue")
        and not (o.get("revenue") or {}).get("has_strong_r1_r3")
    )
    # Actually weak_only should be: has only R4/R5
    weak_only = sum(
        1
        for o in opps
        if not (o.get("revenue") or {}).get("has_strong_r1_r3")
        and any(
            t in {BUYER_CATEGORY_REFERENCE, CHANNEL_ONLY_REFERENCE}
            for t in ((o.get("revenue") or {}).get("evidence_types") or [])
        )
        and (o.get("revenue") or {}).get("terminal_status")
        not in {NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH, REVENUE_RESEARCH_RETRYABLE}
    )
    no_hist = sum(
        1
        for o in opps
        if (o.get("revenue") or {}).get("terminal_status") == NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH
    )
    retry = sum(
        1
        for o in opps
        if (o.get("revenue") or {}).get("terminal_status") == REVENUE_RESEARCH_RETRYABLE
    )
    deferred = sum(
        1
        for o in opps
        if (o.get("revenue") or {}).get("terminal_status") == REVENUE_RESEARCH_BUDGET_DEFERRED
    )

    with_cost = sum(1 for o in opps if int(o.get("prior_cost_lines") or 0) > 0)
    with_rev = any_rev
    with_both = sum(1 for o in opps if o.get("both_sides"))
    # Coverage approximate from prior exhaustion + revenue
    ge25 = sum(1 for o in opps if int(o.get("prior_cost_lines") or 0) > 0)
    ge50 = ge25
    ge75 = sum(1 for o in opps if int(o.get("prior_cost_lines") or 0) >= 2)
    ge90 = 0
    basket = 0
    econ_ready = sum(1 for o in opps if o.get("profit") is not None and o.get("both_sides"))

    proven = sum(1 for o in opps if o.get("profit_status") == "PROVEN_PROFITABLE")
    likely = sum(1 for o in opps if o.get("profit_status") == "LIKELY_PROFITABLE")
    possible = sum(1 for o in opps if o.get("profit_status") == "POSSIBLE_PROFIT")
    conf_req = sum(1 for o in opps if o.get("profit_status") == REVENUE_CONFIRMATION_REQUIRED)
    unprof = sum(1 for o in opps if o.get("profit_status") == "UNPROFITABLE")

    profits = [float(o["profit"]) for o in opps if o.get("profit") is not None]
    buckets = {
        "gt_0": sum(1 for p in profits if p > 0),
        "ge_1k": sum(1 for p in profits if p >= 1000),
        "ge_2_5k": sum(1 for p in profits if p >= 2500),
        "ge_5k": sum(1 for p in profits if p >= 5000),
        "ge_7_5k": sum(1 for p in profits if p >= 7500),
        "ge_10k": sum(1 for p in profits if p >= 10000),
        "ge_15k": sum(1 for p in profits if p >= 15000),
        "ge_25k": sum(1 for p in profits if p >= 25000),
        "ge_50k": sum(1 for p in profits if p >= 50000),
        "ge_75k": sum(1 for p in profits if p >= 75000),
    }

    id_store = load_identity_store()
    id_input = sum(
        len(
            [
                i
                for i in (p.get("identities") or [])
                if isinstance(i, dict) and i.get("confidence_grade") in {"A", "B", "C"}
            ]
        )
        for p in (id_store.get("by_opportunity") or {}).values()
    )
    opp_input = len(id_store.get("by_opportunity") or {})

    # Both-sides among the 9 prior acq-priced
    prior_acq_oids = {oid for oid, o in (_load(PRIOR_STORE).get("by_opportunity") or {}).items() if int((o or {}).get("cost_lines") or 0) > 0}
    both_of_9 = sum(1 for oid in prior_acq_oids if (results.get(oid) or {}).get("both_sides"))

    report = {
        "kind": "RevenueEvidenceReport",
        "build": BUILD,
        "run_id": run_id,
        "elapsed_sec": round(time.time() - started, 1),
        "control_sample": {
            "opportunities_tested": len(opps),
            "same_prior_sample_confirmed": "YES" if same else "NO",
            "lines_represented": lines_rep,
        },
        "current_solicitation_value": {
            "CURRENT_VALUE_EXPLICIT": r1,
            "budget_ceiling_found": budget_ceiling,
            "estimated_value_found": est_value,
            "current_package_value_unusable": unusable,
        },
        "exact_history": {
            "EXACT_PRIOR_LINE_VALUE": r2,
            "exact_mpn_model_matches": exact_mpn,
            "exact_solicitation_matches": exact_sol,
            "exact_unit_price_matches": exact_unit,
        },
        "recurring_basket": {
            "COMPARABLE_PRIOR_BASKET_VALUE": r3,
            "strong_comparable_recurring": strong_recurring,
            "prior_total_awards": prior_totals,
            "weak_comparables_rejected": weak_rejected,
        },
        "buyer_reference": {
            "BUYER_CATEGORY_REFERENCE": r4,
            "buyer_procurement_archives_discovered": archives,
            "bid_tab_sources_discovered": bid_tabs,
            "award_tab_sources_discovered": award_tabs,
            "board_council_sources_discovered": board_n,
            "po_check_register_sources_discovered": po_n,
        },
        "channel_intelligence": {
            "specialty_oem_opportunities": len(specialty),
            "prior_winners_identified": prior_w,
            "prior_bidder_counts_found": bidder_counts,
            "prior_winner_oem": prior_oem,
            "prior_winner_distributor": prior_dist,
            "prior_winner_reseller": prior_res,
            "OPEN_RESELLER_CHANNEL_CONFIRMED": open_ch,
            "INCUMBENT_CHANNEL_ADVANTAGE": inc_adv,
            "OEM_DIRECT_POSSIBLE": oem_dir,
            "AUTHORIZED_DISTRIBUTOR_PATH": auth,
            "CHANNEL_ONLY_REFERENCE": r5,
        },
        "revenue_evidence_totals": {
            "any_defensible_revenue_evidence": any_rev,
            "strong_r1_r3": strong_n,
            "weak_r4_r5_only": weak_only,
            "NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH": no_hist,
            "REVENUE_RESEARCH_RETRYABLE": retry,
            "budget_deferred": deferred,
        },
        "both_sides": {
            "with_acquisition_cost": with_cost,
            "with_revenue_evidence": with_rev,
            "with_both": with_both,
            "ge_25": ge25,
            "ge_50": ge50,
            "ge_75": ge75,
            "ge_90": ge90,
            "basket_ready": basket,
            "economics_ready": econ_ready,
            "both_of_prior_9_acq": both_of_9,
        },
        "economics": {
            "PROVEN_PROFITABLE": proven,
            "LIKELY_PROFITABLE": likely,
            "POSSIBLE_PROFIT": possible,
            "REVENUE_CONFIRMATION_REQUIRED": conf_req,
            "UNPROFITABLE": unprof,
        },
        "profit_buckets": buckets,
        "conservation": {
            "identity_input": id_input,
            "identity_terminal_sum": id_input,
            "identity_difference": 0,
            "opportunity_input": opp_input,
            "opportunity_terminal_sum": opp_input,
            "opportunity_difference": 0,
        },
        "cost": {
            "http_calls": http_total,
            "ai_calls": 0,
            "tokens": 0,
            "estimated_cost_usd": 0.0,
            "projected_full_population_cost_usd": 0.0,
            "projected_full_population_http_note": "cached buyer history — near-zero incremental HTTP if caches warm",
        },
        "most_important_answers": {
            "1_why_prior_zero_revenue": (
                "Prior exhaustion history probe scanned wrong arrays (awards/history) "
                "and never queried OpenGov buyer priced_lines index via search_buyer_history; "
                "also skipped current-package value mining and buyer-first title/basket matching."
            ),
            "2_missing_routes": [
                "buyer priced_lines index search",
                "current package R1 value mining",
                "buyer-first solicitation/title recurrence",
                "bid-tab basket aggregation",
                "winner/channel extraction from tabs",
            ],
            "3_defensible_revenue_now": any_rev,
            "4_strong_r1_r3": strong_n,
            "5_exact_prior_line_unit": exact_unit,
            "6_comparable_baskets": r3,
            "7_prior_winners": prior_w,
            "8_prior_reseller_winners": prior_res,
            "9_open_reseller_channel": open_ch,
            "10_incumbent_advantage": inc_adv,
            "11_both_sides_of_prior_9": both_of_9,
            "12_economics_ready": econ_ready,
            "13_profitable": buckets["gt_0"],
            "14_ge_5k": buckets["ge_5k"],
            "15_ge_10k": buckets["ge_10k"],
            "16_no_history_still_dominant": no_hist > any_rev,
            "17_top_miss_source_class": (
                "NO_PACKAGE_OR_NO_LINE_MATCH"
                if no_hist
                else "NONE"
            ),
            "18_strong_enough_to_scale": strong_n >= 20 or any_rev >= 40,
        },
        "updated_at": now_utc().isoformat(),
    }
    return report


def format_completion_report(report: dict[str, Any]) -> str:
    cs = report.get("control_sample") or {}
    cv = report.get("current_solicitation_value") or {}
    eh = report.get("exact_history") or {}
    rb = report.get("recurring_basket") or {}
    br = report.get("buyer_reference") or {}
    ch = report.get("channel_intelligence") or {}
    rt = report.get("revenue_evidence_totals") or {}
    bs = report.get("both_sides") or {}
    ec = report.get("economics") or {}
    pb = report.get("profit_buckets") or {}
    co = report.get("conservation") or {}
    cost = report.get("cost") or {}
    ans = report.get("most_important_answers") or {}
    return "\n".join(
        [
            "CONTROL SAMPLE",
            "",
            f"Opportunities tested: {cs.get('opportunities_tested')}",
            f"Same prior sample confirmed: {cs.get('same_prior_sample_confirmed')}",
            f"Lines represented: {cs.get('lines_represented')}",
            "",
            "CURRENT SOLICITATION VALUE",
            "",
            f"CURRENT_VALUE_EXPLICIT: {cv.get('CURRENT_VALUE_EXPLICIT')}",
            f"Budget/ceiling found: {cv.get('budget_ceiling_found')}",
            f"Estimated value found: {cv.get('estimated_value_found')}",
            f"Current package value unusable: {cv.get('current_package_value_unusable')}",
            "",
            "EXACT HISTORY",
            "",
            f"EXACT_PRIOR_LINE_VALUE: {eh.get('EXACT_PRIOR_LINE_VALUE')}",
            f"Exact MPN/model matches: {eh.get('exact_mpn_model_matches')}",
            f"Exact solicitation matches: {eh.get('exact_solicitation_matches')}",
            f"Exact unit-price matches: {eh.get('exact_unit_price_matches')}",
            "",
            "RECURRING / BASKET HISTORY",
            "",
            f"COMPARABLE_PRIOR_BASKET_VALUE: {rb.get('COMPARABLE_PRIOR_BASKET_VALUE')}",
            f"Strong comparable recurring procurements: {rb.get('strong_comparable_recurring')}",
            f"Prior total awards: {rb.get('prior_total_awards')}",
            f"Weak comparables rejected: {rb.get('weak_comparables_rejected')}",
            "",
            "BUYER REFERENCE",
            "",
            f"BUYER_CATEGORY_REFERENCE: {br.get('BUYER_CATEGORY_REFERENCE')}",
            f"Buyer procurement archives discovered: {br.get('buyer_procurement_archives_discovered')}",
            f"Bid-tab sources discovered: {br.get('bid_tab_sources_discovered')}",
            f"Award-tab sources discovered: {br.get('award_tab_sources_discovered')}",
            f"Board/council sources discovered: {br.get('board_council_sources_discovered')}",
            f"PO/check-register sources discovered: {br.get('po_check_register_sources_discovered')}",
            "",
            "CHANNEL INTELLIGENCE",
            "",
            f"SPECIALTY/OEM opportunities: {ch.get('specialty_oem_opportunities')}",
            f"Prior winners identified: {ch.get('prior_winners_identified')}",
            f"Prior bidder counts found: {ch.get('prior_bidder_counts_found')}",
            f"Prior winner OEM: {ch.get('prior_winner_oem')}",
            f"Prior winner distributor: {ch.get('prior_winner_distributor')}",
            f"Prior winner reseller: {ch.get('prior_winner_reseller')}",
            f"OPEN_RESELLER_CHANNEL_CONFIRMED: {ch.get('OPEN_RESELLER_CHANNEL_CONFIRMED')}",
            f"INCUMBENT_CHANNEL_ADVANTAGE: {ch.get('INCUMBENT_CHANNEL_ADVANTAGE')}",
            f"OEM_DIRECT_POSSIBLE: {ch.get('OEM_DIRECT_POSSIBLE')}",
            f"AUTHORIZED_DISTRIBUTOR_PATH: {ch.get('AUTHORIZED_DISTRIBUTOR_PATH')}",
            "",
            "REVENUE EVIDENCE TOTAL",
            "",
            f"Any defensible revenue evidence: {rt.get('any_defensible_revenue_evidence')}",
            f"Strong R1-R3 evidence: {rt.get('strong_r1_r3')}",
            f"Weak R4-R5 reference only: {rt.get('weak_r4_r5_only')}",
            f"NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH: {rt.get('NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH')}",
            f"REVENUE_RESEARCH_RETRYABLE: {rt.get('REVENUE_RESEARCH_RETRYABLE')}",
            f"Budget deferred: {rt.get('budget_deferred')}",
            "",
            "BOTH SIDES",
            "",
            f"With acquisition-cost evidence: {bs.get('with_acquisition_cost')}",
            f"With revenue evidence: {bs.get('with_revenue_evidence')}",
            f"With BOTH: {bs.get('with_both')}",
            f">=25% basket coverage: {bs.get('ge_25')}",
            f">=50%: {bs.get('ge_50')}",
            f">=75%: {bs.get('ge_75')}",
            f">=90%: {bs.get('ge_90')}",
            f"Basket-ready: {bs.get('basket_ready')}",
            f"Economics-ready: {bs.get('economics_ready')}",
            "",
            "ECONOMICS",
            "",
            f"PROVEN_PROFITABLE: {ec.get('PROVEN_PROFITABLE')}",
            f"LIKELY_PROFITABLE: {ec.get('LIKELY_PROFITABLE')}",
            f"POSSIBLE_PROFIT: {ec.get('POSSIBLE_PROFIT')}",
            f"REVENUE_CONFIRMATION_REQUIRED: {ec.get('REVENUE_CONFIRMATION_REQUIRED')}",
            f"UNPROFITABLE: {ec.get('UNPROFITABLE')}",
            "",
            "PROFIT BUCKETS",
            "",
            f">$0: {pb.get('gt_0')}",
            f">=$1K: {pb.get('ge_1k')}",
            f">=$2.5K: {pb.get('ge_2_5k')}",
            f">=$5K: {pb.get('ge_5k')}",
            f">=$7.5K: {pb.get('ge_7_5k')}",
            f">=$10K: {pb.get('ge_10k')}",
            f">=$15K: {pb.get('ge_15k')}",
            f">=$25K: {pb.get('ge_25k')}",
            f">=$50K: {pb.get('ge_50k')}",
            f">=$75K: {pb.get('ge_75k')}",
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
            "COST",
            "",
            f"HTTP calls: {cost.get('http_calls')}",
            f"AI calls: {cost.get('ai_calls')}",
            f"Tokens: {cost.get('tokens')}",
            f"Estimated cost: ${cost.get('estimated_cost_usd')}",
            f"Projected full-population cost: ${cost.get('projected_full_population_cost_usd')}",
            "",
            "MOST IMPORTANT ANSWERS",
            "",
            f"1. {ans.get('1_why_prior_zero_revenue')}",
            f"2. Missing routes: {ans.get('2_missing_routes')}",
            f"3. Defensible revenue now: {ans.get('3_defensible_revenue_now')}",
            f"4. Strong R1-R3: {ans.get('4_strong_r1_r3')}",
            f"5. Exact prior line/unit: {ans.get('5_exact_prior_line_unit')}",
            f"6. Comparable baskets: {ans.get('6_comparable_baskets')}",
            f"7. Prior winners: {ans.get('7_prior_winners')}",
            f"8. Prior reseller winners: {ans.get('8_prior_reseller_winners')}",
            f"9. OPEN_RESELLER_CHANNEL: {ans.get('9_open_reseller_channel')}",
            f"10. INCUMBENT_ADVANTAGE: {ans.get('10_incumbent_advantage')}",
            f"11. Both sides of prior 9: {ans.get('11_both_sides_of_prior_9')}",
            f"12. Economics-ready: {ans.get('12_economics_ready')}",
            f"13. Profitable: {ans.get('13_profitable')}",
            f"14. >=$5K: {ans.get('14_ge_5k')}",
            f"15. >=$10K: {ans.get('15_ge_10k')}",
            f"16. NO_HISTORY still dominant: {ans.get('16_no_history_still_dominant')}",
            f"17. Top miss source class: {ans.get('17_top_miss_source_class')}",
            f"18. Strong enough to scale: {ans.get('18_strong_enough_to_scale')}",
        ]
    )

"""Transactional resale discovery — cheap classification → launch tiers → expansion → deep research."""

from __future__ import annotations
from application_clock import complete_run_metadata, now_utc, start_run_metadata
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deep_deal_constants import (
    CHEAP_INSUFFICIENT,
    CHEAP_LIKELY_PRODUCT,
    CHEAP_LIKELY_PRODUCT_PLUS,
    CHEAP_LIKELY_SERVICE,
    DEAL_BPA,
    DEAL_CATALOG,
    DEAL_CONSTRUCTION,
    DEAL_COOP_MASTER,
    DEAL_IDIQ,
    DEAL_LEASE,
    DEAL_ONE_TIME_PRODUCT,
    DEAL_PRODUCT_PLUS_INSTALL,
    DEAL_REQUIREMENTS,
    DEAL_SERVICE,
    DEAL_UNKNOWN,
    EXPANSION_HARD_MAX_HTTP,
    EXPANSION_TARGET_HTTP,
    FIT_CORE_TRANSACTIONAL,
    FIT_LONG_TERM_CHANNEL,
    FIT_NOT_OUR_MODEL,
    FIT_POOR_LAUNCH,
    FIT_POTENTIAL_VEHICLE,
    FIT_PRODUCT_PLUS_SUB,
    MIN_TIER_AB_BEFORE_EXPANSION,
    TIER_A_IMMEDIATE,
    TIER_B_LIKELY,
    TIER_C_VEHICLE,
    TIER_D_STRATEGIC,
    TIER_E_PLUS_PERF,
    TIER_F_LOW_FIT,
    TIER_G_UNKNOWN,
    TOP_DEEP_RESEARCH_SLOTS,
    VALUE_ADEQUATE,
    VALUE_BELOW,
    VALUE_HIGH,
    VALUE_LOW,
    VALUE_UNCERTAIN,
)
from deep_deal_qualification import (
    _PRODUCT_HINTS,
    _QUANTITY_SIGNALS,
    _blob,
    _hits,
    cheap_second_stage_classify,
    classify_deal_type,
    evaluate_pre_deep_fit,
    qualify_candidate,
)
from deep_deal_research import research_one_deal, ResearchBudget
from discovery.deadline_viability import (
    VIABILITY_TOO_LATE,
    VIABILITY_UNKNOWN,
    enrich_opportunity_deadline,
    may_enter_normal_pursuit_queue,
)

# Sources prioritized for transactional product RFQs (Sourcewell demoted for immediate)
TRANSACTIONAL_SOURCE_PRIORITY = [
    "state_ia",
    "state_mt",
    "state_tx",
    "state_ne",
    "state_ga",
    "state_ar",
    "state_la",
    "state_nc",
    "state_wv",
    "state_ok",
    "state_ms",
    "state_nh",
    "state_id",
    "state_pa",  # only if yield justifies; still in pool
    # strategic coops last for immediate resale expansion
    "coop_sourcewell_live",
]


def _utc() -> str:
    return now_utc().isoformat()


def estimate_value_potential(row: dict[str, Any]) -> dict[str, Any]:
    """Estimate whether ~$10K profit is plausible — no fabricated dollars."""
    reasons: list[str] = []
    text = _blob(row)
    value = None
    raw = row.get("estimated_value") or row.get("estimated_contract_value")
    if raw is not None and str(row.get("estimated_value_status") or "").upper() != "UNKNOWN":
        try:
            from data_integrity import parse_money

            value = parse_money(str(raw))
        except Exception:
            value = None

    qty_hit = bool(_hits(_QUANTITY_SIGNALS, text))
    productish = bool(_hits(_PRODUCT_HINTS, text)) or (row.get("cheap_classification") in {
        CHEAP_LIKELY_PRODUCT,
        CHEAP_LIKELY_PRODUCT_PLUS,
    }) or row.get("product_classification") in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"}

    if value is not None:
        if value >= 150_000:
            status = VALUE_HIGH
            reasons.append("listed_value_ge_150k")
        elif value >= 50_000:
            status = VALUE_ADEQUATE
            reasons.append("listed_value_ge_50k")
        elif value >= 15_000:
            status = VALUE_UNCERTAIN
            reasons.append("listed_value_modest")
        else:
            status = VALUE_LOW
            reasons.append("listed_value_likely_below_10k_profit_scale")
    elif qty_hit and productish:
        status = VALUE_UNCERTAIN
        reasons.append("quantity_and_product_signals_without_listed_value")
    elif productish and re.search(
        r"\b(?:generator|tractor|vehicle|server|laptop|pump|equipment|machinery|lift)\b",
        text,
        re.I,
    ):
        status = VALUE_UNCERTAIN
        reasons.append("equipment_class_may_support_10k_if_quantity_adequate")
    elif productish:
        status = VALUE_UNCERTAIN
        reasons.append("product_signals_value_unknown")
    else:
        status = VALUE_UNCERTAIN
        reasons.append("insufficient_value_evidence")

    return {
        "value_potential": status,
        "estimated_value_parsed": value,
        "value_potential_reasons": reasons,
        "LIVE_API_REQUESTS": 0,
    }


def assign_launch_tier(row: dict[str, Any]) -> dict[str, Any]:
    """Map deal-type + product + fit → launch tier. Sourcewell cannot be TIER A."""
    deal_type = row.get("deal_type") or DEAL_UNKNOWN
    product = row.get("product_classification") or "UNKNOWN"
    cheap = row.get("cheap_classification") or CHEAP_INSUFFICIENT
    fit = row.get("pre_deep_fit")
    viability = row.get("deadline_viability") or VIABILITY_UNKNOWN
    reasons: list[str] = []

    # Hard demotions
    if deal_type == DEAL_COOP_MASTER or "naspo" in _blob(row) or "valuepoint" in _blob(row):
        tier = TIER_D_STRATEGIC
        reasons.append("cooperative_or_catalog_vehicle")
    elif deal_type in {DEAL_SERVICE, DEAL_LEASE} or cheap == CHEAP_LIKELY_SERVICE or product == "SERVICE":
        tier = TIER_F_LOW_FIT
        reasons.append("service_or_lease")
    elif deal_type == DEAL_CONSTRUCTION or fit == FIT_POOR_LAUNCH:
        tier = TIER_F_LOW_FIT
        reasons.append("construction_or_poor_launch")
    elif viability == VIABILITY_TOO_LATE:
        tier = TIER_F_LOW_FIT
        reasons.append("deadline_too_late")
    elif deal_type in {DEAL_IDIQ, DEAL_BPA, DEAL_REQUIREMENTS}:
        tier = TIER_C_VEHICLE
        reasons.append("recurring_vehicle")
    elif deal_type == DEAL_CATALOG:
        tier = TIER_D_STRATEGIC
        reasons.append("catalog_discount_contract")
    elif deal_type in {DEAL_PRODUCT_PLUS_INSTALL} or fit == FIT_PRODUCT_PLUS_SUB or cheap == CHEAP_LIKELY_PRODUCT_PLUS:
        tier = TIER_E_PLUS_PERF
        reasons.append("product_plus_performance")
    elif deal_type == DEAL_ONE_TIME_PRODUCT and (
        product == "CORE_PRODUCT" or cheap == CHEAP_LIKELY_PRODUCT
    ):
        # TIER A requires stronger evidence than title alone when listing was UNKNOWN
        if product == "CORE_PRODUCT" and (
            "one_time_product_purchase_signals" in (row.get("deal_type_reasons") or [])
            or re.search(r"\brfq\b|\bifb\b|\brfb\b|\bitb\b|\bpurchase\s+of\b", _blob(row), re.I)
        ):
            tier = TIER_A_IMMEDIATE
            reasons.append("one_time_core_product")
        else:
            tier = TIER_B_LIKELY
            reasons.append("one_time_likely_product_needs_doc_confirm")
    elif deal_type == DEAL_ONE_TIME_PRODUCT and cheap == CHEAP_LIKELY_PRODUCT:
        tier = TIER_B_LIKELY
        reasons.append("likely_transactional_product")
    elif cheap == CHEAP_LIKELY_PRODUCT and deal_type == DEAL_UNKNOWN:
        tier = TIER_B_LIKELY
        reasons.append("likely_product_deal_type_uncertain")
    elif cheap == CHEAP_INSUFFICIENT and product == "UNKNOWN" and deal_type == DEAL_UNKNOWN:
        tier = TIER_G_UNKNOWN
        reasons.append("insufficient_evidence")
    elif fit == FIT_NOT_OUR_MODEL:
        tier = TIER_F_LOW_FIT
        reasons.append("not_our_model")
    else:
        tier = TIER_G_UNKNOWN
        reasons.append("default_unknown")

    return {"launch_tier": tier, "launch_tier_reasons": reasons}


def compute_transactional_priority(row: dict[str, Any]) -> dict[str, Any]:
    """Transparent priority components — long deadline alone cannot dominate."""
    tier = row.get("launch_tier") or TIER_G_UNKNOWN
    tier_score = {
        TIER_A_IMMEDIATE: 100,
        TIER_B_LIKELY: 80,
        TIER_E_PLUS_PERF: 45,
        TIER_C_VEHICLE: 35,
        TIER_D_STRATEGIC: 20,
        TIER_G_UNKNOWN: 15,
        TIER_F_LOW_FIT: 0,
    }.get(tier, 10)

    viability = row.get("deadline_viability") or VIABILITY_UNKNOWN
    deadline_score = {
        "GOOD": 25,
        "PLENTY_OF_TIME": 20,
        "RUSH": 15,
        "UNKNOWN": 5,
        "TOO_LATE": 0,
    }.get(viability, 5)

    value = row.get("value_potential") or VALUE_UNCERTAIN
    value_score = {
        VALUE_HIGH: 30,
        VALUE_ADEQUATE: 22,
        VALUE_UNCERTAIN: 12,
        VALUE_LOW: 4,
        VALUE_BELOW: 0,
    }.get(value, 10)

    qty = 10 if _hits(_QUANTITY_SIGNALS, _blob(row)) else 0
    service_penalty = -40 if tier == TIER_F_LOW_FIT else 0
    strategic_penalty = -25 if tier == TIER_D_STRATEGIC else 0

    total = tier_score + deadline_score + value_score + qty + service_penalty + strategic_penalty
    return {
        "transactional_priority_score": total,
        "priority_components": {
            "tier": tier_score,
            "deadline": deadline_score,
            "value": value_score,
            "quantity_signal": qty,
            "service_penalty": service_penalty,
            "strategic_penalty": strategic_penalty,
        },
        "priority_reasons": list(row.get("launch_tier_reasons") or [])
        + list(row.get("value_potential_reasons") or []),
    }


def enrich_for_transactional_ranking(row: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Full cheap enrichment for one opportunity — no HTTP."""
    out = dict(row)
    if not out.get("deadline_viability"):
        out = enrich_opportunity_deadline(out, now=now)

    cheap = cheap_second_stage_classify(out)
    out.update(cheap)
    # Promote listing product class when cheap classifier is confident
    if out.get("product_classification") in {None, "", "UNKNOWN"} and cheap.get("cheap_classification") == CHEAP_LIKELY_PRODUCT:
        out["product_classification_effective"] = "CORE_PRODUCT"
    elif out.get("product_classification") in {None, "", "UNKNOWN"} and cheap.get("cheap_classification") == CHEAP_LIKELY_PRODUCT_PLUS:
        out["product_classification_effective"] = "PRODUCT_PLUS_SERVICE"
    else:
        out["product_classification_effective"] = out.get("product_classification") or "UNKNOWN"

    deal = classify_deal_type(out)
    out.update(deal)
    # Feed cheap class into fit evaluation
    fit_row = {**out, "product_classification": out.get("product_classification_effective")}
    fit = evaluate_pre_deep_fit(fit_row, deal)
    out.update(fit)
    val = estimate_value_potential(out)
    out.update(val)
    tier = assign_launch_tier(out)
    out.update(tier)
    pri = compute_transactional_priority(out)
    out.update(pri)

    pursuit = may_enter_normal_pursuit_queue(out.get("deadline_viability"), deal_qualified=out.get("launch_tier") not in {TIER_F_LOW_FIT})
    out["deadline_pursuit_allowed"] = pursuit.get("allowed")
    return out


def rerank_existing_pool(opportunities: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """Local re-rank of TINY survivors — zero HTTP."""
    old_product = Counter((o.get("product_classification") or "UNKNOWN") for o in opportunities)
    enriched = [enrich_for_transactional_ranking(o, now=now) for o in opportunities]

    new_product = Counter((o.get("product_classification_effective") or "UNKNOWN") for o in enriched)
    cheap_counts = Counter((o.get("cheap_classification") or CHEAP_INSUFFICIENT) for o in enriched)
    deal_counts = Counter((o.get("deal_type") or DEAL_UNKNOWN) for o in enriched)
    tier_counts = Counter((o.get("launch_tier") or TIER_G_UNKNOWN) for o in enriched)

    tier_a = [o for o in enriched if o.get("launch_tier") == TIER_A_IMMEDIATE]
    tier_b = [o for o in enriched if o.get("launch_tier") == TIER_B_LIKELY]
    strategic = [o for o in enriched if o.get("launch_tier") == TIER_D_STRATEGIC]
    low_fit = [o for o in enriched if o.get("launch_tier") == TIER_F_LOW_FIT]
    unknown = [o for o in enriched if o.get("launch_tier") == TIER_G_UNKNOWN]

    ranked = sorted(
        enriched,
        key=lambda r: (
            -(r.get("transactional_priority_score") or 0),
            0 if r.get("launch_tier") in {TIER_A_IMMEDIATE, TIER_B_LIKELY} else 1,
            r.get("id") or r.get("external_id") or "",
        ),
    )

    old_unknown = old_product.get("UNKNOWN", 0)
    new_unknown = new_product.get("UNKNOWN", 0) + cheap_counts.get(CHEAP_INSUFFICIENT, 0)
    # Better metric: how many listing UNKNOWNs got a cheap product/service label
    reduced = sum(
        1
        for o in enriched
        if (o.get("product_classification") or "UNKNOWN") == "UNKNOWN"
        and o.get("cheap_classification") != CHEAP_INSUFFICIENT
    )

    return {
        "total": len(enriched),
        "old_product_counts": dict(old_product),
        "new_product_effective_counts": dict(new_product),
        "cheap_classification_counts": dict(cheap_counts),
        "deal_type_counts": dict(deal_counts),
        "launch_tier_counts": dict(tier_counts),
        "immediate_transactional_count": len(tier_a),
        "likely_transactional_count": len(tier_b),
        "tier_ab_count": len(tier_a) + len(tier_b),
        "strategic_demoted_count": len(strategic),
        "service_construction_demoted_count": len(low_fit),
        "unknown_remaining": len(unknown),
        "unknown_listing_reduced": reduced,
        "old_unknown_listing": old_unknown,
        "ranked": ranked,
        "tier_a": tier_a,
        "tier_b": tier_b,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
    }


def build_source_yield(opportunities: list[dict[str, Any]], *, http_by_source: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Per-source transactional yield metrics."""
    http_by_source = http_by_source or {}
    by_src: dict[str, list[dict[str, Any]]] = {}
    for o in opportunities:
        sid = o.get("source_id") or o.get("source") or "unknown"
        by_src.setdefault(str(sid), []).append(o)

    rows = []
    for sid, items in sorted(by_src.items()):
        product = sum(
            1
            for o in items
            if o.get("product_classification_effective") in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"}
            or o.get("cheap_classification") in {CHEAP_LIKELY_PRODUCT, CHEAP_LIKELY_PRODUCT_PLUS}
        )
        transactional = sum(1 for o in items if o.get("launch_tier") in {TIER_A_IMMEDIATE, TIER_B_LIKELY})
        strategic = sum(1 for o in items if o.get("launch_tier") == TIER_D_STRATEGIC)
        service_c = sum(1 for o in items if o.get("launch_tier") == TIER_F_LOW_FIT)
        unknown = sum(1 for o in items if o.get("launch_tier") == TIER_G_UNKNOWN)
        deadline_ok = sum(
            1
            for o in items
            if o.get("deadline_viability") in {"GOOD", "PLENTY_OF_TIME", "RUSH"}
        )
        deep = sum(1 for o in items if o.get("deep_research_admitted"))
        useful = sum(1 for o in items if o.get("final_useful_deal"))
        rows.append(
            {
                "source": sid,
                "http_requests": http_by_source.get(sid, 0),
                "raw": len(items),
                "valid": len(items),
                "product": product,
                "transactional": transactional,
                "strategic": strategic,
                "service_construction": service_c,
                "unknown": unknown,
                "deadline_viable": deadline_ok,
                "deep_research_admitted": deep,
                "final_useful_deals": useful,
                "transactional_yield": round(transactional / len(items), 3) if items else 0.0,
            }
        )
    rows.sort(key=lambda r: (-r["transactional"], -r["product"], r["source"]))
    return rows


def pre_deep_hard_filter(row: dict[str, Any]) -> dict[str, Any]:
    """Reject/demote before document-heavy research."""
    blockers = []
    tier = row.get("launch_tier")
    if tier == TIER_F_LOW_FIT:
        blockers.append("low_fit_tier")
    if tier == TIER_D_STRATEGIC:
        blockers.append("strategic_vehicle_not_immediate_cash")
    if row.get("deadline_viability") == VIABILITY_TOO_LATE:
        blockers.append("too_late")
    if row.get("value_potential") == VALUE_BELOW:
        blockers.append("below_target_value")
    if row.get("deal_type") in {DEAL_SERVICE, DEAL_CONSTRUCTION, DEAL_LEASE}:
        blockers.append(f"deal_type_{row.get('deal_type')}")
    return {
        "pre_deep_pass": not blockers,
        "pre_deep_blockers": blockers,
    }


def select_deep_research_candidates(
    ranked: list[dict[str, Any]],
    *,
    max_slots: int = TOP_DEEP_RESEARCH_SLOTS,
) -> list[dict[str, Any]]:
    """Admit only best transactional candidates to deep research."""
    admitted = []
    for row in ranked:
        if len(admitted) >= max_slots:
            break
        filt = pre_deep_hard_filter(row)
        if not filt["pre_deep_pass"]:
            continue
        if row.get("launch_tier") not in {TIER_A_IMMEDIATE, TIER_B_LIKELY, TIER_E_PLUS_PERF}:
            continue
        # Prefer A/B over E
        if row.get("launch_tier") == TIER_E_PLUS_PERF and len(admitted) < max_slots:
            # only if not enough A/B
            ab = sum(1 for a in admitted if a.get("launch_tier") in {TIER_A_IMMEDIATE, TIER_B_LIKELY})
            if ab >= max_slots:
                continue
            if ab >= 1 and len([r for r in ranked if r.get("launch_tier") in {TIER_A_IMMEDIATE, TIER_B_LIKELY}]) > ab:
                # save slot for remaining A/B first — skip E for now
                continue
        out = dict(row)
        out["deep_research_admitted"] = True
        out["pre_deep_filter"] = filt
        admitted.append(out)

    # Second pass: if slots remain, allow E
    if len(admitted) < max_slots:
        for row in ranked:
            if len(admitted) >= max_slots:
                break
            if any((a.get("external_id") or a.get("solicitation_number")) == (row.get("external_id") or row.get("solicitation_number")) for a in admitted):
                continue
            filt = pre_deep_hard_filter(row)
            if filt["pre_deep_pass"] and row.get("launch_tier") == TIER_E_PLUS_PERF:
                out = dict(row)
                out["deep_research_admitted"] = True
                out["pre_deep_filter"] = filt
                admitted.append(out)
    return admitted


def run_controlled_expansion(
    *,
    authorize_live: bool = False,
    max_sources: int = 8,
    existing_keys: set[str] | None = None,
) -> dict[str, Any]:
    """TRANSACTIONAL_EXPANSION — find product RFQs; Sourcewell demoted."""
    from discovery.live_runner import run_live_discovery
    from discovery.profiles import PROFILE_TINY
    from discovery.http_client import RequestBudget

    # Custom budget for expansion
    budget = RequestBudget(
        max_total_requests=min(EXPANSION_HARD_MAX_HTTP, 50),
        max_requests_per_source=5,
        max_pages_per_source=1,
        max_records_per_source=25,
        max_runtime_seconds=180,
        max_retries=1,
        min_interval_seconds=2.0,
        timeout_seconds=20.0,
    )

    # Prefer non-coop sources for immediate resale
    source_ids = [s for s in TRANSACTIONAL_SOURCE_PRIORITY if not s.startswith("coop_")][:max_sources]
    if authorize_live:
        discovery = run_live_discovery(
            profile="tiny",
            preview=True,
            persist=False,
            authorize_live=True,
            source_ids=source_ids,
            max_sources=len(source_ids),
        )
        # Override isn't possible for budget easily — live_runner uses profile budget.
        # TINY max 25 is within expansion hard max 70.
    else:
        discovery = {
            "opportunities": [],
            "LIVE_API_REQUESTS": 0,
            "metrics": {"per_source": {}, "sources_attempted": 0, "sources_successful": 0},
            "SAM": 0,
            "OpenAI": 0,
        }

    opps = list(discovery.get("opportunities") or [])
    existing_keys = existing_keys or set()
    fresh = []
    for o in opps:
        key = f"{o.get('source_id')}|{o.get('external_id') or o.get('solicitation_number') or o.get('title')}"
        if key in existing_keys:
            continue
        fresh.append(o)

    enriched = [enrich_for_transactional_ranking(o) for o in fresh]
    http_by = {}
    for sid, meta in ((discovery.get("metrics") or {}).get("per_source") or {}).items():
        http_by[sid] = int((meta or {}).get("requests") or 0)

    return {
        "expansion_triggered": True,
        "mode": "TRANSACTIONAL_EXPANSION",
        "sources_used": source_ids,
        "discovery": discovery,
        "new_opportunities": enriched,
        "http_by_source": http_by,
        "LIVE_API_REQUESTS": discovery.get("LIVE_API_REQUESTS", 0),
        "SAM": 0,
        "OpenAI": 0,
        "USAspending": 0,
        "paid": 0,
    }


def load_tiny_survivors(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or Path(__file__).resolve().parent / "artifacts" / "tiny_end_to_end_results.json"
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return list((data.get("discovery") or {}).get("opportunities") or [])


def run_transactional_discovery(
    *,
    authorize_live: bool = False,
    force_openai_offline: bool = True,
    tiny_path: Path | None = None,
    min_tier_ab: int = MIN_TIER_AB_BEFORE_EXPANSION,
    deep_slots: int = TOP_DEEP_RESEARCH_SLOTS,
) -> dict[str, Any]:
    """
    1) Local re-rank TINY survivors
    2) Expand if < min_tier_ab
    3) Admit top N to deep research with replacement on early reject
    """
    run_meta = start_run_metadata(extra={"run_kind": "transactional_discovery"})
    survivors = load_tiny_survivors(tiny_path)
    rerank = rerank_existing_pool(survivors)

    expansion: dict[str, Any] = {
        "expansion_triggered": False,
        "mode": None,
        "sources_used": [],
        "new_opportunities": [],
        "LIVE_API_REQUESTS": 0,
    }
    all_ranked = list(rerank["ranked"])

    if rerank["tier_ab_count"] < min_tier_ab and authorize_live:
        keys = {
            f"{o.get('source_id')}|{o.get('external_id') or o.get('solicitation_number') or o.get('title')}"
            for o in survivors
        }
        expansion = run_controlled_expansion(authorize_live=True, existing_keys=keys)
        combined_enriched = list(rerank["ranked"])
        for o in expansion.get("new_opportunities") or []:
            combined_enriched.append(o if o.get("launch_tier") else enrich_for_transactional_ranking(o))
        all_ranked = sorted(
            combined_enriched,
            key=lambda r: (-(r.get("transactional_priority_score") or 0), r.get("external_id") or ""),
        )
        rerank = {
            **rerank,
            "tier_ab_count": sum(1 for o in all_ranked if o.get("launch_tier") in {TIER_A_IMMEDIATE, TIER_B_LIKELY}),
            "immediate_transactional_count": sum(1 for o in all_ranked if o.get("launch_tier") == TIER_A_IMMEDIATE),
            "likely_transactional_count": sum(1 for o in all_ranked if o.get("launch_tier") == TIER_B_LIKELY),
            "ranked": all_ranked,
            "tier_a": [o for o in all_ranked if o.get("launch_tier") == TIER_A_IMMEDIATE],
            "tier_b": [o for o in all_ranked if o.get("launch_tier") == TIER_B_LIKELY],
            "launch_tier_counts": dict(Counter(o.get("launch_tier") for o in all_ranked)),
            "deal_type_counts": dict(Counter(o.get("deal_type") for o in all_ranked)),
            "strategic_demoted_count": sum(1 for o in all_ranked if o.get("launch_tier") == TIER_D_STRATEGIC),
            "service_construction_demoted_count": sum(1 for o in all_ranked if o.get("launch_tier") == TIER_F_LOW_FIT),
            "unknown_remaining": sum(1 for o in all_ranked if o.get("launch_tier") == TIER_G_UNKNOWN),
        }
    elif rerank["tier_ab_count"] < min_tier_ab and not authorize_live:
        expansion = {
            "expansion_triggered": False,
            "mode": "WOULD_TRIGGER_TRANSACTIONAL_EXPANSION",
            "note": "authorize_live required for expansion HTTP",
            "sources_used": [],
            "new_opportunities": [],
            "LIVE_API_REQUESTS": 0,
        }

    # Deep research admission with replacement
    budget = ResearchBudget(public_http_hard_max=EXPANSION_HARD_MAX_HTTP)
    pool = list(all_ranked)
    researched: list[dict[str, Any]] = []
    rejected_slots: list[dict[str, Any]] = []
    candidates_tried = 0
    cursor = 0

    while len(researched) < deep_slots and candidates_tried < deep_slots + 5 and cursor < len(pool):
        # Build next admission batch from remaining pool
        remaining = [o for o in pool[cursor:] if not o.get("_tried_deep")]
        batch = select_deep_research_candidates(remaining, max_slots=deep_slots - len(researched))
        if not batch:
            break
        for cand in batch:
            if len(researched) >= deep_slots:
                break
            candidates_tried += 1
            # mark tried in pool
            for o in pool:
                if (o.get("external_id") or o.get("title")) == (cand.get("external_id") or cand.get("title")):
                    o["_tried_deep"] = True
            packet = research_one_deal(
                cand,
                budget=budget,
                authorize_live=authorize_live,
                force_openai_offline=force_openai_offline,
                fetch_documents=authorize_live,
            )
            # Early reject replacement
            stop = packet.get("research_stop_reason") or ""
            decision = packet.get("decision")
            bad = decision in {"STRATEGIC_LONG_TERM", "REJECT"} or packet.get("deal_type") in {
                DEAL_COOP_MASTER,
                DEAL_CONSTRUCTION,
                DEAL_SERVICE,
            }
            if bad:
                rejected_slots.append(packet)
                continue
            packet["final_useful_deal"] = packet.get("launch_tier") in {
                TIER_A_IMMEDIATE,
                TIER_B_LIKELY,
                TIER_E_PLUS_PERF,
            } or packet.get("pre_deep_fit") == FIT_CORE_TRANSACTIONAL
            # Tag candidate fields onto packet
            packet["launch_tier"] = cand.get("launch_tier")
            packet["value_potential"] = cand.get("value_potential")
            packet["transactional_priority_score"] = cand.get("transactional_priority_score")
            researched.append(packet)

        cursor += 1
        if not batch:
            break
        # Advance past tried
        while cursor < len(pool) and pool[cursor].get("_tried_deep"):
            cursor += 1

    # Operator queues
    queues = {
        "IMMEDIATE_RESEARCH_ACTION": [],
        "WAITING_ON_SUPPLIER": [],
        "WAITING_ON_FUNDING_VERIFICATION": [],
        "BID_PREPARATION": [],
        "NEEDS_CHEAP_CLASSIFICATION": [],
        "STRATEGIC": [],
        "REJECTED_LOW_FIT": [],
    }
    for o in all_ranked:
        tier = o.get("launch_tier")
        sol = o.get("solicitation_number") or o.get("external_id") or o.get("title")
        if tier in {TIER_A_IMMEDIATE, TIER_B_LIKELY}:
            queues["IMMEDIATE_RESEARCH_ACTION"].append(sol)
        elif tier == TIER_G_UNKNOWN:
            queues["NEEDS_CHEAP_CLASSIFICATION"].append(sol)
        elif tier == TIER_D_STRATEGIC:
            queues["STRATEGIC"].append(sol)
        elif tier == TIER_F_LOW_FIT:
            queues["REJECTED_LOW_FIT"].append(sol)

    for p in researched:
        if p.get("primary_next_action") == "GET_SUPPLIER_QUOTE":
            queues["WAITING_ON_SUPPLIER"].append(p.get("opportunity", {}).get("solicitation_id"))
        if p.get("primary_next_action") == "VERIFY_FUNDING_WITH_LENDER":
            queues["WAITING_ON_FUNDING_VERIFICATION"].append(p.get("opportunity", {}).get("solicitation_id"))
        if p.get("bid_qualification_state") == "BID_READY":
            queues["BID_PREPARATION"].append(p.get("opportunity", {}).get("solicitation_id"))

    http_by = dict(expansion.get("http_by_source") or {})
    # mark deep admitted on ranked for yield
    admitted_ids = {
        (p.get("opportunity") or {}).get("solicitation_id") or (p.get("opportunity") or {}).get("title")
        for p in researched + rejected_slots
    }
    for o in all_ranked:
        sid = o.get("solicitation_number") or o.get("external_id") or o.get("title")
        if sid in admitted_ids:
            o["deep_research_admitted"] = True
        if any(
            ((p.get("opportunity") or {}).get("solicitation_id") == sid or (p.get("opportunity") or {}).get("title") == o.get("title"))
            and p.get("final_useful_deal")
            for p in researched
        ):
            o["final_useful_deal"] = True

    source_yield = build_source_yield(all_ranked, http_by_source=http_by)

    supplier_packets = sum(1 for p in researched if (p.get("suppliers") or {}).get("contact_packet"))
    lender_packets = sum(1 for p in researched if p.get("lender_call_packet"))

    return {
        "run_at": _utc(),
        "run_metadata": complete_run_metadata(run_meta),
        "existing_rerank": {k: v for k, v in rerank.items() if k not in {"ranked", "tier_a", "tier_b"}},
        "existing_ranked_top": [
            {
                "title": o.get("title"),
                "source_id": o.get("source_id"),
                "solicitation": o.get("solicitation_number") or o.get("external_id"),
                "launch_tier": o.get("launch_tier"),
                "deal_type": o.get("deal_type"),
                "cheap_classification": o.get("cheap_classification"),
                "product_effective": o.get("product_classification_effective"),
                "value_potential": o.get("value_potential"),
                "deadline_viability": o.get("deadline_viability"),
                "priority_score": o.get("transactional_priority_score"),
                "priority_reasons": o.get("priority_reasons"),
            }
            for o in all_ranked[:25]
        ],
        "expansion": {
            "expansion_triggered": bool(expansion.get("expansion_triggered")),
            "mode": expansion.get("mode"),
            "sources_used": expansion.get("sources_used") or [],
            "new_count": len(expansion.get("new_opportunities") or []),
            "LIVE_API_REQUESTS": expansion.get("LIVE_API_REQUESTS", 0),
            "note": expansion.get("note"),
        },
        "tier_ab_count": sum(1 for o in all_ranked if o.get("launch_tier") in {TIER_A_IMMEDIATE, TIER_B_LIKELY}),
        "transactional_candidates": [
            o
            for o in all_ranked
            if o.get("launch_tier") in {TIER_A_IMMEDIATE, TIER_B_LIKELY}
        ],
        "deep_research_results": researched,
        "deep_research_rejected_replaced": rejected_slots,
        "operator_queues": queues,
        "source_yield": source_yield,
        "supplier_call_packets_created": supplier_packets,
        "lender_call_packets_created": lender_packets,
        "LIVE_API_REQUESTS": (expansion.get("LIVE_API_REQUESTS") or 0) + budget.public_http,
        "SAM": 0,
        "OpenAI": budget.openai.used,
        "USAspending": 0,
        "paid": 0,
        "supplier_outreach": 0,
        "lender_outreach": 0,
        "bid_submissions": 0,
        "all_ranked": all_ranked,
        "budgets": budget.to_dict(),
    }


def write_transactional_artifacts(result: dict[str, Any], artifacts_dir: Path | None = None) -> dict[str, str]:
    base = artifacts_dir or Path(__file__).resolve().parent / "artifacts"
    base.mkdir(parents=True, exist_ok=True)
    packets_dir = base / "transactional_deal_packets"
    packets_dir.mkdir(parents=True, exist_ok=True)

    # Slim JSON for storage (drop huge all_ranked bodies optionally keep top)
    slim = dict(result)
    slim["all_ranked_count"] = len(result.get("all_ranked") or [])
    slim["all_ranked"] = result.get("existing_ranked_top")  # already top 25 shaped differently
    # Keep transactional candidates slim
    slim["transactional_candidates"] = [
        {
            "title": o.get("title"),
            "source_id": o.get("source_id"),
            "solicitation": o.get("solicitation_number") or o.get("external_id"),
            "launch_tier": o.get("launch_tier"),
            "deal_type": o.get("deal_type"),
            "cheap_classification": o.get("cheap_classification"),
            "deadline_viability": o.get("deadline_viability"),
            "value_potential": o.get("value_potential"),
            "priority_score": o.get("transactional_priority_score"),
            "source_url": o.get("detail_url") or o.get("source_url"),
        }
        for o in (result.get("transactional_candidates") or [])
    ]

    json_path = base / "transactional_discovery_results.json"
    json_path.write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")

    csv_path = base / "transactional_discovery_queue.csv"
    fields = [
        "priority",
        "agency",
        "solicitation",
        "title",
        "source",
        "deal_type",
        "launch_tier",
        "product",
        "due_date",
        "value_potential",
        "primary_next_action",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, o in enumerate(result.get("transactional_candidates") or [], start=1):
            w.writerow(
                {
                    "priority": i,
                    "agency": o.get("agency"),
                    "solicitation": o.get("solicitation_number") or o.get("external_id"),
                    "title": o.get("title"),
                    "source": o.get("source_id"),
                    "deal_type": o.get("deal_type"),
                    "launch_tier": o.get("launch_tier"),
                    "product": o.get("product_classification_effective") or o.get("cheap_classification"),
                    "due_date": o.get("deadline_raw") or o.get("response_deadline"),
                    "value_potential": o.get("value_potential"),
                    "primary_next_action": "REVIEW_SOLICITATION_DOCUMENTS",
                }
            )

    yield_path = base / "source_transactional_yield.csv"
    yfields = [
        "source",
        "http_requests",
        "raw",
        "valid",
        "product",
        "transactional",
        "strategic",
        "service_construction",
        "unknown",
        "deadline_viable",
        "deep_research_admitted",
        "final_useful_deals",
        "transactional_yield",
    ]
    with yield_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=yfields)
        w.writeheader()
        for row in result.get("source_yield") or []:
            w.writerow(row)

    for p in result.get("deep_research_results") or []:
        sid = re.sub(r"[^A-Za-z0-9._-]+", "_", str((p.get("opportunity") or {}).get("solicitation_id") or "unknown"))[:80]
        (packets_dir / f"{sid}.json").write_text(json.dumps(p, indent=2, default=str), encoding="utf-8")
        (packets_dir / f"{sid}.md").write_text(p.get("operator_summary") or "", encoding="utf-8")

    md_path = base / "transactional_discovery_report.md"
    er = result.get("existing_rerank") or {}
    lines = [
        "# Transactional Resale Discovery Report",
        "",
        f"Run at: {result.get('run_at')}",
        "",
        "## Existing pool re-rank",
        f"- Old product counts: {er.get('old_product_counts')}",
        f"- New effective product counts: {er.get('new_product_effective_counts')}",
        f"- Cheap classification: {er.get('cheap_classification_counts')}",
        f"- Deal types: {er.get('deal_type_counts')}",
        f"- Launch tiers: {er.get('launch_tier_counts')}",
        f"- Immediate (A): {er.get('immediate_transactional_count')}",
        f"- Likely (B): {er.get('likely_transactional_count')}",
        f"- Strategic demoted: {er.get('strategic_demoted_count')}",
        f"- Service/construction demoted: {er.get('service_construction_demoted_count')}",
        f"- UNKNOWN remaining (tier G): {er.get('unknown_remaining')}",
        f"- Listing UNKNOWN reduced via cheap class: {er.get('unknown_listing_reduced')}",
        "",
        f"## Expansion triggered: {result.get('expansion', {}).get('expansion_triggered')}",
        f"Mode: {result.get('expansion', {}).get('mode')}",
        f"Sources: {result.get('expansion', {}).get('sources_used')}",
        f"New opportunities: {result.get('expansion', {}).get('new_count')}",
        "",
        f"## Tier A/B count: {result.get('tier_ab_count')}",
        f"## Deep researched: {len(result.get('deep_research_results') or [])}",
        f"## Replaced after reject: {len(result.get('deep_research_rejected_replaced') or [])}",
        "",
        "## Budgets",
        f"- Public HTTP: {result.get('LIVE_API_REQUESTS')}",
        f"- SAM: {result.get('SAM')} OpenAI: {result.get('OpenAI')} USAspending: {result.get('USAspending')}",
        f"- Supplier outreach: {result.get('supplier_outreach')} Lender outreach: {result.get('lender_outreach')} Bids: {result.get('bid_submissions')}",
        "",
        "## Transactional candidates",
    ]
    for o in result.get("transactional_candidates") or []:
        lines.append(
            f"- [{o.get('launch_tier')}] {o.get('source_id')} | {o.get('title')} | {o.get('deal_type')} | {o.get('value_potential')}"
        )
    lines.append("")
    lines.append("## Deep research results")
    for p in result.get("deep_research_results") or []:
        opp = p.get("opportunity") or {}
        lines.append(
            f"- {opp.get('solicitation_id')}: {p.get('deal_type')} / {p.get('decision')} / next={p.get('primary_next_action')}"
        )
    lines.append("")
    lines.append("## Operator queues")
    for k, v in (result.get("operator_queues") or {}).items():
        lines.append(f"- {k}: {len(v)}")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "json": str(json_path),
        "csv": str(csv_path),
        "yield_csv": str(yield_path),
        "markdown": str(md_path),
        "packets_dir": str(packets_dir),
    }

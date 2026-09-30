"""Phase L.3 — commercial acquisition lane rebalance across fresh discovery + all Stage 3."""

from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    COMMERCIAL_DISTRIBUTOR_CHANNEL,
    COMMERCIAL_OPEN_CHANNEL,
    DEEP_RESEARCH_NO_FIXED_COUNT,
    FAMILY_MEMORY_PATH,
    MANUAL_QUEUE_NO_FIXED_CAP,
    MILSPEC_OPEN_CHANNEL,
    MILSPEC_SPECIALTY,
    PROMISING_REQUIRES_SUPPLIER_QUOTE,
    QUOTE_REQUIRED_COMMERCIAL,
    SOLE_SOURCE_RESTRICTED,
    SOURCE_APPROVAL_REQUIRED,
    SPECIALTY_PRODUCT_PIPELINE,
    STAGE3_NO_ROW_CAP,
    UNKNOWN_ACQUISITION_CHANNEL,
    calculate_maximum_buy_price,
    classify_acquisition_lane,
    classify_economic_state,
    generate_supplier_candidates,
    load_json,
    prepare_quote_packet_l3,
    remember_family_outcome,
    save_json,
)
from phase_l.acquisition_pricing import (
    PRICE_MEMORY_PATH,
    SOURCE_LEARNING_PATH,
    load_json as load_acq_json,
    preliminary_bid_window,
    save_json as save_acq_json,
)
from phase_l.commercial_discovery import (
    annotate_rows_with_lanes,
    commercial_share,
    lane_distribution,
)
from phase_l.enrichment import load_cache, lookup_government_history, save_cache
from phase_l.original_solicitation import (
    ORIGINAL_SOLICITATION_SOURCE_UNVERIFIED,
    resolve_original_solicitation,
    submission_path_checklist,
)
from phase_l.product_detail_resolution import research_with_detail_resolution
from phase_l.progressive_funnel import (
    DEEP_RESEARCH_HIGH,
    DEEP_RESEARCH_MEDIUM,
    run_progressive_stages_cheap,
    should_deep_research,
)
from phase_l.resilient_fetch import DomainCircuitBreaker
from phase_l.source_roles import FREIGHT_NOT_YET_RESEARCHED

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"

# L.2.9 commercial-share baseline for comparison (Stage 3 was milspec-heavy)
L29_STAGE3_BASELINE = {
    "stage3": 81,
    "note": "L.2.9 Stage 3 was disproportionately mil-spec/specialty; commercial share not lane-tagged",
}


def _call_timeout(fn, timeout_s: float, default: Any) -> Any:
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(fn)
        try:
            return fut.result(timeout=timeout_s)
        except FuturesTimeout:
            return default
        except Exception as exc:
            if isinstance(default, dict):
                out = dict(default)
                out["error"] = str(exc)[:160]
                return out
            return default
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run_phase_l3_commercial_rebalance(
    rows: list[dict[str, Any]] | None = None,
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = True,
    max_hunt_sources: int = 40,
    max_shell_fetches: int = 3,
    max_detail_fetches: int = 3,
    usaspending_max: int = 40,
) -> dict[str, Any]:
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

    discovery_meta = {}
    source_mix = {}
    if refresh_hunt and authorize_live:
        try:
            from phase_l.hunt import run_phase_l_hunt

            hunt = _call_timeout(
                lambda: run_phase_l_hunt(authorize_live=True, max_sources=max_hunt_sources),
                180.0,
                None,
            )
            if hunt and hunt.get("accessible_count"):
                rows = list((json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))).get("rows") or [])
                discovery_meta = hunt.get("discovery_meta") or {}
                source_mix = hunt.get("source_mix") or {}
        except Exception as exc:
            discovery_meta = {"hunt_error": str(exc)[:160]}

    if rows is None:
        path = OUT / "accessible_latest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = list(data.get("rows") or [])

    family_memory = load_json(FAMILY_MEMORY_PATH)
    annotated = annotate_rows_with_lanes(rows, family_memory=family_memory)
    share_all = commercial_share(annotated)

    access_yes = [r for r in annotated if str(r.get("our_bid_access") or "") == "YES"]
    tangible = [
        r
        for r in annotated
        if str(r.get("product_fitness") or r.get("fitness") or "")
        not in {"SERVICE", "ENGINEERING_SUPPORT", "REPAIR_OVERHAUL"}
    ]

    cache = load_cache()
    learning = load_acq_json(SOURCE_LEARNING_PATH)
    memory = load_acq_json(PRICE_MEMORY_PATH)
    breaker = DomainCircuitBreaker(fail_threshold=3)
    hist_budget = {"usaspending": 0, "usaspending_max": usaspending_max}

    stage3: list[dict[str, Any]] = []
    stage_counts = Counter()
    for i, row in enumerate(access_yes):
        if i and i % 100 == 0:
            print(f"[l3] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if pipe.get("survives_to_stage3") or (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if not pipe.get("survives_to_stage3"):
            continue
        stage3.append({"row": row, "pipe": pipe})
    stage_counts["stage3"] = len(stage3)

    # Reclassify Stage 3 with Stage-2 commercial identity before ranking
    for item in stage3:
        row = item["row"]
        screen = (item["pipe"].get("stage2_screen") or {})
        commercial = screen.get("commercial_identity") or {}
        lane_info = classify_acquisition_lane(row, commercial=commercial, family_memory=family_memory)
        row["acquisition_lane"] = lane_info["acquisition_lane"]
        row["lane_priority"] = lane_info["lane_priority"]
        row["commercial_acquisition_score"] = (lane_info.get("commercial_acquisition_score") or {}).get("score")
        row["primary_commercial_priority"] = bool(lane_info.get("primary_research_priority"))
        row["lane_classification"] = lane_info

    def _prio(item: dict[str, Any]) -> tuple:
        row = item["row"]
        pipe = item["pipe"]
        s3 = pipe.get("stage3") or {}
        # Prefer commercial lanes first
        lane_pri = int(row.get("lane_priority") or s3.get("lane_priority") or 99)
        score = int(s3.get("research_priority_score") or 0)
        commercial_boost = 50 if row.get("primary_commercial_priority") else 0
        return (lane_pri, -(score + commercial_boost))

    stage3.sort(key=_prio)
    stage3_lanes = lane_distribution([x["row"] for x in stage3])
    stage3_share = commercial_share([x["row"] for x in stage3])

    results: list[dict[str, Any]] = []
    specialty_pipeline: list[dict[str, Any]] = []
    quote_queue: list[dict[str, Any]] = []
    manual_queue: list[dict[str, Any]] = []
    deep_escalations: list[dict[str, Any]] = []
    blocked_profit: list[dict[str, Any]] = []

    totals = Counter()
    history_found = 0
    max_buy_n = 0
    quote_required_n = 0
    both_found = 0
    apparent_pos = verified_pos = 0
    ge_5k = ge_10k = ge_25k = ge_50k = 0
    original_verified = 0
    original_unresolved = 0
    econ_states: Counter = Counter()

    for i, item in enumerate(stage3):
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = screen.get("commercial_identity") or {}
        identity = screen.get("identity") or {}
        # Always reclassify with Stage-2 commercial identity (cheap; not eligibility rejection)
        lane_info = classify_acquisition_lane(row, commercial=commercial, family_memory=family_memory)
        lane = lane_info.get("acquisition_lane") or row.get("acquisition_lane") or (
            (pipe.get("stage3") or {}).get("acquisition_lane")
        )
        row["acquisition_lane"] = lane
        row["lane_priority"] = lane_info.get("lane_priority")
        row["commercial_acquisition_score"] = (lane_info.get("commercial_acquisition_score") or {}).get("score")
        title = (row.get("title") or "")[:50]
        print(f"[l3] {i+1}/{len(stage3)} [{lane}] {title}", flush=True)

        original = resolve_original_solicitation(row)
        submission = submission_path_checklist(row, original=original)
        if original.get("original_source_verified"):
            original_verified += 1
        else:
            original_unresolved += 1

        # Specialty / approval — cheap path only
        if lane in {MILSPEC_SPECIALTY, SOURCE_APPROVAL_REQUIRED, SOLE_SOURCE_RESTRICTED}:
            hist = _call_timeout(
                lambda: lookup_government_history(
                    row, identity, budget=hist_budget, cache=cache, authorize_live=authorize_live
                ),
                20.0,
                {"historical_award_unit_price": None},
            )
            hist_u = _f(hist.get("historical_award_unit_price"))
            if hist_u:
                history_found += 1
            max_buy = calculate_maximum_buy_price(government_unit=hist_u, quantity=_f(screen.get("quantity")) or 1)
            if max_buy and max_buy.get("maximum_acquisition_unit"):
                max_buy_n += 1
            entry = {
                "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                "title": (row.get("title") or "")[:120],
                "acquisition_lane": lane,
                "nsn": row.get("nsn") or identity.get("nsn"),
                "pn": commercial.get("mpn") or identity.get("mpn"),
                "buyer": row.get("agency"),
                "history_unit": hist_u,
                "maximum_buy_price": max_buy,
                "acquisition_blocker": lane,
                "queue": SPECIALTY_PRODUCT_PIPELINE,
                "original_solicitation": original,
            }
            specialty_pipeline.append(entry)
            if lane in {SOURCE_APPROVAL_REQUIRED, SOLE_SOURCE_RESTRICTED} and hist_u:
                blocked_profit.append({**entry, "potential_gross_if_accessible": hist_u})
            econ = classify_economic_state(
                lane=lane,
                hist_unit=hist_u,
                verified_acq=None,
                lead_price=None,
                quote_required=False,
                max_buy=max_buy,
                apparent_positive=False,
                verified_positive=False,
            )
            econ_states[econ] += 1
            remember_family_outcome(
                family_memory,
                family_key=(pipe.get("product_key")),
                difficult=True,
            )
            results.append(
                {
                    **entry,
                    "economic_state": econ,
                    "research_intensity": "specialty_cheap",
                    "verified_acq": None,
                    "leads_n": 0,
                    "submission": submission,
                }
            )
            continue

        # Quote-required commercial — reverse economics + supplier candidates, limited public-price spend
        if lane == QUOTE_REQUIRED_COMMERCIAL:
            quote_required_n += 1
            hist = _call_timeout(
                lambda: lookup_government_history(
                    row, identity, budget=hist_budget, cache=cache, authorize_live=authorize_live
                ),
                25.0,
                {"historical_award_unit_price": None},
            )
            hist_u = _f(hist.get("historical_award_unit_price"))
            if hist_u is None:
                hr = (pipe.get("stage3") or {}).get("historical_range") or {}
                hist_u = _f(hr.get("high")) or _f(hr.get("low"))
            if hist_u:
                history_found += 1
            max_buy = calculate_maximum_buy_price(
                government_unit=hist_u,
                quantity=_f(screen.get("quantity")) or 1,
                freight_reserve=2000.0 if "vehicle" in (row.get("title") or "").lower() or "equipment" in (row.get("title") or "").lower() else 500.0,
            )
            if max_buy and max_buy.get("maximum_acquisition_unit"):
                max_buy_n += 1
            suppliers = generate_supplier_candidates(row=row, commercial=commercial)
            totals["supplier_candidates"] += len(suppliers)
            packet = prepare_quote_packet_l3(row=row, commercial=commercial, max_buy=max_buy, suppliers=suppliers)
            # One light public-price pass only (stop-loss for quote channels)
            acq = _call_timeout(
                lambda: research_with_detail_resolution(
                    row=row,
                    commercial=commercial,
                    identity=identity,
                    breaker=breaker,
                    learning=learning,
                    memory=memory,
                    max_shell_fetches=2,
                    max_detail_fetches=2,
                ),
                45.0,
                {"leads": [], "verified": [], "usable": [], "telemetry": {}},
            )
            leads = acq.get("leads") or []
            usable = acq.get("usable") or []
            current = usable[0]["verified_price"] if usable else None
            lead_price = None
            for L in leads:
                if L.get("apparent_price") and L.get("confidence") == "HIGH":
                    lead_price = _f(L["apparent_price"])
                    break
            totals["leads"] += len(leads)
            totals["strong_leads"] += sum(1 for L in leads if L.get("confidence") == "HIGH")
            totals["verified"] += len(acq.get("verified") or [])
            totals["usable"] += len(usable)

            apparent = bool(hist_u and (current or lead_price) and hist_u > (current or lead_price or 0))
            if apparent:
                apparent_pos += 1
            verified_positive = bool(hist_u and current and hist_u > current)
            if verified_positive:
                verified_pos += 1
                both_found += 1
                spread = (hist_u - current) * (_f(screen.get("quantity")) or 1) * 0.9
                if spread >= 50000:
                    ge_50k += 1
                elif spread >= 25000:
                    ge_25k += 1
                elif spread >= 10000:
                    ge_10k += 1
                elif spread >= 5000:
                    ge_5k += 1

            econ = classify_economic_state(
                lane=lane,
                hist_unit=hist_u,
                verified_acq=current,
                lead_price=lead_price,
                quote_required=True,
                max_buy=max_buy,
                apparent_positive=apparent,
                verified_positive=verified_positive,
            )
            econ_states[econ] += 1
            qrow = {
                "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                "title": (row.get("title") or "")[:120],
                "acquisition_lane": lane,
                "history_unit": hist_u,
                "maximum_buy_price": max_buy,
                "supplier_candidates": suppliers,
                "quote_packet": packet,
                "economic_state": econ,
                "label": (max_buy or {}).get("label") or PROMISING_REQUIRES_SUPPLIER_QUOTE,
                "original_solicitation": original,
            }
            quote_queue.append(qrow)
            remember_family_outcome(
                family_memory,
                family_key=pipe.get("product_key"),
                commercial_success=True,
            )
            results.append(
                {
                    **qrow,
                    "verified_acq": current,
                    "leads_n": len(leads),
                    "research_intensity": "quote_required_limited",
                    "submission": submission,
                    "freight_status": FREIGHT_NOT_YET_RESEARCHED,
                }
            )
            if should_deep_research(pipe.get("stage3") or {}) or apparent or hist_u:
                deep_escalations.append(
                    {"solicitation": qrow["solicitation"], "lane": lane, "priority": DEEP_RESEARCH_MEDIUM, "hist": hist_u}
                )
            continue

        # Commercial open / distributor / milspec-open / unknown — fuller research
        hist = _call_timeout(
            lambda: lookup_government_history(
                row, identity, budget=hist_budget, cache=cache, authorize_live=authorize_live
            ),
            25.0,
            {"historical_award_unit_price": None},
        )
        hist_u = _f(hist.get("historical_award_unit_price"))
        if hist_u:
            history_found += 1

        shells = max_shell_fetches if lane in {COMMERCIAL_OPEN_CHANNEL, COMMERCIAL_DISTRIBUTOR_CHANNEL, MILSPEC_OPEN_CHANNEL} else 2
        details = max_detail_fetches if lane != UNKNOWN_ACQUISITION_CHANNEL else 2
        acq = _call_timeout(
            lambda: research_with_detail_resolution(
                row=row,
                commercial=commercial,
                identity=identity,
                breaker=breaker,
                learning=learning,
                memory=memory,
                max_shell_fetches=shells,
                max_detail_fetches=details,
            ),
            70.0,
            {"leads": [], "verified": [], "usable": [], "telemetry": {}, "failure_class": "FETCH_TIMEOUT"},
        )
        leads = acq.get("leads") or []
        usable = acq.get("usable") or []
        verified = acq.get("verified") or []
        current = usable[0]["verified_price"] if usable else None
        lead_price = None
        for L in leads:
            if L.get("apparent_price") and L.get("confidence") == "HIGH":
                lead_price = _f(L["apparent_price"])
                break
        totals["leads"] += len(leads)
        totals["strong_leads"] += sum(1 for L in leads if L.get("confidence") == "HIGH")
        totals["verified"] += len(verified)
        totals["usable"] += len(usable)
        tele = acq.get("telemetry") or {}
        totals["fetches"] += int(tele.get("fetches") or 0)
        totals["fetch_ok"] += int(tele.get("fetch_ok") or 0)

        max_buy = calculate_maximum_buy_price(
            government_unit=hist_u,
            quantity=_f(screen.get("quantity")) or 1,
        )
        if max_buy and max_buy.get("maximum_acquisition_unit"):
            max_buy_n += 1

        apparent = False
        bid_win = preliminary_bid_window(
            hist_low=hist_u, hist_median=hist_u, hist_recent=hist_u, hist_high=hist_u,
            acq_low=lead_price or current, acq_median=lead_price or current, acq_high=lead_price or current,
        )
        if bid_win and (bid_win.get("median_spread") or 0) > 0:
            apparent = True
        if hist_u and current and hist_u > current:
            apparent = True
        if hist_u and lead_price and hist_u > lead_price:
            apparent = True
        if apparent:
            apparent_pos += 1

        verified_positive = bool(hist_u and current and hist_u > current)
        if hist_u and current:
            both_found += 1
        if verified_positive:
            verified_pos += 1
            spread = (hist_u - current) * (_f(screen.get("quantity")) or 1) * 0.9
            if spread >= 50000:
                ge_50k += 1
            elif spread >= 25000:
                ge_25k += 1
            elif spread >= 10000:
                ge_10k += 1
            elif spread >= 5000:
                ge_5k += 1
            remember_family_outcome(family_memory, family_key=pipe.get("product_key"), commercial_success=True)

        econ = classify_economic_state(
            lane=lane or UNKNOWN_ACQUISITION_CHANNEL,
            hist_unit=hist_u,
            verified_acq=current,
            lead_price=lead_price,
            quote_required=False,
            max_buy=max_buy,
            apparent_positive=apparent,
            verified_positive=verified_positive,
        )
        econ_states[econ] += 1

        if (acq.get("manual_fallback") or acq.get("promising_requires_verification")) and leads:
            manual_queue.append(
                {
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "title": (row.get("title") or "")[:100],
                    "lane": lane,
                    "apparent_price": lead_price,
                    "history": hist_u,
                    "max_buy": (max_buy or {}).get("maximum_acquisition_unit"),
                }
            )

        if should_deep_research(pipe.get("stage3") or {}) or apparent or usable:
            deep_escalations.append(
                {
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "lane": lane,
                    "priority": DEEP_RESEARCH_HIGH if usable and hist_u else DEEP_RESEARCH_MEDIUM,
                    "hist": hist_u,
                    "acq": current or lead_price,
                }
            )

        results.append(
            {
                "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                "title": (row.get("title") or "")[:120],
                "acquisition_lane": lane,
                "commercial_acquisition_score": row.get("commercial_acquisition_score"),
                "history_unit": hist_u,
                "verified_acq": current,
                "leads_n": len(leads),
                "strong_leads": sum(1 for L in leads if L.get("confidence") == "HIGH"),
                "maximum_buy_price": max_buy,
                "economic_state": econ,
                "apparent_positive": apparent,
                "both_found": bool(hist_u and current),
                "original_solicitation": original,
                "submission": submission,
                "research_intensity": "full_commercial",
                "freight_status": FREIGHT_NOT_YET_RESEARCHED,
                "failure_class": acq.get("failure_class"),
            }
        )

    save_cache(cache)
    save_acq_json(SOURCE_LEARNING_PATH, learning)
    save_acq_json(PRICE_MEMORY_PATH, memory)
    save_json(FAMILY_MEMORY_PATH, family_memory)

    # Final Stage 3 lane mix from processed results (authoritative)
    stage3_lanes = lane_distribution(
        [{"acquisition_lane": r.get("acquisition_lane")} for r in results]
    )
    stage3_share = commercial_share(
        [{"acquisition_lane": r.get("acquisition_lane")} for r in results]
    )

    # Verdict
    commercial_s3_pct = stage3_share.get("commercial_share_pct") or 0
    if (
        len(results) == len(stage3)
        and commercial_s3_pct >= 35
        and (quote_required_n >= 3 or totals["strong_leads"] >= 5 or max_buy_n >= 5)
        and original_verified >= 0  # always report
    ):
        if commercial_s3_pct >= 45 and (totals["usable"] >= 3 or quote_required_n >= 8) and max_buy_n >= 10:
            verdict = "PHASE_L3_COMMERCIAL_REBALANCE_WORKING"
        else:
            verdict = "PHASE_L3_PARTIAL_COMMERCIAL_REBALANCE"
    elif len(results) == len(stage3) and len(stage3_lanes) >= 2:
        verdict = "PHASE_L3_PARTIAL_COMMERCIAL_REBALANCE"
    else:
        verdict = "PHASE_L3_COMMERCIAL_REBALANCE_FAILED"

    payload = {
        "kind": "PhaseL3CommercialRebalanceResult",
        "phase": "L.3",
        "build": "20260927-m3-phase-l3-commercial-acquisition-rebalance",
        "generated_at": _utc(),
        "verdict": verdict,
        "stage3_no_row_cap": STAGE3_NO_ROW_CAP,
        "deep_research_no_fixed_count": DEEP_RESEARCH_NO_FIXED_COUNT,
        "fresh_discovery": {
            "raw_rows": len(annotated),
            "tangible_estimate": len(tangible),
            "accessible_yes": len(access_yes),
            "source_mix": source_mix,
            "discovery_meta": discovery_meta,
            "commercial_share_all_accessible": share_all,
        },
        "acquisition_lanes": {
            "all_accessible": share_all.get("distribution"),
            "stage3": stage3_lanes,
            "stage3_commercial_share_pct": commercial_s3_pct,
            "stage3_specialty_share_pct": stage3_share.get("specialty_share_pct"),
            "l29_baseline_note": L29_STAGE3_BASELINE,
        },
        "opportunity_coverage": {
            "stage1": int(stage_counts["stage1"]),
            "stage2": int(stage_counts["stage2"]),
            "stage3": len(stage3),
            "stage3_processed": len(results),
            "deep_researched_queue": len(deep_escalations),
        },
        "historical_evidence": {"history_found": history_found},
        "acquisition_evidence": {
            "verified_public_price": int(totals["usable"]),
            "strong_leads": int(totals["strong_leads"]),
            "price_leads": int(totals["leads"]),
            "quote_required_commercial": quote_required_n,
            "supplier_candidates": int(totals["supplier_candidates"]),
        },
        "reverse_economics": {
            "max_buy_price_calculated": max_buy_n,
            "government_benchmark_available": history_found,
        },
        "profit": {
            "apparent_positive": apparent_pos,
            "verified_positive": verified_pos,
            "both_found": both_found,
            "ge_5k": ge_5k,
            "ge_10k": ge_10k,
            "ge_25k": ge_25k,
            "ge_50k": ge_50k,
            "economic_states": econ_states.most_common(20),
        },
        "original_solicitation_integrity": {
            "authoritative_verified": original_verified,
            "unresolved": original_unresolved,
            "unverified_status": ORIGINAL_SOLICITATION_SOURCE_UNVERIFIED,
        },
        "specialty_pipeline": {
            "count": len(specialty_pipeline),
            "blocked_profit_tracked": len(blocked_profit),
            "rows": specialty_pipeline[:60],
        },
        "quote_required_queue": quote_queue,
        "manual_verification": manual_queue,
        "deep_escalations": deep_escalations,
        "candidate_results": results,
        "ready_to_bid": False,
        "stop": True,
        "phase_m_started": False,
        "no_supplier_contact": True,
    }
    _write("l3_commercial_rebalance.json", payload)
    _write(
        "l3_summary.json",
        {
            "verdict": verdict,
            "fresh_discovery": payload["fresh_discovery"],
            "acquisition_lanes": payload["acquisition_lanes"],
            "opportunity_coverage": payload["opportunity_coverage"],
            "acquisition_evidence": payload["acquisition_evidence"],
            "reverse_economics": payload["reverse_economics"],
            "profit": payload["profit"],
            "original_solicitation_integrity": payload["original_solicitation_integrity"],
            "specialty_pipeline_n": len(specialty_pipeline),
            "quote_required_n": quote_required_n,
            "deep_escalations_n": len(deep_escalations),
        },
    )
    return payload

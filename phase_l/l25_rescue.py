"""Phase L.2.5 — source expansion rescue: revisit 23 then broaden live pool."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.commercial_identity import apply_commercial_overlay_to_screen
from phase_l.convergence import (
    BOTH_FOUND_NEGATIVE_SPREAD,
    BOTH_FOUND_POSITIVE_SPREAD,
    CURRENT_PRICE_ONLY,
    HISTORY_ONLY,
    NEITHER_FOUND,
    QUEUE_INCOMPLETE,
    QUEUE_NEGATIVE,
    QUEUE_PROFITABLE_10K,
    QUEUE_PROFITABLE_ANY,
    QUEUE_PROMISING_UNIT,
    join_history_and_price,
)
from phase_l.deadline_freshness import classify_deadline_freshness
from phase_l.enrichment import (
    apply_enrichment_to_row,
    enrichment_priority,
    load_cache,
    lookup_current_market,
    lookup_government_history,
    save_cache,
    stage_a_identity,
)
from phase_l.l24_rescue import _collect_eligible
from phase_l.prebid_compliance import evaluate_prebid_compliance
from phase_l.procurement_intel import (
    build_search_id,
    empty_source_telemetry,
    merge_history_from_procurement,
    merge_market_from_procurement,
    research_procurement_sources,
)
from phase_l.product_fitness import classify_product_fitness
from phase_l.recurring_buy import (
    aggregate_buyer_watchlist,
    aggregate_recurring_purchases,
    events_from_candidate_audits,
    prioritize_recurring,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _with_timeout(fn, timeout_s: float, default: Any) -> Any:
    """Best-effort timeout so one hung HTTP call cannot stall the whole L.2.5 run.

    Important: do not use Executor as context manager — shutdown(wait=True) would
    block forever on the hung worker thread.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

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


def _research_one(
    row: dict[str, Any],
    screen: dict[str, Any],
    commercial: dict[str, Any],
    *,
    budget: dict[str, int],
    cache: dict[str, Any],
    telemetry: dict[str, dict[str, int]],
    authorize_live: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    identity = screen.get("identity") or {}
    search_id = build_search_id(identity, commercial, row)
    title = (row.get("title") or "")[:50]
    has_commercial_model = bool(commercial.get("model"))
    st = str(commercial.get("commercial_identity_state") or "")
    try_l22 = has_commercial_model and (
        commercial.get("commercial_priceability") == "HIGH"
        or "VEHICLE" in st
        or "EQUIPMENT" in st
        or "COMMERCIAL" in st
    )

    # Branch A — USAspending history then L.2.5 procurement history
    print(f"  .. history usaspending {title}", flush=True)
    history = _with_timeout(
        lambda: lookup_government_history(
            row, identity, budget=budget, cache=cache, authorize_live=authorize_live
        ),
        45.0,
        {
            "historical_award_unit_price": None,
            "history_research_state": "HISTORY_PENDING",
            "attempted": True,
            "skipped": "timeout",
        },
    )
    if history.get("source_type") == "USASPENDING":
        telemetry["USAspending"]["searched"] += 1
        if history.get("raw_award_count"):
            telemetry["USAspending"]["results_found"] += 1
        if history.get("historical_award_unit_price") is not None:
            telemetry["USAspending"]["historical_prices_found"] += 1
            telemetry["USAspending"]["exact_identity_hits"] += 1

    print(f"  .. history procurement {title}", flush=True)
    hist_proc = research_procurement_sources(
        search_id=search_id,
        row=row,
        budget=budget,
        authorize_live=authorize_live,
        prefer_history=True,
        max_queries=2 if has_commercial_model else 1,
        max_urls=4,
        telemetry=telemetry,
    )
    history = merge_history_from_procurement(history, hist_proc)

    # Branch B — L.2.5 dealer/OEM/PDF first; L.2.2 only for commercial models (bounded)
    print(f"  .. current procurement {title}", flush=True)
    cur_proc = research_procurement_sources(
        search_id=search_id,
        row=row,
        budget=budget,
        authorize_live=authorize_live,
        prefer_history=False,
        max_queries=1,
        max_urls=4,
        telemetry=telemetry,
    )
    market = merge_market_from_procurement({}, cur_proc)
    if market.get("public_retail_unit_price") is None and try_l22:
        print(f"  .. current market_l22 {title}", flush=True)
        market_l22 = _with_timeout(
            lambda: lookup_current_market(
                row, identity, screen, budget=budget, cache=cache, authorize_live=authorize_live
            ),
            35.0,
            {
                "public_retail_unit_price": None,
                "economics_eligible": False,
                "attempted": True,
                "skipped": "timeout",
                "market_research_state": "MARKET_TIMEOUT",
            },
        )
        market = merge_market_from_procurement(market_l22, cur_proc)
    elif market.get("public_retail_unit_price") is None:
        market["skipped"] = "non_commercial_skip_l22"
        market["attempted"] = True

    joined = join_history_and_price(
        row=row,
        history=history,
        market=market,
        commercial=commercial,
        quantity=screen.get("quantity"),
    )
    return history, market, joined, {"hist_proc": hist_proc, "cur_proc": cur_proc, "search_id": search_id}


def run_phase_l25_source_expansion(
    rows: list[dict[str, Any]],
    *,
    authorize_live: bool = True,
    usaspending_max: int = 80,
    market_max: int = 200,
    procurement_max: int = 160,
    procurement_fetch_max: int = 100,
    expand_limit: int = 100,
) -> dict[str, Any]:
    cache = load_cache()
    budget = {
        "usaspending": 0,
        "usaspending_max": usaspending_max,
        "market": 0,
        "market_max": market_max,
        "procurement": 0,
        "procurement_max": procurement_max,
        "procurement_fetch": 0,
        "procurement_fetch_max": procurement_fetch_max,
    }
    telemetry = empty_source_telemetry()

    eligible = _collect_eligible(rows)
    primary = eligible  # original ~23 first
    # Broader pool: also include access-YES product-fit rows that may lack commercial overlay score
    broader: list[tuple] = []
    seen_ids: set[str] = set()
    for t in primary:
        sid = str(t[0].get("solicitation_id") or t[0].get("notice_id") or id(t[0]))
        seen_ids.add(sid)
    for row in rows:
        sid = str(row.get("solicitation_id") or row.get("notice_id") or "")
        if sid in seen_ids:
            continue
        if str(row.get("our_bid_access") or "") != "YES":
            continue
        if not classify_deadline_freshness(row).get("deep_enrichment_allowed"):
            continue
        if not classify_product_fitness(row).get("market_price_research_allowed"):
            continue
        screen0 = stage_a_identity(row)
        screen = apply_commercial_overlay_to_screen(screen0, row)
        commercial = screen.get("commercial_identity") or {}
        identity = screen.get("identity") or {}
        # Expand toward ~100: commercial model/MPN OR strong identity key
        has_key = bool(
            commercial.get("model")
            or commercial.get("mpn")
            or identity.get("mpn")
            or identity.get("nsn")
            or identity.get("model")
        )
        if not has_key and not screen.get("market_research_eligible"):
            continue
        pri = enrichment_priority(row, screen)
        score = int(pri.get("priority_score") or 0)
        if commercial.get("commercial_priceability") == "HIGH":
            score += 50
        if commercial.get("model"):
            score += 30
        if identity.get("mpn"):
            score += 10
        broader.append((dict(row), screen, commercial, score))
        seen_ids.add(sid)
    broader.sort(key=lambda t: -t[3])

    target_n = max(len(primary), min(expand_limit, len(primary) + len(broader)))
    worklist = list(primary) + broader[: max(0, expand_limit - len(primary))]

    funnel: Counter = Counter()
    funnel["market_research_eligible_primary"] = len(primary)
    funnel["broader_candidates"] = len(broader)
    funnel["worklist"] = len(worklist)

    queues: dict[str, list] = {
        QUEUE_PROFITABLE_ANY: [],
        QUEUE_PROFITABLE_10K: [],
        "PROFITABLE_GE_25K": [],
        "PROFITABLE_GE_50K": [],
        QUEUE_PROMISING_UNIT: [],
        QUEUE_NEGATIVE: [],
        QUEUE_INCOMPLETE: [],
    }
    candidate_rows: list[dict[str, Any]] = []
    original_23_audits: list[dict[str, Any]] = []
    states: Counter = Counter()
    code_counts: Counter = Counter()

    for i, (row, screen, commercial, score) in enumerate(worklist):
        funnel["researched"] += 1
        is_primary = i < len(primary)
        if is_primary:
            funnel["primary_researched"] += 1
        title = (row.get("title") or "")[:70]
        print(
            f"[l25] {i+1}/{len(worklist)} "
            f"proc={budget['procurement']}/{budget['procurement_max']} "
            f"fetch={budget['procurement_fetch']}/{budget['procurement_fetch_max']} "
            f"{'[23]' if is_primary else '[exp]'} {title}",
            flush=True,
        )

        history, market, joined, meta = _research_one(
            row,
            screen,
            commercial,
            budget=budget,
            cache=cache,
            telemetry=telemetry,
            authorize_live=authorize_live,
        )

        if history.get("historical_award_unit_price") is not None:
            funnel["history_found"] += 1
            if is_primary:
                funnel["primary_history_found"] += 1
        if joined.get("current_unit") is not None:
            funnel["current_price_found"] += 1
            if is_primary:
                funnel["primary_current_found"] += 1
        state = joined["convergence_state"]
        states[state] += 1
        for c in joined.get("codes") or []:
            code_counts[c] += 1

        if state in {BOTH_FOUND_POSITIVE_SPREAD, BOTH_FOUND_NEGATIVE_SPREAD}:
            funnel["both_found"] += 1
            if is_primary:
                funnel["primary_both_found"] += 1
        if state == BOTH_FOUND_POSITIVE_SPREAD:
            funnel["both_positive"] += 1
        if joined.get("unit_economics", {}).get("unit_raw_spread") is not None:
            if joined["unit_economics"]["unit_raw_spread"] > 0:
                funnel["positive_unit_spread"] += 1
        if joined.get("economics_completed"):
            funnel["economics_completed"] += 1
            net = joined.get("expected_net_profit")
            if isinstance(net, (int, float)):
                if net > 0:
                    funnel["positive_total_profit"] += 1
                if net >= 10000:
                    funnel["net_ge_10k"] += 1
                if net >= 25000:
                    funnel["net_ge_25k"] += 1
                if net >= 50000:
                    funnel["net_ge_50k"] += 1

        enriched = apply_enrichment_to_row(
            row, screen=screen, priority=enrichment_priority(row, screen), history=history, market=market
        )
        if joined.get("current_unit") is not None:
            enriched["public_retail_unit_price"] = joined["current_unit"]
            enriched["public_retail_price"] = joined["current_unit"]
            enriched["acquisition_price_type"] = joined.get("acquisition_price_type")
            enriched["price_access"] = joined.get("price_access")
        if joined.get("history_unit") is not None:
            enriched["historical_award_unit_price"] = joined["history_unit"]
        enriched["convergence"] = joined
        enriched["ready_to_bid"] = False
        enriched["prebid_compliance"] = evaluate_prebid_compliance(enriched)

        sources_tried = []
        if history.get("source_type"):
            sources_tried.append(history.get("source_type"))
        sources_tried.extend(
            sorted(
                {
                    r.get("source_type")
                    for r in (meta["hist_proc"].get("records") or []) + (meta["cur_proc"].get("records") or [])
                    if r.get("source_type")
                }
            )
        )

        audit = {
            "solicitation": row.get("solicitation_id") or row.get("notice_id"),
            "title": (row.get("title") or "")[:120],
            "primary_23": is_primary,
            "identity_state": commercial.get("commercial_identity_state"),
            "manufacturer": commercial.get("manufacturer"),
            "model": commercial.get("model"),
            "mpn": commercial.get("mpn"),
            "agency": row.get("agency") or row.get("department"),
            "buyer": row.get("agency") or row.get("department"),
            "quantity": screen.get("quantity"),
            "sources_tried": sources_tried,
            "history_queries_l25": (meta["hist_proc"].get("queries") or [])[:8],
            "current_queries_l25": (meta["cur_proc"].get("queries") or [])[:8],
            "history_result": history.get("history_research_state"),
            "history_unit": joined.get("history_unit"),
            "history_source": history.get("source_type"),
            "history_confidence": joined.get("history_confidence_l24"),
            "new_history_hit": bool(
                history.get("l25_history_source") and history.get("historical_award_unit_price") is not None
            ),
            "price_result": market.get("market_research_state"),
            "current_unit": joined.get("current_unit"),
            "price_access": joined.get("price_access"),
            "acquisition_price_type": joined.get("acquisition_price_type"),
            "new_current_hit": bool(str(market.get("selection_reason") or "").startswith("l25:")),
            "price_url": (joined.get("acquisition") or {}).get("public_retail_source")
            or market.get("public_retail_source"),
            "freight": joined.get("freight"),
            "unit_spread": (joined.get("unit_economics") or {}).get("unit_raw_spread"),
            "expected_net": joined.get("expected_net_profit"),
            "economics_completed": joined.get("economics_completed"),
            "convergence_state": state,
            "queue": joined.get("queue"),
            "codes": joined.get("codes"),
            "ready_to_bid": False,
        }
        candidate_rows.append(audit)
        if is_primary:
            original_23_audits.append(audit)

        q = joined.get("queue") or QUEUE_INCOMPLETE
        if q in queues:
            queues[q].append(audit)
        else:
            queues[QUEUE_INCOMPLETE].append(audit)
        net = joined.get("expected_net_profit")
        if isinstance(net, (int, float)) and net > 0:
            if net >= 25000:
                queues["PROFITABLE_GE_25K"].append(audit)
            if net >= 50000:
                queues["PROFITABLE_GE_50K"].append(audit)

    save_cache(cache)

    # Recurring-buy intelligence from what we found
    events = events_from_candidate_audits(candidate_rows)
    recurring = prioritize_recurring(aggregate_recurring_purchases(events))
    buyers = aggregate_buyer_watchlist(events)

    researched = int(funnel.get("researched", 0)) or 1
    profitable = int(funnel.get("positive_total_profit", 0))
    access_ok = sum(
        1
        for a in candidate_rows
        if a.get("price_access") in {None, "YES"} or a.get("current_unit") is not None
    )
    # Bid-accessible profitable rate: live + access yes + positive economics
    bid_accessible_profitable = sum(
        1
        for a in candidate_rows
        if isinstance(a.get("expected_net"), (int, float))
        and a["expected_net"] > 0
        and (a.get("price_access") in {"YES", None} or a.get("current_unit") is not None)
        and a.get("economics_completed")
    )
    rate = round(bid_accessible_profitable / researched, 4)

    # Funnel math toward 80–100 profitable
    funnel_math = {
        "live_access_yes_input": len([r for r in rows if str(r.get("our_bid_access") or "") == "YES"]),
        "fully_researched": int(funnel.get("researched", 0)),
        "history_found": int(funnel.get("history_found", 0)),
        "current_usable": int(funnel.get("current_price_found", 0)),
        "both_found": int(funnel.get("both_found", 0)),
        "complete_economics": int(funnel.get("economics_completed", 0)),
        "profitable": profitable,
        "projected_live_for_100_profitable": (
            int(round(100 / (profitable / researched))) if profitable > 0 else None
        ),
    }

    # Success recoveries / failures for audit
    successes = [
        a
        for a in candidate_rows
        if a.get("new_history_hit") or a.get("new_current_hit") or a.get("both_found" if False else None)
        or (a.get("history_unit") is not None and a.get("primary_23"))
        or (a.get("current_unit") is not None)
    ]
    # clearer success = source recovery of history or current
    source_successes = [
        a for a in candidate_rows if a.get("new_history_hit") or a.get("new_current_hit") or a.get("history_unit") or a.get("current_unit")
    ]
    failures = [
        a
        for a in original_23_audits
        if a.get("history_unit") is None and a.get("current_unit") is None
    ]

    primary_n = len(original_23_audits) or 1
    verdict_bits = {
        "primary_history": int(funnel.get("primary_history_found", 0)),
        "primary_current": int(funnel.get("primary_current_found", 0)),
        "primary_both": int(funnel.get("primary_both_found", 0)),
        "positive_econ": int(funnel.get("positive_total_profit", 0)),
        "source_coverage": sum(1 for v in telemetry.values() if v.get("exact_identity_hits") or v.get("historical_prices_found") or v.get("current_prices_found")),
    }
    if (
        verdict_bits["primary_both"] >= 5
        and verdict_bits["primary_history"] >= 8
        and verdict_bits["primary_current"] >= 8
        and verdict_bits["positive_econ"] >= 3
    ):
        verdict = "PHASE_L25_SOURCE_EXPANSION_WORKING"
    elif (
        verdict_bits["source_coverage"] >= 2
        or verdict_bits["primary_history"] >= 1
        or verdict_bits["primary_current"] >= 1
        or int(funnel.get("history_found", 0)) > 2
        or int(funnel.get("current_price_found", 0)) > 0
    ):
        verdict = "PHASE_L25_PARTIAL_SOURCE_EXPANSION"
    else:
        verdict = "PHASE_L25_SOURCE_EXPANSION_FAILED"

    payload = {
        "kind": "PhaseL25SourceExpansionResult",
        "phase": "L.2.5",
        "build": "20260927-m3-phase-l25-source-expansion",
        "generated_at": _utc(),
        "verdict": verdict,
        "budget_used": {
            "usaspending": budget["usaspending"],
            "market": budget["market"],
            "procurement": budget["procurement"],
            "procurement_fetch": budget["procurement_fetch"],
        },
        "funnel": dict(funnel),
        "funnel_math": funnel_math,
        "convergence_states": dict(states),
        "code_counts": code_counts.most_common(30),
        "source_telemetry": telemetry,
        "candidate_table": candidate_rows,
        "original_23": original_23_audits,
        "original_23_counts": {
            "n": len(original_23_audits),
            "history_found": int(funnel.get("primary_history_found", 0)),
            "current_price_found": int(funnel.get("primary_current_found", 0)),
            "both_found": int(funnel.get("primary_both_found", 0)),
            "positive_economics": sum(
                1
                for a in original_23_audits
                if isinstance(a.get("expected_net"), (int, float)) and a["expected_net"] > 0
            ),
        },
        "queues": {k: v for k, v in queues.items()},
        "queue_counts": {k: len(v) for k, v in queues.items()},
        "counts": {
            "researched": int(funnel.get("researched", 0)),
            "history_found": int(funnel.get("history_found", 0)),
            "current_price_found": int(funnel.get("current_price_found", 0)),
            "both_found": int(funnel.get("both_found", 0)),
            "positive_unit_spread": int(funnel.get("positive_unit_spread", 0)),
            "positive_total_profit": int(funnel.get("positive_total_profit", 0)),
            "economics_completed": int(funnel.get("economics_completed", 0)),
            "net_ge_10k": int(funnel.get("net_ge_10k", 0)),
            "target_researched": target_n,
        },
        "profitable_bid_accessible_rate": {
            "researched": researched,
            "bid_accessible_profitable": bid_accessible_profitable,
            "rate": rate,
            "access_ok_proxy": access_ok,
        },
        "recurring_buy": {
            "products_tracked": len(recurring),
            "repeat_buy_candidates": [
                w for w in recurring if int(w.get("purchase_count") or 0) >= 3
            ],
            "watches": recurring[:40],
        },
        "buyer_watchlist": buyers[:40],
        "audit_samples": {
            "source_successes": source_successes[:15],
            "failures": failures[:15],
        },
        "ready_to_bid": False,
        "stop": True,
        "phase_m_started": False,
    }

    _write("l25_source_expansion.json", payload)
    _write(
        "l25_summary.json",
        {
            "verdict": verdict,
            "original_23_counts": payload["original_23_counts"],
            "counts": payload["counts"],
            "funnel_math": funnel_math,
            "source_telemetry_nonzero": {
                k: v for k, v in telemetry.items() if any(v.values())
            },
            "queue_counts": payload["queue_counts"],
            "profitable_bid_accessible_rate": payload["profitable_bid_accessible_rate"],
            "recurring_buy": {
                "products_tracked": len(recurring),
                "repeat_buy_candidates": len(payload["recurring_buy"]["repeat_buy_candidates"]),
            },
            "buyer_watchlist_n": len(buyers),
        },
    )
    return payload

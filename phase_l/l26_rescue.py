"""Phase L.2.6 — progressive funnel live rescue + before/after comparison."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.convergence import join_history_and_price
from phase_l.enrichment import (
    load_cache,
    lookup_current_market,
    lookup_government_history,
    save_cache,
)
from phase_l.l24_rescue import _collect_eligible as l24_collect_eligible
from phase_l.progressive_funnel import (
    DEEP_RESEARCH_HIGH,
    DEEP_RESEARCH_MEDIUM,
    KEEP,
    LOW_PRIORITY,
    PROMISING,
    QUEUE_BID_CANDIDATE,
    QUEUE_BROAD,
    QUEUE_DEEP_IN_PROGRESS,
    QUEUE_ECON_RECON,
    QUEUE_PROFITABLE_COMPLIANCE,
    QUEUE_PROMISING_DEEP,
    load_product_memory,
    recall_product,
    remember_product,
    run_progressive_stages_cheap,
    save_product_memory,
    should_deep_research,
    stage5_final_economic_gate,
    stage6_prebid_gate,
)
from phase_l.recurring_buy import (
    aggregate_recurring_purchases,
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


def _recurring_boost_map(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Build product-key → boost from any prior history fields on rows."""
    events = []
    for r in rows:
        if r.get("historical_award_unit_price") is None and r.get("history_unit") is None:
            continue
        events.append(
            {
                "manufacturer": r.get("manufacturer"),
                "model": r.get("model"),
                "mpn": r.get("mpn") or r.get("nsn"),
                "buyer": r.get("agency") or r.get("department"),
                "unit_price": r.get("historical_award_unit_price") or r.get("history_unit"),
                "quantity": r.get("quantity"),
            }
        )
    watches = prioritize_recurring(aggregate_recurring_purchases(events))
    out: dict[str, float] = {}
    for w in watches:
        key = w.get("product_key")
        if not key:
            continue
        boost = 5.0
        if int(w.get("purchase_count") or 0) >= 3:
            boost += 25.0
        if (w.get("historical_margin_estimate") or 0) > 0:
            boost += 15.0
        out[key] = boost
    return out


def run_phase_l26_progressive_funnel(
    rows: list[dict[str, Any]],
    *,
    authorize_live: bool = True,
    usaspending_max: int = 40,
    market_max: int = 60,
    deep_limit: int = 40,
) -> dict[str, Any]:
    """Restore progressive funnel; deep spend only on HIGH/MEDIUM recon candidates."""
    cache = load_cache()
    memory = load_product_memory()
    budget = {
        "usaspending": 0,
        "usaspending_max": usaspending_max,
        "market": 0,
        "market_max": market_max,
    }

    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    # Before: L.2.4/L.2.5 narrow eligible set
    before_eligible = l24_collect_eligible(access_yes)

    boost_map = _recurring_boost_map(access_yes)

    funnel = Counter()
    funnel["raw_live_access_yes"] = len(access_yes)
    funnel["before_l24_eligible"] = len(before_eligible)

    triage_counts: Counter = Counter()
    deep_priority_counts: Counter = Counter()
    queues: dict[str, list] = {
        QUEUE_BROAD: [],
        QUEUE_ECON_RECON: [],
        QUEUE_PROMISING_DEEP: [],
        QUEUE_DEEP_IN_PROGRESS: [],
        QUEUE_PROFITABLE_COMPLIANCE: [],
        QUEUE_BID_CANDIDATE: [],
    }

    stage3_rows: list[dict[str, Any]] = []
    deep_candidates: list[dict[str, Any]] = []
    rescued_to_stage3: list[dict[str, Any]] = []
    cost_by_stage: Counter = Counter()

    # --- Stages 0–3 on all access-YES ---
    for i, row in enumerate(access_yes):
        if i and i % 50 == 0:
            print(f"[l26] cheap {i}/{len(access_yes)} stage3={funnel['stage3_pass']}", flush=True)

        result = run_progressive_stages_cheap(
            row,
            memory=memory,
            recurring_boost=0.0,  # filled after key known
        )
        # Re-run boost if we have a product key
        key = result.get("product_key")
        if key and key in boost_map and result.get("stage3"):
            # adjust score lightly for reporting
            result["stage3"]["research_priority_score"] = int(
                result["stage3"].get("research_priority_score") or 0
            ) + int(boost_map[key])
            if result["stage3"]["research_priority_score"] >= 70:
                result["stage3"]["deep_research_priority"] = DEEP_RESEARCH_HIGH
                result["deep_research_priority"] = DEEP_RESEARCH_HIGH
            elif result["stage3"]["research_priority_score"] >= 40:
                result["stage3"]["deep_research_priority"] = DEEP_RESEARCH_MEDIUM
                result["deep_research_priority"] = DEEP_RESEARCH_MEDIUM

        s1 = result.get("stage1") or {}
        triage_counts[s1.get("triage_state") or "NONE"] += 1
        if result.get("reached_stage", 0) >= 0 and (result.get("stage0") or {}).get("pass"):
            funnel["stage0_pass"] += 1
        if s1.get("pass"):
            funnel["stage1_keep_promising"] += 1
            if s1.get("triage_state") in {KEEP, PROMISING, LOW_PRIORITY}:
                queues[QUEUE_BROAD].append(
                    {
                        "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                        "title": (row.get("title") or "")[:100],
                        "triage": s1.get("triage_state"),
                    }
                )
        if (result.get("stage2") or {}).get("pass"):
            funnel["stage2_identity"] += 1
        if result.get("survives_to_stage3"):
            funnel["stage3_pass"] += 1
            s3 = result["stage3"]
            deep_priority_counts[s3.get("deep_research_priority") or "NONE"] += 1
            if s3.get("promising_economics"):
                funnel["stage3_promising_economics"] += 1
            audit = {
                "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                "title": (row.get("title") or "")[:120],
                "triage": s1.get("triage_state"),
                "identity_anchors": (result.get("stage2") or {}).get("identity_anchors"),
                "identity_confidence": (result.get("stage2") or {}).get("identity_confidence"),
                "market_research_eligible_l23": (result.get("stage2") or {}).get("market_research_eligible"),
                "missing_fields": [],
                "why_researchable": (result.get("stage2") or {}).get("reason"),
                "historical_range": s3.get("historical_range"),
                "acquisition_range": s3.get("acquisition_range"),
                "spread_range": s3.get("spread_range"),
                "signals": s3.get("signals"),
                "deep_priority": s3.get("deep_research_priority"),
                "score": s3.get("research_priority_score"),
                "was_l24_eligible": False,
            }
            screen = result.get("stage2_screen") or {}
            if screen.get("quantity") is None:
                audit["missing_fields"].append("quantity")
            if s3.get("freight", {}).get("freight_unresolved"):
                audit["missing_fields"].append("freight")
            if not (result.get("stage2") or {}).get("market_research_eligible"):
                audit["missing_fields"].append("l23_market_research_eligible")
                rescued_to_stage3.append(audit)
            anchors = (result.get("stage2") or {}).get("identity_anchors") or []
            if "mpn" not in anchors and (
                "commercial_model" in anchors or "manufacturer_model" in anchors or "commercial_state" in anchors
            ):
                if not any(x.get("solicitation") == audit.get("solicitation") for x in rescued_to_stage3):
                    rescued_to_stage3.append(audit)

            stage3_rows.append({**audit, "_row": row, "_result": result})
            if should_deep_research(s3):
                funnel["deep_queue"] += 1
                deep_candidates.append({**audit, "_row": row, "_result": result})
                queues[QUEUE_PROMISING_DEEP].append({k: v for k, v in audit.items()})
            elif s3.get("pass"):
                queues[QUEUE_ECON_RECON].append({k: v for k, v in audit.items()})

        cost = result.get("cost") or {}
        cost_by_stage[int(cost.get("stage") or result.get("reached_stage") or 0)] += float(
            cost.get("estimated_cost_units") or 0
        )

    # Mark which stage3 were also L24 eligible
    before_ids = {
        str(t[0].get("solicitation_id") or t[0].get("notice_id") or "")
        for t in before_eligible
    }
    for a in stage3_rows:
        sid = str(a.get("solicitation") or "")
        a["was_l24_eligible"] = sid in before_ids

    # --- Stage 4 deep research (budgeted) ---
    deep_candidates.sort(key=lambda a: -int(a.get("score") or 0))
    deep_work = deep_candidates[:deep_limit]
    deep_results: list[dict[str, Any]] = []

    for i, item in enumerate(deep_work):
        row = item["_row"]
        result = item["_result"]
        screen = result.get("stage2_screen") or {}
        commercial = (result.get("stage2") or {})
        # commercial dict in stage2 export may lack nested commercial — recover from screen
        commercial_full = screen.get("commercial_identity") or {}
        identity = screen.get("identity") or {}
        title = (row.get("title") or "")[:60]
        print(
            f"[l26] deep {i+1}/{len(deep_work)} "
            f"hist={budget['usaspending']}/{budget['usaspending_max']} "
            f"mkt={budget['market']}/{budget['market_max']} {title}",
            flush=True,
        )
        funnel["deep_researched"] += 1
        queues[QUEUE_DEEP_IN_PROGRESS].append(
            {"solicitation": item.get("solicitation"), "title": item.get("title")}
        )

        history = _with_timeout(
            lambda: lookup_government_history(
                row, identity, budget=budget, cache=cache, authorize_live=authorize_live
            ),
            40.0,
            {
                "historical_award_unit_price": None,
                "history_research_state": "HISTORY_PENDING",
                "attempted": True,
                "skipped": "timeout",
            },
        )
        key = result.get("product_key")
        mem = recall_product(memory, key)
        if mem and history.get("historical_award_unit_price") is None and mem.get("historical_unit_price"):
            history["historical_award_unit_price"] = float(mem["historical_unit_price"])
            history["history_research_state"] = "HISTORY_FOUND"
            history["history_confidence"] = "STRONG_COMPARABLE"
            history["source_type"] = "KNOWN_PRODUCT_MEMORY"
            history["attempted"] = True
            funnel["memory_history_reuse"] += 1

        market = _with_timeout(
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
        if mem and market.get("public_retail_unit_price") is None and mem.get("public_retail_unit_price"):
            market["public_retail_unit_price"] = float(mem["public_retail_unit_price"])
            market["economics_eligible"] = True
            market["public_retail_source"] = mem.get("source_url") or "known_product_memory"
            market["l22_confidence"] = "STRONG_VERIFIED"
            market["market_research_state"] = "MARKET_PRICE_FOUND"
            funnel["memory_price_reuse"] += 1

        stage5 = stage5_final_economic_gate(
            row=row,
            history=history,
            market=market,
            commercial=commercial_full,
            quantity=screen.get("quantity"),
        )
        joined = stage5["joined"]
        if stage5.get("verified_positive"):
            funnel["verified_positive_economics"] += 1
            net = stage5.get("expected_net_profit")
            if isinstance(net, (int, float)) and net >= 10000:
                funnel["verified_ge_10k"] += 1
        if stage5.get("promising") or (result.get("stage3") or {}).get("promising_economics"):
            pass
        if joined.get("unit_economics", {}).get("unit_raw_spread"):
            if joined["unit_economics"]["unit_raw_spread"] > 0:
                funnel["deep_positive_unit_spread"] += 1

        stage6 = stage6_prebid_gate(row, stage5=stage5)
        if stage6.get("ready_to_bid"):
            funnel["ready_to_bid"] += 1
            queues[QUEUE_BID_CANDIDATE].append(item)
        elif stage5.get("verified_positive"):
            queues[QUEUE_PROFITABLE_COMPLIANCE].append(item)

        # Update memory
        remember_product(
            memory,
            key,
            {
                "historical_unit_price": joined.get("history_unit") or history.get("historical_award_unit_price"),
                "public_retail_unit_price": joined.get("current_unit") or market.get("public_retail_unit_price"),
                "manufacturer": commercial_full.get("manufacturer"),
                "model": commercial_full.get("model"),
                "mpn": commercial_full.get("mpn") or identity.get("mpn"),
                "source_url": market.get("public_retail_source"),
            },
        )

        deep_results.append(
            {
                "solicitation": item.get("solicitation"),
                "title": item.get("title"),
                "deep_priority": item.get("deep_priority"),
                "score": item.get("score"),
                "history_unit": joined.get("history_unit"),
                "current_unit": joined.get("current_unit"),
                "convergence_state": joined.get("convergence_state"),
                "expected_net": stage5.get("expected_net_profit"),
                "profit_band": stage5.get("profit_band"),
                "verified_positive": stage5.get("verified_positive"),
                "economics_completed": stage5.get("economics_completed"),
                "ready_to_bid": False,  # force STOP
                "codes": joined.get("codes"),
                "signals_stage3": item.get("signals"),
                "missing_fields": item.get("missing_fields"),
            }
        )

    save_cache(cache)
    save_product_memory(memory)

    # Strip heavy objects from stage3 audit export
    stage3_export = []
    for a in stage3_rows:
        stage3_export.append({k: v for k, v in a.items() if not k.startswith("_")})

    # Dedupe rescued list
    seen = set()
    rescued_unique = []
    for a in rescued_to_stage3:
        sid = a.get("solicitation") or a.get("title")
        if sid in seen:
            continue
        seen.add(sid)
        rescued_unique.append({k: v for k, v in a.items() if not k.startswith("_")})

    before_s3 = int(funnel.get("before_l24_eligible") or 0) or 1
    after_s3 = int(funnel.get("stage3_pass") or 0)
    ratio = round(after_s3 / before_s3, 2)

    if (
        after_s3 >= before_s3 * 2
        and int(funnel.get("deep_queue") or 0) >= max(10, before_s3 // 2)
        and int(funnel.get("ready_to_bid") or 0) == 0
    ):
        verdict = "PHASE_L26_PROGRESSIVE_FUNNEL_RESTORED"
    elif after_s3 > before_s3 and int(funnel.get("stage3_pass") or 0) >= 10:
        verdict = "PHASE_L26_PARTIAL_FUNNEL_RESTORATION"
    else:
        verdict = "PHASE_L26_FUNNEL_RESTORATION_FAILED"

    payload = {
        "kind": "PhaseL26ProgressiveFunnelResult",
        "phase": "L.2.6",
        "build": "20260927-m3-phase-l26-progressive-funnel",
        "generated_at": _utc(),
        "verdict": verdict,
        "before_after": {
            "raw_live_access_yes": len(access_yes),
            "before_l24_l25_eligible": len(before_eligible),
            "after_stage1_keep_promising": int(funnel.get("stage1_keep_promising") or 0),
            "after_stage2_identity": int(funnel.get("stage2_identity") or 0),
            "after_stage3_recon": after_s3,
            "stage3_expansion_ratio_vs_l24": ratio,
            "deep_research_queue": int(funnel.get("deep_queue") or 0),
            "deep_researched": int(funnel.get("deep_researched") or 0),
            "promising_apparent_economics": int(funnel.get("stage3_promising_economics") or 0),
            "verified_positive_economics": int(funnel.get("verified_positive_economics") or 0),
            "verified_ge_10k": int(funnel.get("verified_ge_10k") or 0),
            "ready_to_bid": 0,
        },
        "funnel": dict(funnel),
        "triage_counts": dict(triage_counts),
        "deep_priority_counts": dict(deep_priority_counts),
        "cost_by_stage_units": dict(cost_by_stage),
        "budget_used": {
            "usaspending": budget["usaspending"],
            "market": budget["market"],
        },
        "queues": {k: v[:80] for k, v in queues.items()},
        "queue_counts": {k: len(v) for k, v in queues.items()},
        "stage3_candidates": stage3_export[:200],
        "rescued_to_stage3_sample": rescued_unique[:40],
        "deep_results": deep_results,
        "ready_to_bid": False,
        "stop": True,
        "phase_m_started": False,
    }
    _write("l26_progressive_funnel.json", payload)
    _write(
        "l26_summary.json",
        {
            "verdict": verdict,
            "before_after": payload["before_after"],
            "triage_counts": dict(triage_counts),
            "deep_priority_counts": dict(deep_priority_counts),
            "queue_counts": payload["queue_counts"],
            "cost_by_stage_units": dict(cost_by_stage),
            "budget_used": payload["budget_used"],
            "rescued_sample_n": len(rescued_unique),
        },
    )
    return payload

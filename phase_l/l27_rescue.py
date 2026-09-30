"""Phase L.2.7 — resilient acquisition pricing across Stage 3 candidates."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from application_clock import now_utc
from phase_l.acquisition_pricing import (
    SOURCE_LEARNING_PATH,
    PRICE_MEMORY_PATH,
    STRONG_PRICE_LEAD_REQUIRES_VERIFICATION,
    load_json,
    preliminary_bid_window,
    research_acquisition_price,
    save_json,
)
from phase_l.enrichment import load_cache, lookup_government_history, save_cache
from phase_l.progressive_funnel import (
    DEEP_RESEARCH_HIGH,
    DEEP_RESEARCH_MEDIUM,
    run_progressive_stages_cheap,
)
from phase_l.resilient_fetch import DomainCircuitBreaker


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

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"


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


def run_phase_l27_resilient_pricing(
    rows: list[dict[str, Any]],
    *,
    authorize_live: bool = True,
    max_fetches_per_candidate: int = 6,
    usaspending_max: int = 40,
    allow_bing_fallback: bool = False,
) -> dict[str, Any]:
    """Research acquisition pricing for all Stage 3 candidates (not just old 23)."""
    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    cache = load_cache()
    learning = load_json(SOURCE_LEARNING_PATH)
    memory = load_json(PRICE_MEMORY_PATH)
    breaker = DomainCircuitBreaker(fail_threshold=3)
    hist_budget = {"usaspending": 0, "usaspending_max": usaspending_max}

    funnel: Counter = Counter()
    funnel["raw_access_yes"] = len(access_yes)
    domain_stats: dict[str, Counter] = defaultdict(Counter)
    failure_taxonomy: Counter = Counter()

    stage3: list[dict[str, Any]] = []
    # Cheap progressive pass → Stage 3 pool
    for i, row in enumerate(access_yes):
        if i and i % 100 == 0:
            print(f"[l27] stage3-scan {i}/{len(access_yes)} n={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if not pipe.get("survives_to_stage3"):
            continue
        stage3.append({"row": row, "pipe": pipe})
    funnel["stage3_candidates"] = len(stage3)

    # Priority order for research
    def _prio(item: dict[str, Any]) -> tuple:
        pipe = item["pipe"]
        s2 = pipe.get("stage2") or {}
        s3 = pipe.get("stage3") or {}
        commercial = (pipe.get("stage2_screen") or {}).get("commercial_identity") or {}
        score = int(s3.get("research_priority_score") or 0)
        if commercial.get("commercial_priceability") == "HIGH":
            score += 40
        if (s3.get("historical_range") or {}).get("low"):
            score += 30
        if s2.get("market_research_eligible"):
            score += 10
        anchors = s2.get("identity_anchors") or []
        if "commercial_model" in anchors or "manufacturer_model" in anchors:
            score += 20
        if "mpn" in anchors:
            score += 15
        return (-score,)

    stage3.sort(key=_prio)

    results: list[dict[str, Any]] = []
    manual_queue: list[dict[str, Any]] = []
    deep_escalations: list[dict[str, Any]] = []

    total_fetches = 0
    total_fetch_ok = 0
    total_leads = 0
    total_strong = 0
    total_verified = 0
    total_usable = 0
    both_found = 0
    apparent_positive = 0
    verified_positive = 0
    ge_10k = 0
    history_found = 0
    cost_units = 0.0

    for i, item in enumerate(stage3):
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = screen.get("commercial_identity") or {}
        identity = screen.get("identity") or {}
        title = (row.get("title") or "")[:60]
        print(
            f"[l27] price {i+1}/{len(stage3)} "
            f"fetch={breaker.telemetry().get('skipped') and len(breaker.skipped)} "
            f"{title}",
            flush=True,
        )
        funnel["price_searches"] += 1

        # History (bounded)
        history = _call_timeout(
            lambda: lookup_government_history(
                row,
                identity,
                budget=hist_budget,
                cache=cache,
                authorize_live=authorize_live,
            ),
            25.0,
            {"historical_award_unit_price": None, "attempted": True, "skipped": "timeout"},
        )
        hist_u = _f(history.get("historical_award_unit_price"))
        if hist_u is not None:
            history_found += 1
            funnel["history_found"] += 1

        acq = _call_timeout(
            lambda: research_acquisition_price(
                row=row,
                commercial=commercial,
                identity=identity,
                breaker=breaker,
                learning=learning,
                memory=memory,
                max_fetches=max_fetches_per_candidate,
                allow_bing_fallback=allow_bing_fallback and authorize_live,
            ),
            55.0,
            {
                "kind": "PhaseL27AcquisitionResearch",
                "leads": [],
                "verified": [],
                "usable": [],
                "acquisition_range": None,
                "failure_class": "FETCH_TIMEOUT",
                "failure_classes": ["FETCH_TIMEOUT"],
                "telemetry": {"fetches": 0, "fetch_ok": 0, "leads": 0, "strong_leads": 0, "verified_prices": 0, "usable_prices": 0, "bot_blocks": 0, "timeouts": 1},
                "fetch_log": [],
                "manual_fallback": False,
            },
        )
        tele = acq.get("telemetry") or {}
        total_fetches += int(tele.get("fetches") or 0)
        total_fetch_ok += int(tele.get("fetch_ok") or 0)
        total_leads += int(tele.get("leads") or 0)
        total_strong += int(tele.get("strong_leads") or 0)
        total_verified += int(tele.get("verified_prices") or 0)
        total_usable += int(tele.get("usable_prices") or 0)
        cost_units += float(tele.get("fetches") or 0) * 2 + 3  # relative

        for fl in acq.get("fetch_log") or []:
            d = fl.get("domain") or "unknown"
            domain_stats[d]["attempts"] += 1
            st = fl.get("status") or ""
            if st == "FETCH_OK":
                domain_stats[d]["success"] += 1
            if st in {"FETCH_403", "FETCH_429", "FETCH_BOT_BLOCKED"}:
                domain_stats[d]["blocks"] += 1
            if st == "FETCH_TIMEOUT":
                domain_stats[d]["timeouts"] += 1

        if acq.get("failure_class"):
            failure_taxonomy[acq["failure_class"]] += 1

        usable = acq.get("usable") or []
        verified = acq.get("verified") or []
        leads = acq.get("leads") or []
        arange = acq.get("acquisition_range")
        acq_med = (arange or {}).get("median")
        acq_lo = (arange or {}).get("low")
        acq_hi = (arange or {}).get("high")
        # Prefer usable verified for economics
        current_unit = usable[0]["verified_price"] if usable else None
        current_verified = verified[0]["verified_price"] if verified else None

        # Apparent economics using ranges / leads for Stage 3 ranking
        hist_lo = hist_u
        hist_hi = hist_u
        hist_med = hist_u
        if hist_u is None:
            # leave empty — preliminary window needs hist
            pass

        bid_win = preliminary_bid_window(
            hist_low=hist_lo,
            hist_median=hist_med,
            hist_recent=hist_u,
            hist_high=hist_hi,
            acq_low=acq_lo or (_f(leads[0].get("apparent_price")) if leads else None),
            acq_median=acq_med or (_f(leads[0].get("apparent_price")) if leads else None),
            acq_high=acq_hi or (_f(leads[0].get("apparent_price")) if leads else None),
        )

        apparent = False
        if bid_win and (bid_win.get("median_spread") or 0) > 0:
            apparent = True
        if hist_u and current_unit and hist_u > current_unit:
            apparent = True
        if hist_u and not current_unit and acq_med and hist_u > acq_med:
            apparent = True
        if apparent:
            apparent_positive += 1
            funnel["apparent_positive_economics"] += 1

        both = hist_u is not None and current_unit is not None
        if both:
            both_found += 1
            funnel["both_found"] += 1
            # Verified positive unit economics (not full qty/freight finalize)
            if hist_u > current_unit:
                verified_positive += 1
                funnel["verified_positive_unit"] += 1
                qty = _f(screen.get("quantity")) or 1
                est_net = (hist_u - current_unit) * qty * 0.9  # rough
                if est_net >= 10000:
                    ge_10k += 1
                    funnel["ge_10k_est"] += 1

        # Manual fallback
        if acq.get("manual_fallback") and apparent:
            manual_queue.append(
                {
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "title": (row.get("title") or "")[:120],
                    "apparent_spread": (bid_win or {}).get("median_spread"),
                    "leads": leads[:3],
                    "reason": STRONG_PRICE_LEAD_REQUIRES_VERIFICATION,
                    "failure_class": acq.get("failure_class"),
                }
            )
            funnel["manual_fallback"] += 1

        # Deep escalation: economics-driven
        s3 = dict(pipe.get("stage3") or {})
        if apparent or usable or (leads and hist_u):
            s3["deep_research_priority"] = DEEP_RESEARCH_HIGH if (usable and hist_u) else DEEP_RESEARCH_MEDIUM
            s3["promising_economics"] = apparent or bool(usable and hist_u)
            deep_escalations.append(
                {
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "title": (row.get("title") or "")[:100],
                    "priority": s3["deep_research_priority"],
                    "hist": hist_u,
                    "acq": current_unit or acq_med,
                    "leads": len(leads),
                }
            )

        audit = {
            "solicitation": row.get("solicitation_id") or row.get("notice_id"),
            "title": (row.get("title") or "")[:120],
            "family": acq.get("family"),
            "identity_anchors": (pipe.get("stage2") or {}).get("identity_anchors"),
            "history_unit": hist_u,
            "verified_acq": current_verified,
            "usable_acq": current_unit,
            "acquisition_range": arange,
            "leads_n": len(leads),
            "strong_leads": sum(1 for L in leads if L.get("confidence") == "HIGH"),
            "failure_class": acq.get("failure_class"),
            "failure_classes": acq.get("failure_classes"),
            "preliminary_bid_window": bid_win,
            "apparent_positive": apparent,
            "both_found": both,
            "manual_fallback": acq.get("manual_fallback"),
            "telemetry": tele,
            "fetch_ok": tele.get("fetch_ok"),
            "bot_blocks": tele.get("bot_blocks"),
            "timeouts": tele.get("timeouts"),
        }
        results.append(audit)

    save_cache(cache)
    save_json(SOURCE_LEARNING_PATH, learning)
    save_json(PRICE_MEMORY_PATH, memory)

    researched = max(1, len(stage3))
    verified_prices = total_usable  # usable counts for "usable prices"
    cost_per_verified = round(cost_units / max(1, total_verified), 2)
    cost_per_positive = round(cost_units / max(1, apparent_positive), 2) if apparent_positive else None

    # Verdict
    if (
        total_fetches > 0
        and len(breaker.skipped) >= 0
        and total_leads >= 15
        and total_usable >= 8
        and both_found >= 5
    ):
        verdict = "PHASE_L27_RESILIENT_PRICING_WORKING"
    elif total_fetches > 0 and (total_leads >= 5 or total_verified >= 1 or total_fetch_ok >= 5):
        verdict = "PHASE_L27_PARTIAL_RESILIENT_PRICING"
    else:
        verdict = "PHASE_L27_RESILIENT_PRICING_FAILED"

    domain_table = {
        d: {
            "attempts": int(c["attempts"]),
            "success": int(c["success"]),
            "blocks": int(c["blocks"]),
            "timeouts": int(c["timeouts"]),
            "block_rate": round(c["blocks"] / max(1, c["attempts"]), 3),
        }
        for d, c in sorted(domain_stats.items(), key=lambda kv: -kv[1]["attempts"])[:40]
    }

    payload = {
        "kind": "PhaseL27ResilientPricingResult",
        "phase": "L.2.7",
        "build": "20260927-m3-phase-l27-resilient-acquisition-pricing",
        "generated_at": _utc(),
        "verdict": verdict,
        "funnel": {
            "stage3_candidates": len(stage3),
            "price_searches": int(funnel.get("price_searches") or 0),
            "price_leads": total_leads,
            "strong_leads": total_strong,
            "pages_attempted": total_fetches,
            "fetch_successes": total_fetch_ok,
            "bot_blocks": sum(int(c["blocks"]) for c in domain_stats.values()),
            "timeouts": sum(int(c["timeouts"]) for c in domain_stats.values()),
            "exact_product_pages": sum(int((r.get("telemetry") or {}).get("exact_product_pages") or 0) for r in results),
            "verified_prices": total_verified,
            "usable_prices": total_usable,
            "history_found": history_found,
            "both_found": both_found,
            "apparent_positive_economics": apparent_positive,
            "verified_positive_economics": verified_positive,
            "ge_10k": ge_10k,
            "manual_fallback": len(manual_queue),
            "deep_escalations": len(deep_escalations),
        },
        "failure_taxonomy": failure_taxonomy.most_common(20),
        "domain_performance": domain_table,
        "circuit_breaker": breaker.telemetry(),
        "cost": {
            "relative_units": round(cost_units, 1),
            "cost_per_verified_acquisition_price": cost_per_verified,
            "cost_per_positive_economic_candidate": cost_per_positive,
            "usaspending": hist_budget["usaspending"],
        },
        "manual_price_verification_priority": manual_queue[:40],
        "deep_escalations": deep_escalations[:60],
        "candidate_results": results,
        "ready_to_bid": False,
        "stop": True,
        "phase_m_started": False,
    }
    _write("l27_resilient_pricing.json", payload)
    _write(
        "l27_summary.json",
        {
            "verdict": verdict,
            "funnel": payload["funnel"],
            "failure_taxonomy": payload["failure_taxonomy"][:10],
            "domain_performance_top": dict(list(domain_table.items())[:12]),
            "circuit_breaker_skipped": breaker.telemetry().get("skipped"),
            "cost": payload["cost"],
            "manual_fallback_n": len(manual_queue),
        },
    )
    return payload

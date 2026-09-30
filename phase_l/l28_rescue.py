"""Phase L.2.8 — product-detail resolution + bot-resilient price recovery on all Stage 3."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from application_clock import now_utc
from phase_l.acquisition_pricing import (
    PRICE_MEMORY_PATH,
    QUEUE_MANUAL_PRICE,
    SOURCE_LEARNING_PATH,
    STRONG_PRICE_LEAD_REQUIRES_VERIFICATION,
    load_json,
    preliminary_bid_window,
    save_json,
)
from phase_l.enrichment import load_cache, lookup_government_history, save_cache
from phase_l.product_detail_resolution import (
    PROMISING_REQUIRES_PRICE_VERIFICATION,
    research_with_detail_resolution,
)
from phase_l.progressive_funnel import (
    DEEP_RESEARCH_HIGH,
    DEEP_RESEARCH_MEDIUM,
    run_progressive_stages_cheap,
    should_deep_research,
)
from phase_l.resilient_fetch import DomainCircuitBreaker

# Hard rule: process every Stage 3 survivor — no fixed 23/50/100 row caps.
STAGE3_NO_ROW_CAP = True
DEEP_RESEARCH_NO_FIXED_COUNT = True

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"


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


def ballpark_economics(
    *,
    hist_unit: float | None,
    acq_low: float | None,
    acq_high: float | None,
    lead_price: float | None,
) -> dict[str, Any] | None:
    """Stage 3 reconnaissance range — not a bid recommendation."""
    acq_lo = acq_low or lead_price
    acq_hi = acq_high or lead_price
    if hist_unit is None or acq_lo is None:
        return None
    acq_hi = acq_hi or acq_lo
    spread_lo = round(hist_unit - acq_hi, 2)
    spread_hi = round(hist_unit - acq_lo, 2)
    return {
        "kind": "STAGE3_BALLPARK",
        "historical": {"low": hist_unit, "high": hist_unit},
        "current_acq": {"low": acq_lo, "high": acq_hi},
        "apparent_spread": {"low": min(spread_lo, spread_hi), "high": max(spread_lo, spread_hi)},
        "status": PROMISING_REQUIRES_PRICE_VERIFICATION
        if (spread_hi > 0 or spread_lo > 0)
        else "RECON_NEGATIVE_OR_FLAT",
        "bid_recommendation": False,
    }


def run_phase_l28_product_detail_resolution(
    rows: list[dict[str, Any]],
    *,
    authorize_live: bool = True,
    max_shell_fetches: int = 4,
    max_detail_fetches: int = 4,
    usaspending_max: int = 40,
) -> dict[str, Any]:
    """Resolve search shells → product detail / static artifacts for ALL Stage 3 candidates."""
    assert STAGE3_NO_ROW_CAP is True
    assert DEEP_RESEARCH_NO_FIXED_COUNT is True

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
    for i, row in enumerate(access_yes):
        if i and i % 100 == 0:
            print(f"[l28] stage3-scan {i}/{len(access_yes)} n={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if not pipe.get("survives_to_stage3"):
            continue
        # Freight may flag FREIGHT_REQUIRED / FREIGHT_UNRESOLVED but must not drop Stage 3.
        s3 = pipe.get("stage3") or {}
        freight = s3.get("freight") or {}
        if freight.get("freight_unresolved"):
            funnel["freight_unresolved_but_stage3"] += 1
        stage3.append({"row": row, "pipe": pipe})
    funnel["stage3_candidates"] = len(stage3)

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
    total_shells = 0
    total_candidate_links = 0
    total_detail_pages = 0
    total_static = 0
    total_blocked = 0
    total_alt = 0
    total_detail_resolutions = 0
    both_found = 0
    history_joins = 0
    apparent_positive = 0
    verified_positive = 0
    ge_10k = 0
    history_found = 0
    cost_units = 0.0
    rows_with_detail_candidates = 0
    rows_with_strong_leads = 0

    for i, item in enumerate(stage3):
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = screen.get("commercial_identity") or {}
        identity = screen.get("identity") or {}
        title = (row.get("title") or "")[:60]
        print(f"[l28] resolve {i+1}/{len(stage3)} {title}", flush=True)
        funnel["price_searches"] += 1

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
        hist_is_approx = False
        if hist_u is None:
            # Stage 3 recon may carry approximate historical range — ballpark only
            hr = ((pipe.get("stage3") or {}).get("historical_range") or {})
            hist_u = _f(hr.get("high")) or _f(hr.get("low"))
            if hist_u is not None:
                hist_is_approx = True
                funnel["history_approx_recon"] += 1
        if history.get("historical_award_unit_price") is not None:
            history_found += 1
            funnel["history_found"] += 1

        acq = _call_timeout(
            lambda: research_with_detail_resolution(
                row=row,
                commercial=commercial,
                identity=identity,
                breaker=breaker,
                learning=learning,
                memory=memory,
                max_shell_fetches=max_shell_fetches,
                max_detail_fetches=max_detail_fetches,
            ),
            70.0,
            {
                "kind": "PhaseL28AcquisitionResearch",
                "leads": [],
                "verified": [],
                "usable": [],
                "acquisition_range": None,
                "failure_class": "FETCH_TIMEOUT",
                "telemetry": {
                    "shells": 0,
                    "candidate_links": 0,
                    "product_detail_pages": 0,
                    "static_artifacts": 0,
                    "blocked_domains": 0,
                    "alternate_attempts": 0,
                    "fetches": 0,
                    "fetch_ok": 0,
                    "leads": 0,
                    "strong_leads": 0,
                    "verified_prices": 0,
                    "usable_prices": 0,
                    "exact_product_pages": 0,
                },
                "fetch_log": [],
                "manual_fallback": False,
                "detail_resolutions": 0,
                "blocked_domains": [],
            },
        )
        tele = acq.get("telemetry") or {}
        total_fetches += int(tele.get("fetches") or 0)
        total_fetch_ok += int(tele.get("fetch_ok") or 0)
        total_leads += int(tele.get("leads") or 0)
        total_strong += int(tele.get("strong_leads") or 0)
        total_verified += int(tele.get("verified_prices") or 0)
        total_usable += int(tele.get("usable_prices") or 0)
        total_shells += int(tele.get("shells") or 0)
        total_candidate_links += int(tele.get("candidate_links") or 0)
        total_detail_pages += int(tele.get("product_detail_pages") or tele.get("exact_product_pages") or 0)
        total_static += int(tele.get("static_artifacts") or 0)
        total_blocked += int(tele.get("blocked_domains") or 0)
        total_alt += int(tele.get("alternate_attempts") or 0)
        total_detail_resolutions += int(acq.get("detail_resolutions") or 0)
        cost_units += float(tele.get("fetches") or 0) * 2 + 4

        if int(tele.get("candidate_links") or 0) > 0 or int(tele.get("exact_product_pages") or 0) > 0:
            rows_with_detail_candidates += 1
        if int(tele.get("strong_leads") or 0) > 0:
            rows_with_strong_leads += 1

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
        current_unit = usable[0]["verified_price"] if usable else None
        current_verified = verified[0]["verified_price"] if verified else None
        lead_price = None
        for L in leads:
            if L.get("apparent_price") and L.get("confidence") == "HIGH":
                lead_price = _f(L["apparent_price"])
                break
        if lead_price is None and leads:
            lead_price = _f(leads[0].get("apparent_price"))

        # Immediate history+price convergence when price appears
        ballpark = ballpark_economics(
            hist_unit=hist_u,
            acq_low=acq_lo or (_f(current_unit) if current_unit else None),
            acq_high=acq_hi,
            lead_price=lead_price,
        )
        if ballpark and hist_u is not None and (current_unit or lead_price):
            history_joins += 1
            funnel["history_price_joins"] += 1

        bid_win = preliminary_bid_window(
            hist_low=hist_u,
            hist_median=hist_u,
            hist_recent=hist_u,
            hist_high=hist_u,
            acq_low=acq_lo or lead_price,
            acq_median=acq_med or lead_price,
            acq_high=acq_hi or lead_price,
        )

        apparent = False
        if bid_win and (bid_win.get("median_spread") or 0) > 0:
            apparent = True
        if hist_u and current_unit and hist_u > current_unit:
            apparent = True
        if hist_u and not current_unit and (acq_med or lead_price) and hist_u > (acq_med or lead_price or 0):
            apparent = True
        if ballpark and ballpark.get("status") == PROMISING_REQUIRES_PRICE_VERIFICATION:
            apparent = True
        if apparent:
            apparent_positive += 1
            funnel["apparent_positive_economics"] += 1

        both = (history.get("historical_award_unit_price") is not None) and current_unit is not None
        if both:
            both_found += 1
            funnel["both_found"] += 1
            hv = _f(history.get("historical_award_unit_price"))
            if hv is not None and hv > current_unit:
                verified_positive += 1
                funnel["verified_positive_unit"] += 1
                qty = _f(screen.get("quantity")) or 1
                est_net = (hv - current_unit) * qty * 0.9
                if est_net >= 10000:
                    ge_10k += 1

        # Manual verification queue — strong identity + lead + auto verify failed
        if (acq.get("manual_fallback") or acq.get("promising_requires_verification")) and (
            apparent or leads
        ):
            manual_queue.append(
                {
                    "queue": QUEUE_MANUAL_PRICE,
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "title": (row.get("title") or "")[:120],
                    "product": commercial.get("model") or commercial.get("mpn") or identity.get("model"),
                    "seller": (leads[0].get("seller") if leads else None)
                    or ((acq.get("blocked_domains") or [None])[0]),
                    "apparent_price": lead_price,
                    "blocked_urls": acq.get("blocked_domains") or [],
                    "source_snippet": (leads[0].get("evidence_text") if leads else None),
                    "history_price": _f(history.get("historical_award_unit_price")) or hist_u,
                    "history_approx": hist_is_approx,
                    "apparent_spread": (bid_win or {}).get("median_spread")
                    or ((ballpark or {}).get("apparent_spread") or {}).get("high"),
                    "reason": STRONG_PRICE_LEAD_REQUIRES_VERIFICATION,
                    "failure_class": acq.get("failure_class"),
                    "ballpark": ballpark,
                }
            )
            funnel["manual_fallback"] += 1

        # Deep-research ALL promising (no fixed count)
        s3 = dict(pipe.get("stage3") or {})
        if apparent or usable or (leads and hist_u) or should_deep_research(s3):
            s3["deep_research_priority"] = (
                DEEP_RESEARCH_HIGH if (usable and hist_u) else DEEP_RESEARCH_MEDIUM
            )
            s3["promising_economics"] = apparent or bool(usable and hist_u)
            deep_escalations.append(
                {
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "title": (row.get("title") or "")[:100],
                    "priority": s3["deep_research_priority"],
                    "hist": hist_u,
                    "acq": current_unit or acq_med or lead_price,
                    "leads": len(leads),
                    "detail_pages": tele.get("exact_product_pages") or 0,
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
            "preliminary_bid_window": bid_win,
            "ballpark": ballpark,
            "apparent_positive": apparent,
            "both_found": both,
            "manual_fallback": bool(acq.get("manual_fallback") or acq.get("promising_requires_verification")),
            "telemetry": tele,
            "detail_resolutions": acq.get("detail_resolutions"),
            "blocked_domains": acq.get("blocked_domains"),
            "fetch_ok": tele.get("fetch_ok"),
            "candidate_links": tele.get("candidate_links"),
            "exact_product_pages": tele.get("exact_product_pages"),
            "static_artifacts": tele.get("static_artifacts"),
            "freight_unresolved": bool((s3.get("freight") or {}).get("freight_unresolved")),
        }
        results.append(audit)

    save_cache(cache)
    save_json(SOURCE_LEARNING_PATH, learning)
    save_json(PRICE_MEMORY_PATH, memory)

    # Verdict targets (diagnostic)
    if (
        rows_with_detail_candidates >= 30
        and rows_with_strong_leads >= 15
        and total_usable >= 8
        and history_joins >= 5
        and apparent_positive >= 10
    ):
        verdict = "PHASE_L28_PRODUCT_DETAIL_RECOVERY_WORKING"
    elif total_fetches > 0 and (
        rows_with_detail_candidates >= 5
        or total_leads >= 5
        or total_verified >= 1
        or total_alt >= 1
        or total_detail_resolutions >= 5
    ):
        verdict = "PHASE_L28_PARTIAL_PRODUCT_DETAIL_RECOVERY"
    else:
        verdict = "PHASE_L28_PRODUCT_DETAIL_RECOVERY_FAILED"

    domain_table = {
        d: {
            "attempts": int(c["attempts"]),
            "success": int(c["success"]),
            "blocks": int(c["blocks"]),
            "timeouts": int(c["timeouts"]),
            "block_rate": round(c["blocks"] / max(1, c["attempts"]), 3),
            "accessibility_score": round(
                (c["success"] / max(1, c["attempts"])) * (1.0 - c["blocks"] / max(1, c["attempts"])),
                3,
            ),
        }
        for d, c in sorted(domain_stats.items(), key=lambda kv: -kv[1]["attempts"])[:40]
    }

    payload = {
        "kind": "PhaseL28ProductDetailResolutionResult",
        "phase": "L.2.8",
        "build": "20260927-m3-phase-l28-product-detail-resolution",
        "generated_at": _utc(),
        "verdict": verdict,
        "stage3_no_row_cap": STAGE3_NO_ROW_CAP,
        "deep_research_no_fixed_count": DEEP_RESEARCH_NO_FIXED_COUNT,
        "funnel": {
            "stage3_candidates": len(stage3),
            "stage3_processed": len(results),
            "price_searches": int(funnel.get("price_searches") or 0),
            "search_shells": total_shells,
            "candidate_product_links": total_candidate_links,
            "product_detail_pages": total_detail_pages,
            "product_detail_resolutions": total_detail_resolutions,
            "rows_with_detail_candidates": rows_with_detail_candidates,
            "static_artifacts": total_static,
            "blocked_domain_events": total_blocked,
            "alternate_source_attempts": total_alt,
            "pages_attempted": total_fetches,
            "fetch_successes": total_fetch_ok,
            "bot_blocks": sum(int(c["blocks"]) for c in domain_stats.values()),
            "timeouts": sum(int(c["timeouts"]) for c in domain_stats.values()),
            "price_leads": total_leads,
            "strong_leads": total_strong,
            "rows_with_strong_leads": rows_with_strong_leads,
            "verified_prices": total_verified,
            "usable_prices": total_usable,
            "history_found": history_found,
            "history_price_joins": history_joins,
            "both_found": both_found,
            "apparent_positive_economics": apparent_positive,
            "verified_positive_economics": verified_positive,
            "ge_10k": ge_10k,
            "manual_fallback": len(manual_queue),
            "deep_escalations": len(deep_escalations),
            "freight_unresolved_but_stage3": int(funnel.get("freight_unresolved_but_stage3") or 0),
        },
        "failure_taxonomy": failure_taxonomy.most_common(20),
        "domain_performance": domain_table,
        "circuit_breaker": breaker.telemetry(),
        "cost": {
            "relative_units": round(cost_units, 1),
            "usaspending": hist_budget["usaspending"],
        },
        "manual_price_verification_priority": manual_queue[:80],
        "deep_escalations": deep_escalations,  # all promising — no fixed trim
        "candidate_results": results,
        "ready_to_bid": False,
        "stop": True,
        "phase_m_started": False,
    }
    _write("l28_product_detail_resolution.json", payload)
    _write(
        "l28_summary.json",
        {
            "verdict": verdict,
            "funnel": payload["funnel"],
            "failure_taxonomy": payload["failure_taxonomy"][:10],
            "domain_performance_top": dict(list(domain_table.items())[:12]),
            "circuit_breaker_skipped": breaker.telemetry().get("skipped"),
            "manual_fallback_n": len(manual_queue),
            "deep_escalations_n": len(deep_escalations),
        },
    )
    return payload

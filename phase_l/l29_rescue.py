"""Phase L.2.9 — maximum source coverage across ALL Stage 3 candidates (no row caps)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any

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
from phase_l.l28_rescue import ballpark_economics
from phase_l.maximum_source_coverage import research_maximum_sources
from phase_l.progressive_funnel import (
    DEEP_RESEARCH_HIGH,
    DEEP_RESEARCH_MEDIUM,
    run_progressive_stages_cheap,
    should_deep_research,
)
from phase_l.recurring_buy import (
    aggregate_buyer_watchlist,
    aggregate_recurring_purchases,
    events_from_candidate_audits,
    persist_known_product_economics,
    prioritize_recurring,
    reverse_live_hunt,
)
from phase_l.resilient_fetch import DomainCircuitBreaker
from phase_l.source_inventory import health_dashboard, load_health, save_health
from phase_l.source_roles import (
    CORROBORATED_STRONG_PRICE,
    DEEP_RESEARCH_NO_FIXED_COUNT,
    FREIGHT_NOT_YET_RESEARCHED,
    MANUAL_QUEUE_NO_FIXED_CAP,
    PRIMARY_SOURCE_BLOCKED,
    STAGE3_NO_ROW_CAP,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
KNOWN_PATH = ROOT / "data" / "phase_l29_known_product_economics.json"


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


def run_phase_l29_maximum_source_coverage(
    rows: list[dict[str, Any]],
    *,
    authorize_live: bool = True,
    max_shell_fetches: int = 3,
    max_detail_fetches: int = 3,
    usaspending_max: int = 50,
    parallel_branches: bool = True,
) -> dict[str, Any]:
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    cache = load_cache()
    learning = load_json(SOURCE_LEARNING_PATH)
    memory = load_json(PRICE_MEMORY_PATH)
    health = load_health()
    known = load_json(KNOWN_PATH)
    breaker = DomainCircuitBreaker(fail_threshold=3)
    hist_budget = {"usaspending": 0, "usaspending_max": usaspending_max}

    funnel: Counter = Counter()
    funnel["raw_access_yes"] = len(access_yes)
    failure_taxonomy: Counter = Counter()
    family_table: dict[str, Counter] = defaultdict(Counter)

    # Cheap progressive → ALL Stage 3
    stage3: list[dict[str, Any]] = []
    stage_counts = {"stage1": 0, "stage2": 0, "stage3": 0}
    for i, row in enumerate(access_yes):
        if i and i % 100 == 0:
            print(f"[l29] stage3-scan {i}/{len(access_yes)} n={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass") or pipe.get("survives_to_stage3"):
            stage_counts["stage2"] += 1
        if not pipe.get("survives_to_stage3"):
            continue
        s3 = pipe.get("stage3") or {}
        if (s3.get("freight") or {}).get("freight_unresolved"):
            funnel["freight_not_yet_researched"] += 1
        stage3.append({"row": row, "pipe": pipe})
    funnel["stage3_candidates"] = len(stage3)
    stage_counts["stage3"] = len(stage3)

    def _prio(item: dict[str, Any]) -> tuple:
        pipe = item["pipe"]
        s3 = pipe.get("stage3") or {}
        commercial = (pipe.get("stage2_screen") or {}).get("commercial_identity") or {}
        score = int(s3.get("research_priority_score") or 0)
        if commercial.get("commercial_priceability") == "HIGH":
            score += 40
        return (-score,)

    stage3.sort(key=_prio)

    results: list[dict[str, Any]] = []
    manual_queue: list[dict[str, Any]] = []
    deep_escalations: list[dict[str, Any]] = []

    totals = Counter()
    history_found = 0
    both_found = 0
    apparent_positive = 0
    verified_positive = 0
    ge_10k = ge_25k = ge_50k = 0
    history_joins = 0
    rows_with_price_evidence = 0

    for i, item in enumerate(stage3):
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = screen.get("commercial_identity") or {}
        identity = screen.get("identity") or {}
        title = (row.get("title") or "")[:55]
        print(f"[l29] cover {i+1}/{len(stage3)} {title}", flush=True)
        funnel["price_searches"] += 1

        def _hist():
            return lookup_government_history(
                row, identity, budget=hist_budget, cache=cache, authorize_live=authorize_live
            )

        research = _call_timeout(
            lambda: research_maximum_sources(
                row=row,
                commercial=commercial,
                identity=identity,
                breaker=breaker,
                learning=learning,
                memory=memory,
                health=health,
                hist_lookup=_hist,
                authorize_live=authorize_live,
                max_shell_fetches=max_shell_fetches,
                max_detail_fetches=max_detail_fetches,
                parallel=parallel_branches,
            ),
            90.0,
            {
                "kind": "PhaseL29MaximumSourceResearch",
                "history_branch": {"hist_unit": None, "family_hits": {}},
                "acquisition_branch": {
                    "leads": [],
                    "verified": [],
                    "usable": [],
                    "telemetry": {},
                    "failure_class": "FETCH_TIMEOUT",
                },
                "source_family_table": {},
                "both_found": False,
            },
        )

        for fam, counts in (research.get("source_family_table") or {}).items():
            for k, v in counts.items():
                family_table[fam][k] += int(v)

        hist_u = _f(research.get("hist_unit"))
        if hist_u is not None:
            history_found += 1
            funnel["history_found"] += 1

        acq = research.get("acquisition_branch") or {}
        usable = acq.get("usable") or []
        verified = acq.get("verified") or []
        leads = acq.get("leads") or []
        corroborated = acq.get("corroborated_leads") or []
        arange = acq.get("acquisition_range")
        current_unit = usable[0]["verified_price"] if usable else None
        lead_price = research.get("lead_price")
        tele = acq.get("telemetry") or {}

        totals["fetches"] += int(tele.get("fetches") or 0)
        totals["fetch_ok"] += int(tele.get("fetch_ok") or 0)
        totals["leads"] += len(leads)
        totals["strong_leads"] += sum(1 for L in leads if L.get("confidence") == "HIGH")
        totals["corroborated"] += len(corroborated)
        totals["verified"] += len(verified)
        totals["usable"] += len(usable)
        totals["blocked"] += int(tele.get("blocked_domains") or 0)
        totals["alt"] += int(tele.get("alternate_attempts") or 0)
        totals["static"] += int(tele.get("static_artifacts") or 0)
        totals["detail_res"] += int(research.get("acquisition_branch", {}).get("detail_resolutions") or 0)

        if leads or verified or usable:
            rows_with_price_evidence += 1

        if acq.get("failure_class"):
            failure_taxonomy[acq["failure_class"]] += 1

        ballpark = ballpark_economics(
            hist_unit=hist_u,
            acq_low=(arange or {}).get("low") or (_f(current_unit) if current_unit else None),
            acq_high=(arange or {}).get("high"),
            lead_price=lead_price,
        )
        if ballpark and hist_u is not None and (current_unit or lead_price):
            history_joins += 1

        bid_win = preliminary_bid_window(
            hist_low=hist_u,
            hist_median=hist_u,
            hist_recent=hist_u,
            hist_high=hist_u,
            acq_low=(arange or {}).get("low") or lead_price,
            acq_median=(arange or {}).get("median") or lead_price,
            acq_high=(arange or {}).get("high") or lead_price,
        )

        apparent = False
        if bid_win and (bid_win.get("median_spread") or 0) > 0:
            apparent = True
        if hist_u and current_unit and hist_u > current_unit:
            apparent = True
        if hist_u and lead_price and hist_u > lead_price:
            apparent = True
        if ballpark and "PROMISING" in str(ballpark.get("status") or ""):
            apparent = True
        if apparent:
            apparent_positive += 1

        both = bool(research.get("both_found"))
        if both:
            both_found += 1
        spread = research.get("joined_unit_spread")
        if both and current_unit and hist_u and hist_u > current_unit:
            verified_positive += 1
            qty = _f(screen.get("quantity")) or 1
            est = (hist_u - current_unit) * qty * 0.9
            if est >= 50000:
                ge_50k += 1
            elif est >= 25000:
                ge_25k += 1
            elif est >= 10000:
                ge_10k += 1
            # Persist known product economics
            pk = "|".join(
                str(x or "").upper()
                for x in (commercial.get("manufacturer"), commercial.get("model") or commercial.get("mpn"))
                if x
            )
            if pk and spread and spread > 0:
                persist_known_product_economics(
                    known,
                    product_key=pk,
                    hist_unit=hist_u,
                    acq_unit=current_unit,
                    source_urls=[usable[0].get("source_url")] if usable else None,
                    solicitation=row.get("solicitation_id") or row.get("notice_id"),
                )

        # Manual queue — NO fixed discard; rank later
        if (acq.get("manual_fallback") or acq.get("promising_requires_verification") or corroborated) and (
            leads or apparent
        ):
            manual_queue.append(
                {
                    "queue": QUEUE_MANUAL_PRICE,
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "title": (row.get("title") or "")[:120],
                    "product": commercial.get("model") or commercial.get("mpn"),
                    "apparent_price": lead_price,
                    "history_price": hist_u,
                    "apparent_spread": (bid_win or {}).get("median_spread")
                    or ((ballpark or {}).get("apparent_spread") or {}).get("high"),
                    "deadline": row.get("response_deadline") or row.get("due_date"),
                    "access": row.get("our_bid_access"),
                    "corroborated": bool(corroborated),
                    "blocked_domains": acq.get("blocked_domains"),
                    "snippet": (leads[0].get("evidence_text") if leads else None),
                    "reason": STRONG_PRICE_LEAD_REQUIRES_VERIFICATION,
                    "failure_class": acq.get("failure_class"),
                    "rank_score": (
                        float((bid_win or {}).get("median_spread") or 0)
                        + (5000 if corroborated else 0)
                        + (3000 if hist_u else 0)
                    ),
                }
            )

        s3 = dict(pipe.get("stage3") or {})
        if apparent or usable or (leads and hist_u) or should_deep_research(s3) or corroborated:
            s3["deep_research_priority"] = DEEP_RESEARCH_HIGH if (usable and hist_u) else DEEP_RESEARCH_MEDIUM
            deep_escalations.append(
                {
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "title": (row.get("title") or "")[:100],
                    "priority": s3["deep_research_priority"],
                    "hist": hist_u,
                    "acq": current_unit or lead_price,
                    "leads": len(leads),
                    "corroborated": len(corroborated),
                }
            )

        results.append(
            {
                "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                "title": (row.get("title") or "")[:120],
                "family": research.get("family"),
                "history_unit": hist_u,
                "hist_sources": (research.get("history_branch") or {}).get("hist_sources"),
                "verified_acq": verified[0]["verified_price"] if verified else None,
                "usable_acq": current_unit,
                "leads_n": len(leads),
                "strong_leads": sum(1 for L in leads if L.get("confidence") == "HIGH"),
                "corroborated": len(corroborated),
                "acquisition_range": arange,
                "ballpark": ballpark,
                "apparent_positive": apparent,
                "both_found": both,
                "joined_spread": spread,
                "failure_class": acq.get("failure_class"),
                "blocked_domains": acq.get("blocked_domains"),
                "freight_status": acq.get("freight_status") or FREIGHT_NOT_YET_RESEARCHED,
                "telemetry": tele,
            }
        )

    # Rank manual queue — keep ALL
    manual_queue.sort(key=lambda x: -float(x.get("rank_score") or 0))

    # Recurring + reverse hunt
    events = events_from_candidate_audits(
        [
            {
                **r,
                "manufacturer": (r.get("family")),
                "model": r.get("title"),
                "history_unit": r.get("history_unit"),
                "current_unit": r.get("usable_acq"),
                "buyer": "UNKNOWN",
            }
            for r in results
            if r.get("history_unit") is not None
        ]
    )
    # Better events from history sources when present
    recurring = prioritize_recurring(aggregate_recurring_purchases(events))
    buyers = aggregate_buyer_watchlist(events)
    reverse_matches = reverse_live_hunt(watches=recurring, live_rows=access_yes)

    save_cache(cache)
    save_json(SOURCE_LEARNING_PATH, learning)
    save_json(PRICE_MEMORY_PATH, memory)
    save_json(KNOWN_PATH, known)
    save_health(health)

    coverage_pct = round(100.0 * rows_with_price_evidence / max(1, len(stage3)), 1)

    # Verdict
    if (
        len(stage3) > 0
        and len(results) == len(stage3)
        and totals["strong_leads"] >= 25
        and totals["usable"] >= 10
        and both_found >= 8
        and apparent_positive >= 10
    ):
        verdict = "PHASE_L29_MAXIMUM_SOURCE_EXPANSION_WORKING"
    elif len(results) == len(stage3) and (
        len(family_table) >= 4
        or totals["strong_leads"] >= 10
        or history_found > 14
        or totals["alt"] >= 10
        or len(manual_queue) >= 1
    ):
        verdict = "PHASE_L29_PARTIAL_MAXIMUM_SOURCE_EXPANSION"
    else:
        verdict = "PHASE_L29_MAXIMUM_SOURCE_EXPANSION_FAILED"

    source_report = []
    for fam, c in sorted(family_table.items(), key=lambda kv: -kv[1]["attempts"]):
        source_report.append(
            {
                "source_family": fam,
                "attempts": int(c["attempts"]),
                "exact_hits": int(c["exact_hits"]),
                "price_leads": int(c["price_leads"]),
                "verified_prices": int(c["verified_prices"]),
                "history_hits": int(c["history_hits"]),
                "blocks_failures": int(c["blocks"]),
            }
        )
    # Ensure inventory families appear
    for dash in health_dashboard(health):
        if not any(s["source_family"] == dash["family"] for s in source_report):
            source_report.append(
                {
                    "source_family": dash["family"],
                    "attempts": dash["attempts"],
                    "exact_hits": dash["exact_hits"],
                    "price_leads": dash["price_leads"],
                    "verified_prices": dash["verified_prices"],
                    "history_hits": dash["history_hits"],
                    "blocks_failures": dash["blocks"],
                    "health": dash["status"],
                }
            )

    payload = {
        "kind": "PhaseL29MaximumSourceCoverageResult",
        "phase": "L.2.9",
        "build": "20260927-m3-phase-l29-maximum-source-coverage",
        "generated_at": _utc(),
        "verdict": verdict,
        "stage3_no_row_cap": STAGE3_NO_ROW_CAP,
        "deep_research_no_fixed_count": DEEP_RESEARCH_NO_FIXED_COUNT,
        "manual_queue_no_fixed_cap": MANUAL_QUEUE_NO_FIXED_CAP,
        "no_captcha_bypass": True,
        "opportunity_coverage": {
            "raw_access_yes": len(access_yes),
            "stage1": stage_counts["stage1"],
            "stage2": stage_counts["stage2"],
            "stage3": len(stage3),
            "stage3_processed": len(results),
            "deep_researched_queue": len(deep_escalations),
        },
        "funnel": {
            "stage3_candidates": len(stage3),
            "stage3_processed": len(results),
            "price_evidence_coverage_pct": coverage_pct,
            "history_found": history_found,
            "history_price_joins": history_joins,
            "price_leads": int(totals["leads"]),
            "strong_leads": int(totals["strong_leads"]),
            "corroborated_strong": int(totals["corroborated"]),
            "verified_prices": int(totals["verified"]),
            "usable_prices": int(totals["usable"]),
            "both_found": both_found,
            "apparent_positive_economics": apparent_positive,
            "verified_positive_economics": verified_positive,
            "ge_10k": ge_10k,
            "ge_25k": ge_25k,
            "ge_50k": ge_50k,
            "manual_fallback": len(manual_queue),
            "deep_escalations": len(deep_escalations),
            "pages_attempted": int(totals["fetches"]),
            "fetch_successes": int(totals["fetch_ok"]),
            "blocked_events": int(totals["blocked"]),
            "alternate_attempts": int(totals["alt"]),
            "static_artifacts": int(totals["static"]),
            "freight_not_yet_researched": int(funnel.get("freight_not_yet_researched") or 0),
        },
        "source_by_source": source_report,
        "source_health": health_dashboard(health),
        "failure_taxonomy": failure_taxonomy.most_common(20),
        "circuit_breaker": breaker.telemetry(),
        "recurring_buy": {
            "products_tracked": len(recurring),
            "buyers_tracked": len(buyers),
            "known_repeat_products": len(
                [w for w in recurring if "KNOWN_PROFITABLE" in str(w.get("status") or "")]
            ),
            "known_product_economics": len((known.get("products") or {})),
            "reverse_hunt_live_matches": len(reverse_matches),
            "watches": recurring[:40],
            "reverse_matches": reverse_matches[:40],
        },
        "manual_price_verification_priority": manual_queue,  # ALL — no trim discard
        "deep_escalations": deep_escalations,
        "candidate_results": results,
        "ready_to_bid": False,
        "stop": True,
        "phase_m_started": False,
    }
    _write("l29_maximum_source_coverage.json", payload)
    _write(
        "l29_summary.json",
        {
            "verdict": verdict,
            "opportunity_coverage": payload["opportunity_coverage"],
            "funnel": payload["funnel"],
            "source_by_source_top": source_report[:16],
            "recurring_buy": {
                "products_tracked": len(recurring),
                "buyers_tracked": len(buyers),
                "reverse_hunt_live_matches": len(reverse_matches),
                "known_product_economics": len((known.get("products") or {})),
            },
            "manual_fallback_n": len(manual_queue),
            "deep_escalations_n": len(deep_escalations),
            "circuit_breaker_skipped": breaker.telemetry().get("skipped"),
        },
    )
    return payload

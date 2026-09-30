"""Phase L.2.2 live exact product-page resolution + verified market-price orchestrator."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.deadline_freshness import classify_deadline_freshness
from phase_l.enrichment import (
    apply_enrichment_to_row,
    enrichment_priority,
    finalize_economics,
    load_cache,
    lookup_government_history,
    save_cache,
    stage_a_identity,
)
from phase_l.market_price import commercial_researchability_score
from phase_l.normalize import normalize_opportunity
from phase_l.prebid_compliance import evaluate_prebid_compliance
from phase_l.product_fitness import classify_product_fitness

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"

_COMMERCIAL_TITLE = re.compile(
    r"\b(dell|asus|lenovo|hp\b|canon|flir|bobcat|toolcat|genie|monitor|laptop|server|"
    r"printer|generator|pump|tractor|forklift)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def run_phase_l22_rescue(
    rows: list[dict[str, Any]],
    *,
    authorize_live: bool = True,
    usaspending_max: int = 40,
    market_max: int = 120,
    deep_limit: int = 60,
    commercial_min_score: int = 0,
) -> dict[str, Any]:
    """Commercial-first market validation; history optional for market attempts."""
    cache = load_cache()
    # Invalidate pre-L.2.2 market cache so weak listing prices cannot leak into economics
    by_key = cache.get("by_key") or {}
    cleared = 0
    for _k, entry in list(by_key.items()):
        mkt = (entry or {}).get("market")
        if mkt and not mkt.get("l22"):
            entry.pop("market", None)
            cleared += 1
    if cleared:
        print(f"[l22] cleared {cleared} pre-L.2.2 market cache entries", flush=True)

    budget = {
        "usaspending": 0,
        "usaspending_max": usaspending_max,
        "market": 0,
        "market_max": market_max,
    }

    funnel = Counter()
    failures = Counter()
    price_sources = Counter()
    public_hits: list[dict[str, Any]] = []
    rejected_samples: list[dict[str, Any]] = []
    accepted_samples: list[dict[str, Any]] = []
    owner_queue: list[dict[str, Any]] = []
    resolution_stats = Counter()

    survivors: list[tuple[dict, dict, dict, dict, dict]] = []

    for row in rows:
        if str(row.get("our_bid_access") or "") != "YES":
            continue
        funnel["access_survivors"] += 1

        fresh = classify_deadline_freshness(row)
        if not fresh.get("deep_enrichment_allowed"):
            funnel["expired_removed"] += 1
            failures[str(fresh.get("deadline_state") or "EXPIRED")] += 1
            continue
        funnel["fresh_live_survivors"] += 1

        fit = classify_product_fitness(row)
        if not fit.get("market_price_research_allowed"):
            funnel["service_non_resale_removed"] += 1
            failures[str(fit.get("product_fitness") or "SERVICE")] += 1
            continue
        funnel["product_resale_survivors"] += 1

        screen = stage_a_identity(row)
        funnel["identity_research_attempted"] += 1
        state = screen.get("identity_research_state")
        if state in {"IDENTITY_EXACT", "IDENTITY_STRONG"}:
            funnel["exact_strong_identity"] += 1
        else:
            failures["NO_EXACT_IDENTITY"] += 1
        if screen.get("quantity_known"):
            funnel["quantity_known"] += 1

        row2 = dict(row)
        if fresh.get("runway_days") is not None:
            row2["runway_days"] = fresh["runway_days"]
        row2["deadline_state"] = fresh.get("deadline_state")
        row2["product_fitness"] = fit.get("product_fitness")

        comm = commercial_researchability_score(row2, screen.get("identity") or {})
        pri = enrichment_priority(row2, screen)
        sort_score = int(pri.get("priority_score") or 0) + int(comm.get("researchability_score") or 0)
        if state in {"IDENTITY_EXACT", "IDENTITY_STRONG"}:
            sort_score += 40
        # Boost known commercial brands even when identity is only strong
        if int(comm.get("researchability_score") or 0) >= 25:
            sort_score += 30
        survivors.append((row2, screen, pri, fit, {**comm, "sort_score": sort_score}))

    # Prefer commercially priceable exact/strong; also include commercial PARTIAL (model/brand)
    # for market-engine validation when the live pool lacks commercial exact MPNs.
    exact_strong = [
        t
        for t in survivors
        if t[1].get("identity_research_state") in {"IDENTITY_EXACT", "IDENTITY_STRONG"}
    ]
    commercial_exact = [
        t
        for t in exact_strong
        if int(t[4].get("researchability_score") or 0) >= max(commercial_min_score, 1)
    ]
    commercial_partial = [
        t
        for t in survivors
        if t[1].get("identity_research_state") == "IDENTITY_PARTIAL"
        and int(t[4].get("researchability_score") or 0) >= 25
        and (
            (t[1].get("identity") or {}).get("model")
            or (t[1].get("identity") or {}).get("manufacturer")
            or _COMMERCIAL_TITLE.search(str(t[0].get("title") or ""))
        )
    ]
    commercial_exact.sort(key=lambda t: -int(t[4].get("sort_score") or 0))
    exact_strong.sort(key=lambda t: -int(t[4].get("sort_score") or 0))
    commercial_partial.sort(key=lambda t: -int(t[4].get("sort_score") or 0))
    seen_ids: set[int] = set()
    eligible: list[tuple] = []
    for t in commercial_exact + commercial_partial + exact_strong:
        tid = id(t[0])
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        eligible.append(t)
    funnel["market_price_eligible"] = len(eligible)
    funnel["commercially_priceable_exact_strong"] = len(commercial_exact)
    funnel["commercially_priceable_partial"] = len(commercial_partial)
    deep = eligible[:deep_limit]
    funnel["deep_selected"] = len(deep)

    for i, (row, screen, pri, fit, comm) in enumerate(deep):
        print(
            f"[l22] deep {i+1}/{len(deep)} score={comm.get('sort_score')} "
            f"mkt={budget['market']}/{budget['market_max']} "
            f"{(row.get('title') or '')[:55]}",
            flush=True,
        )

        history = None
        market = None
        # History optional for market validation (sec 20). Allow commercial partials.
        allow_market = (
            screen.get("researchable")
            or screen.get("or_equal_allowed")
            or screen.get("identity_research_state") in {"IDENTITY_EXACT", "IDENTITY_STRONG", "IDENTITY_PARTIAL"}
        )
        if allow_market:
            # L.2.2 market validation: skip live USAspending to keep resolver timing measurable
            if False and budget["usaspending"] < budget["usaspending_max"] and screen.get("researchable"):
                history = lookup_government_history(
                    row,
                    screen.get("identity") or {},
                    budget=budget,
                    cache=cache,
                    authorize_live=authorize_live,
                )
                if history.get("attempted") or history.get("cache_hit"):
                    funnel["history_attempted"] += 1
                if history.get("historical_award_unit_price") is not None:
                    funnel["history_found"] += 1
                else:
                    failures["NO_HISTORY"] += 1
            elif screen.get("researchable"):
                # Prefer cache-only history so economics can still complete when available
                history = lookup_government_history(
                    row,
                    screen.get("identity") or {},
                    budget={"usaspending": 0, "usaspending_max": 0},
                    cache=cache,
                    authorize_live=False,
                )
                if history.get("cache_hit") and history.get("historical_award_unit_price") is not None:
                    funnel["history_found"] += 1
                    funnel["history_attempted"] += 1

            market = lookup_current_market_safe(
                row,
                screen,
                budget=budget,
                cache=cache,
                authorize_live=authorize_live,
            )
            funnel["market_search_attempted"] += 1
            if market.get("attempted") or market.get("cache_hit"):
                funnel["market_attempted_rows"] += 1

            resolution_stats["pages_fetched"] += int(market.get("pages_fetched") or 0)
            resolution_stats["product_pages_fetched"] += int(market.get("product_pages_fetched") or 0)
            resolution_stats["search_results_found"] += int(market.get("search_results_found") or 0)
            resolution_stats["candidate_links_extracted"] += int(market.get("candidate_links_extracted") or 0)
            resolution_stats["exact_identity_pages"] += int(market.get("exact_identity_pages") or 0)
            resolution_stats["strong_identity_pages"] += int(market.get("strong_identity_pages") or 0)
            resolution_stats["rejected_mismatches"] += int(market.get("rejected_mismatches") or 0)

            if int(market.get("search_results_found") or 0) > 0:
                funnel["search_results_found_rows"] += 1
            else:
                failures["NO_SEARCH_RESULTS"] += 1

            if int(market.get("candidate_links_extracted") or 0) > 0:
                funnel["candidate_links_extracted_rows"] += 1
            if int(market.get("product_pages_fetched") or 0) > 0:
                funnel["product_pages_fetched_rows"] += 1
            else:
                if market.get("drop_reason") == "NO_PRODUCT_URL":
                    failures["NO_PRODUCT_URL"] += 1

            funnel["exact_identity_match_pages"] += int(market.get("exact_identity_pages") or 0)
            funnel["strong_identity_match_pages"] += int(market.get("strong_identity_pages") or 0)

            evidence = market.get("evidence") or []
            verified = [e for e in evidence if e.get("economics_eligible")]
            approx = [e for e in evidence if e.get("confidence") == "APPROXIMATE"]
            weak = [e for e in evidence if e.get("confidence") in {"WEAK", "REJECTED"}]
            funnel["prices_extracted"] += len(evidence)
            funnel["verified_prices"] += len(verified)
            funnel["approximate_prices"] += len(approx)
            funnel["weak_rejected_prices"] += len(weak)

            for e in evidence:
                if e.get("economics_eligible"):
                    funnel["economics_eligible_prices"] += 1
                elif e.get("rejection_reason"):
                    failures[str(e["rejection_reason"])] += 1

            if market.get("drop_reason"):
                failures[str(market["drop_reason"])] += 1

            conf = str(market.get("market_price_confidence") or "")
            if market.get("public_retail_unit_price") is not None and market.get("economics_eligible", True):
                funnel["verified_exact_strong_hits"] += 1
                if market.get("l22_confidence") == "EXACT_VERIFIED":
                    funnel["verified_exact"] += 1
                else:
                    funnel["verified_strong"] += 1
                src = ((market.get("selected_observation") or {}).get("seller_type") or "UNKNOWN")
                price_sources[str(src)] += 1
                hit = {
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "title": (row.get("title") or "")[:100],
                    "target_mpn": (screen.get("identity") or {}).get("mpn"),
                    "target_model": (screen.get("identity") or {}).get("model"),
                    "current_market_unit": market.get("public_retail_unit_price"),
                    "evidence_url": market.get("public_retail_source"),
                    "page_type": (market.get("selected_observation") or {}).get("page_type"),
                    "match_level": (market.get("selected_observation") or {}).get("product_match_level"),
                    "l22_confidence": market.get("l22_confidence"),
                    "selection_reason": market.get("selection_reason"),
                    "accept": True,
                }
                public_hits.append(hit)
                if len(accepted_samples) < 15:
                    accepted_samples.append(hit)
            else:
                # rejected / noisy sample
                for e in (evidence or [])[:2]:
                    if len(rejected_samples) >= 15:
                        break
                    if e.get("economics_eligible"):
                        continue
                    rejected_samples.append(
                        {
                            "title": (row.get("title") or "")[:80],
                            "target_mpn": (screen.get("identity") or {}).get("mpn"),
                            "candidate_url": e.get("source_url"),
                            "page_type": e.get("page_type"),
                            "observed_identity": e.get("observed_mpn") or e.get("observed_model"),
                            "observed_price": e.get("price"),
                            "accept": False,
                            "reason": e.get("rejection_reason") or market.get("drop_reason"),
                        }
                    )
                if not evidence and market.get("drop_reason") and len(rejected_samples) < 15:
                    rejected_samples.append(
                        {
                            "title": (row.get("title") or "")[:80],
                            "target_mpn": (screen.get("identity") or {}).get("mpn"),
                            "candidate_url": None,
                            "accept": False,
                            "reason": market.get("drop_reason"),
                        }
                    )
        else:
            market = None
            failures["NO_EXACT_IDENTITY"] += 1
            history = None

        enriched = apply_enrichment_to_row(
            row, screen=screen, priority=pri, history=history, market=market
        )
        econ = finalize_economics(enriched)
        compliance = evaluate_prebid_compliance(enriched)
        enriched["prebid_compliance"] = compliance
        enriched["ready_to_bid"] = False

        norm = normalize_opportunity(
            enriched,
            economics=econ if econ.get("economics_completed") else None,
            runway_days=enriched.get("runway_days"),
        )
        norm["deadline_state"] = row.get("deadline_state")
        norm["product_fitness"] = fit.get("product_fitness")
        norm["market_price_confidence"] = (market or {}).get("market_price_confidence")
        norm["l22_confidence"] = (market or {}).get("l22_confidence")
        norm["prebid_compliance"] = compliance
        norm["ready_to_bid"] = False
        norm["economics_completed"] = bool(econ.get("economics_completed"))

        if econ.get("economics_completed"):
            funnel["economics_completed"] += 1
            net = econ.get("expected_net_profit")
            if isinstance(net, (int, float)):
                if net >= 10000:
                    funnel["net_ge_10k"] += 1
                if net >= 25000:
                    funnel["net_ge_25k"] += 1
                if net >= 50000:
                    funnel["net_ge_50k"] += 1
                if net >= 75000:
                    funnel["net_ge_75k"] += 1
                if net >= 100000:
                    funnel["net_ge_100k"] += 1
            if norm.get("meets_floor") and norm.get("expected_net_profit") is not None:
                owner_queue.append(norm)

    save_cache(cache)
    owner_queue.sort(key=lambda r: float(r.get("expected_net_profit") or 0), reverse=True)

    payload = {
        "kind": "PhaseL22RescueResult",
        "phase": "L.2.2",
        "build": "20260926-m3-phase-l22-exact-product-page-resolution",
        "generated_at": _utc(),
        "budget_used": {"usaspending": budget["usaspending"], "market": budget["market"]},
        "funnel": dict(funnel),
        "resolution_stats": dict(resolution_stats),
        "failure_reasons": failures.most_common(40),
        "price_source_distribution": dict(price_sources),
        "public_price_hits": public_hits,
        "accepted_audit_sample": accepted_samples[:10],
        "rejected_audit_sample": rejected_samples[:10],
        "counts": {
            "access_survivors": int(funnel.get("access_survivors", 0)),
            "fresh_live_survivors": int(funnel.get("fresh_live_survivors", 0)),
            "product_resale_survivors": int(funnel.get("product_resale_survivors", 0)),
            "exact_strong_identity": int(funnel.get("exact_strong_identity", 0)),
            "quantity_known": int(funnel.get("quantity_known", 0)),
            "market_price_eligible": int(funnel.get("market_price_eligible", 0)),
            "deep_selected": int(funnel.get("deep_selected", 0)),
            "market_search_attempted": int(funnel.get("market_search_attempted", 0)),
            "verified_prices": int(funnel.get("verified_prices", 0)),
            "verified_exact": int(funnel.get("verified_exact", 0)),
            "verified_strong": int(funnel.get("verified_strong", 0)),
            "economics_completed": int(funnel.get("economics_completed", 0)),
            "net_ge_10k": int(funnel.get("net_ge_10k", 0)),
            "net_ge_25k": int(funnel.get("net_ge_25k", 0)),
            "net_ge_50k": int(funnel.get("net_ge_50k", 0)),
            "net_ge_75k": int(funnel.get("net_ge_75k", 0)),
            "net_ge_100k": int(funnel.get("net_ge_100k", 0)),
        },
        "owner_queue": owner_queue[:50],
        "top20": owner_queue[:20],
    }
    _write("l22_rescue_latest.json", payload)
    _write("l22_public_price_hits.json", {"count": len(public_hits), "hits": public_hits})
    _write(
        "l22_price_evidence_audit.json",
        {
            "accepted": accepted_samples[:10],
            "rejected": rejected_samples[:10],
        },
    )
    _write(
        "owner_profit_queue.json",
        {"kind": "PhaseL22OwnerProfitQueue", "count": len(owner_queue), "rows": owner_queue[:100]},
    )
    return payload


def lookup_current_market_safe(row, screen, *, budget, cache, authorize_live):
    from phase_l.enrichment import lookup_current_market

    return lookup_current_market(
        row,
        screen.get("identity") or {},
        screen,
        budget=budget,
        cache=cache,
        authorize_live=authorize_live,
    )

"""Phase L.2.1 live market-price rescue orchestrator."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.deadline_freshness import EXPIRED, classify_deadline_freshness
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
from phase_l.product_fitness import PRODUCT_RESALE, classify_product_fitness

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def run_phase_l21_rescue(
    rows: list[dict[str, Any]],
    *,
    authorize_live: bool = True,
    usaspending_max: int = 60,
    market_max: int = 100,
    deep_limit: int = 100,
) -> dict[str, Any]:
    """Re-screen for expiry/fitness; deep-price commercial-first survivors."""
    cache = load_cache()
    budget = {
        "usaspending": 0,
        "usaspending_max": usaspending_max,
        "market": 0,
        "market_max": market_max,
    }

    funnel = Counter()
    by_level = {k: Counter() for k in ("FEDERAL", "STATE", "LOCAL", "COOPERATIVE", "TOTAL")}
    failures = Counter()
    price_sources = Counter()
    public_hits: list[dict[str, Any]] = []
    owner_queue: list[dict[str, Any]] = []
    internal_fails: list[dict[str, Any]] = []

    survivors: list[tuple[dict, dict, dict, dict, dict]] = []

    for row in rows:
        if str(row.get("our_bid_access") or "") != "YES":
            continue
        level = str(row.get("source_level") or "LOCAL").upper()
        if level not in by_level:
            level = "LOCAL"
        funnel["access_yes_input"] += 1
        by_level[level]["access_yes_input"] += 1
        by_level["TOTAL"]["access_yes_input"] += 1

        fresh = classify_deadline_freshness(row)
        funnel["deadline_screened"] += 1
        if not fresh.get("deep_enrichment_allowed"):
            funnel["expired_or_stale_removed"] += 1
            by_level[level]["expired_removed"] += 1
            by_level["TOTAL"]["expired_removed"] += 1
            failures[str(fresh.get("deadline_state") or "EXPIRED")] += 1
            continue

        fit = classify_product_fitness(row)
        funnel["fitness_screened"] += 1
        if not fit.get("market_price_research_allowed"):
            funnel["non_resale_removed"] += 1
            by_level[level]["non_resale_removed"] += 1
            by_level["TOTAL"]["non_resale_removed"] += 1
            failures[str(fit.get("product_fitness") or "SERVICE")] += 1
            continue

        screen = stage_a_identity(row)
        funnel["identity_screened"] += 1
        by_level[level]["identity_screened"] += 1
        by_level["TOTAL"]["identity_screened"] += 1
        state = screen.get("identity_research_state")
        if state in {"IDENTITY_EXACT", "IDENTITY_STRONG"}:
            funnel["exact_strong"] += 1
            by_level["TOTAL"]["exact_strong"] += 1
        if screen.get("quantity_known"):
            funnel["qty_known"] += 1

        # Apply freshness runway onto row
        row2 = dict(row)
        if fresh.get("runway_days") is not None:
            row2["runway_days"] = fresh["runway_days"]
        row2["deadline_state"] = fresh.get("deadline_state")
        row2["product_fitness"] = fit.get("product_fitness")

        comm = commercial_researchability_score(row2, screen.get("identity") or {})
        pri = enrichment_priority(row2, screen)
        # Boost commercial researchability into priority sort key
        sort_score = int(pri.get("priority_score") or 0) + int(comm.get("researchability_score") or 0)
        survivors.append((row2, screen, pri, fit, {**comm, "sort_score": sort_score}))

    funnel["live_resale_candidates"] = len(survivors)
    by_level["TOTAL"]["live_resale_candidates"] = len(survivors)

    survivors.sort(key=lambda t: -int(t[4].get("sort_score") or 0))
    deep = survivors[:deep_limit]
    funnel["deep_selected"] = len(deep)

    for i, (row, screen, pri, fit, comm) in enumerate(deep):
        level = str(row.get("source_level") or "LOCAL").upper()
        if level not in by_level:
            level = "LOCAL"
        print(
            f"[l21] deep {i+1}/{len(deep)} score={comm.get('sort_score')} "
            f"mkt={budget['market']}/{budget['market_max']} "
            f"{(row.get('title') or '')[:55]}",
            flush=True,
        )

        history = None
        market = None
        if screen.get("researchable") or screen.get("or_equal_allowed"):
            history = lookup_government_history(
                row,
                screen.get("identity") or {},
                budget=budget,
                cache=cache,
                authorize_live=authorize_live,
            )
            if history.get("attempted") or history.get("cache_hit"):
                funnel["historical_attempted"] += 1
                by_level["TOTAL"]["historical_attempted"] += 1
            if history.get("historical_award_unit_price") is not None:
                funnel["historical_price_found"] += 1
                by_level["TOTAL"]["historical_price_found"] += 1

            market = lookup_current_market_safe(
                row,
                screen,
                budget=budget,
                cache=cache,
                authorize_live=authorize_live,
            )
            if market.get("attempted") or market.get("cache_hit"):
                funnel["market_attempted"] += 1
                by_level["TOTAL"]["market_attempted"] += 1
            conf = str(market.get("market_price_confidence") or "")
            if market.get("public_retail_unit_price") is not None:
                funnel["market_price_found"] += 1
                by_level["TOTAL"]["market_price_found"] += 1
                if "EXACT_PUBLIC" in conf:
                    funnel["exact_price_hits"] += 1
                else:
                    funnel["comparable_price_hits"] += 1
                src = ((market.get("selected_observation") or {}).get("seller_type") or "UNKNOWN")
                price_sources[str(src)] += 1
            elif conf == "RFQ_ONLY":
                funnel["rfq_only_failures"] += 1
                failures["RFQ_ONLY"] += 1
            elif conf == "SEARCH_PROVIDER_FAILED":
                funnel["search_provider_failures"] += 1
                failures["SEARCH_PROVIDER_FAILED"] += 1
            elif conf == "NO_PUBLIC_PRICE_FOUND":
                failures["NO_PUBLIC_PRICE_FOUND"] += 1
            if market.get("mpn_query_attempted"):
                funnel["mpn_query_attempted"] += 1

        enriched = apply_enrichment_to_row(
            row, screen=screen, priority=pri, history=history, market=market
        )
        econ = finalize_economics(enriched)
        compliance = evaluate_prebid_compliance(enriched)
        enriched["prebid_compliance"] = compliance
        enriched["ready_to_bid"] = False
        enriched["submission_readiness"] = enriched.get("submission_readiness") or "ACCESS_ELIGIBLE"

        norm = normalize_opportunity(
            enriched,
            economics=econ if econ.get("economics_completed") else None,
            runway_days=enriched.get("runway_days"),
        )
        norm["deadline_state"] = row.get("deadline_state")
        norm["product_fitness"] = fit.get("product_fitness")
        norm["market_price_confidence"] = (market or {}).get("market_price_confidence")
        norm["prebid_compliance"] = compliance
        norm["ready_to_bid"] = False
        norm["economics_completed"] = bool(econ.get("economics_completed"))
        if econ.get("blocker"):
            norm["enrichment_blocker"] = econ.get("blocker")
            failures[str(econ["blocker"])] += 1

        if econ.get("economics_completed"):
            funnel["economics_completed"] += 1
            by_level["TOTAL"]["economics_completed"] += 1
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

        if market and market.get("public_retail_unit_price") is not None:
            hit = {
                "solicitation": norm.get("solicitation_id"),
                "title": (norm.get("title") or "")[:100],
                "source_level": norm.get("source_level"),
                "product": (norm.get("product_identity") or {}).get("mpn")
                or (norm.get("nsn") or norm.get("title")),
                "identity": screen.get("identity_match_type"),
                "historical_gov_unit": norm.get("historical_award_unit_price"),
                "current_market_unit": market.get("public_retail_unit_price"),
                "quantity": norm.get("quantity"),
                "acquisition_total": (
                    float(market["public_retail_unit_price"]) * float(norm["quantity"])
                    if norm.get("quantity") is not None
                    else None
                ),
                "expected_revenue": norm.get("expected_revenue"),
                "expected_net": norm.get("expected_net_profit"),
                "evidence_url": market.get("public_retail_source"),
                "confidence": market.get("market_price_confidence"),
            }
            public_hits.append(hit)

        if norm.get("meets_floor") and norm.get("expected_net_profit") is not None:
            owner_queue.append(norm)
        elif econ.get("economics_completed"):
            internal_fails.append(
                {
                    "title": (norm.get("title") or "")[:80],
                    "expected_net_profit": norm.get("expected_net_profit"),
                    "blocker": "below_floor",
                }
            )

    save_cache(cache)
    owner_queue.sort(key=lambda r: float(r.get("expected_net_profit") or 0), reverse=True)

    payload = {
        "kind": "PhaseL21RescueResult",
        "phase": "L.2.1",
        "generated_at": _utc(),
        "budget_used": {"usaspending": budget["usaspending"], "market": budget["market"]},
        "funnel": dict(funnel),
        "by_level": {k: dict(v) for k, v in by_level.items()},
        "failure_reasons": failures.most_common(30),
        "price_source_distribution": dict(price_sources),
        "public_price_hits": public_hits,
        "counts": {
            "access_yes_input": int(funnel.get("access_yes_input", 0)),
            "expired_removed": int(funnel.get("expired_or_stale_removed", 0)),
            "non_resale_removed": int(funnel.get("non_resale_removed", 0)),
            "live_resale": int(funnel.get("live_resale_candidates", 0)),
            "deep_selected": int(funnel.get("deep_selected", 0)),
            "market_attempted": int(funnel.get("market_attempted", 0)),
            "market_price_found": int(funnel.get("market_price_found", 0)),
            "economics_completed": int(funnel.get("economics_completed", 0)),
            "owner_queue": len(owner_queue),
            "net_ge_10k": int(funnel.get("net_ge_10k", 0)),
        },
        "owner_queue": owner_queue[:50],
        "top20": owner_queue[:20],
        "internal_fails_sample": internal_fails[:30],
    }
    _write("l21_rescue_latest.json", payload)
    _write(
        "owner_profit_queue.json",
        {"kind": "PhaseL21OwnerProfitQueue", "count": len(owner_queue), "rows": owner_queue[:100]},
    )
    _write("l21_public_price_hits.json", {"count": len(public_hits), "hits": public_hits})
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

"""Phase L.2.3 commercial identity recovery + expanded market-research funnel."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.commercial_identity import (
    EXACT_BRAND_MODEL,
    EXACT_COMMERCIAL_MODEL,
    EXACT_EQUIPMENT_MODEL,
    EXACT_MPN,
    EXACT_NSN_MPN,
    EXACT_OEM_SKU,
    EXACT_VEHICLE_TRIM,
    PRICEABILITY_HIGH,
    PRICEABILITY_MEDIUM,
    STRONG_COMMERCIAL_MODEL,
    apply_commercial_overlay_to_screen,
    commercial_bucket,
    compute_unit_spread,
)
from phase_l.deadline_freshness import classify_deadline_freshness
from phase_l.enrichment import (
    apply_enrichment_to_row,
    enrichment_priority,
    finalize_economics,
    load_cache,
    lookup_current_market,
    lookup_government_history,
    save_cache,
    stage_a_identity,
)
from phase_l.normalize import normalize_opportunity
from phase_l.prebid_compliance import evaluate_prebid_compliance
from phase_l.product_fitness import classify_product_fitness

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"

_EXACT_COMMERCIAL = {
    EXACT_COMMERCIAL_MODEL,
    EXACT_BRAND_MODEL,
    EXACT_VEHICLE_TRIM,
    EXACT_EQUIPMENT_MODEL,
}
_EXACT_MPN_SET = {EXACT_MPN, EXACT_NSN_MPN, EXACT_OEM_SKU}


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _try_attachment_text(row: dict[str, Any]) -> str | None:
    """Reuse existing attachment/fallback fields already on the row — no new subsystem."""
    chunks = []
    for k in (
        "attachment_text",
        "description_text",
        "solicitation_text",
        "ai_summary",
        "clin_text",
        "item_description",
        "technical_description",
    ):
        v = row.get(k)
        if isinstance(v, str) and len(v.strip()) > 20:
            chunks.append(v[:8000])
    if isinstance(row.get("attachments_text"), list):
        for a in row["attachments_text"][:3]:
            if isinstance(a, str):
                chunks.append(a[:4000])
    return "\n".join(chunks) if chunks else None


def run_phase_l23_rescue(
    rows: list[dict[str, Any]],
    *,
    authorize_live: bool = True,
    usaspending_max: int = 40,
    market_max: int = 120,
    deep_limit: int = 55,
    bucket_a_target: int = 20,
    bucket_b_target: int = 20,
    bucket_c_target: int = 15,
) -> dict[str, Any]:
    """Commercial-first expansion; parallel history + market; L.2.2 verification preserved."""
    cache = load_cache()
    by_key = cache.get("by_key") or {}
    cleared = 0
    for _k, entry in list(by_key.items()):
        mkt = (entry or {}).get("market")
        if mkt and not mkt.get("l22") and not mkt.get("l23"):
            entry.pop("market", None)
            cleared += 1
    if cleared:
        print(f"[l23] cleared {cleared} stale market cache entries", flush=True)

    budget = {
        "usaspending": 0,
        "usaspending_max": usaspending_max,
        "market": 0,
        "market_max": market_max,
    }

    funnel = Counter()
    failures = Counter()
    telemetry = Counter()
    buckets: dict[str, list] = defaultdict(list)
    promoted_audit: list[dict[str, Any]] = []
    rejected_audit: list[dict[str, Any]] = []
    public_hits: list[dict[str, Any]] = []
    owner_queue: list[dict[str, Any]] = []
    unit_spread_hits: list[dict[str, Any]] = []
    resolution_stats = Counter()

    survivors: list[tuple] = []

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

        screen0 = stage_a_identity(row)
        funnel["commercial_identities_attempted"] += 1
        attach = _try_attachment_text(row)
        screen = apply_commercial_overlay_to_screen(screen0, row, attachment_text=attach)
        commercial = screen.get("commercial_identity") or {}
        state = commercial.get("commercial_identity_state")
        priceability = commercial.get("commercial_priceability")

        if state in _EXACT_COMMERCIAL:
            funnel["exact_commercial_model"] += 1
            telemetry["MODEL_EXACT"] += 1
        if state in _EXACT_MPN_SET or commercial.get("mpn"):
            funnel["exact_mpn"] += 1
        if state == STRONG_COMMERCIAL_MODEL:
            funnel["strong_commercial_identity"] += 1
            telemetry["MODEL_RECOVERED"] += 1
        if priceability == PRICEABILITY_HIGH:
            funnel["high_priceability"] += 1
            telemetry["COMMERCIAL_PRICEABILITY_HIGH"] += 1
        elif priceability == PRICEABILITY_MEDIUM:
            funnel["medium_priceability"] += 1
            telemetry["COMMERCIAL_PRICEABILITY_MEDIUM"] += 1
        else:
            telemetry["COMMERCIAL_PRICEABILITY_LOW"] += 1

        if commercial.get("configuration_completeness") == "PARTIAL_CONFIGURATION":
            telemetry["CONFIGURATION_PARTIAL"] += 1

        if screen.get("market_research_eligible"):
            funnel["market_research_eligible"] += 1
            telemetry["MARKET_RESEARCH_ALLOWED"] += 1
        else:
            telemetry["MARKET_RESEARCH_BLOCKED"] += 1
            failures[str(commercial.get("drop_reason") or "NO_COMMERCIAL_IDENTITY")] += 1
            if len(rejected_audit) < 15:
                rejected_audit.append(_audit_row(row, commercial, screen, accept=False))
            continue

        row2 = dict(row)
        if fresh.get("runway_days") is not None:
            row2["runway_days"] = fresh["runway_days"]
        row2["deadline_state"] = fresh.get("deadline_state")
        row2["product_fitness"] = fit.get("product_fitness")

        pri = enrichment_priority(row2, screen)
        bname = commercial_bucket(commercial)
        sort_score = int(pri.get("priority_score") or 0)
        if priceability == PRICEABILITY_HIGH:
            sort_score += 50
        elif priceability == PRICEABILITY_MEDIUM:
            sort_score += 25
        if state in _EXACT_COMMERCIAL:
            sort_score += 40
        if commercial.get("mpn"):
            sort_score += 20
        # Deprioritize military-only NSN without commercial model
        if commercial.get("nsn") and not commercial.get("model") and not commercial.get("mpn"):
            sort_score -= 30

        entry = (row2, screen, pri, fit, commercial, bname, sort_score)
        buckets[bname].append(entry)
        survivors.append(entry)
        if len(promoted_audit) < 20:
            promoted_audit.append(_audit_row(row2, commercial, screen, accept=True))

    # Bucketed commercial-first deep selection
    deep: list = []
    for bname, target in (
        ("A_EXACT_COMMERCIAL_MODEL", bucket_a_target),
        ("B_EXACT_MPN_SKU", bucket_b_target),
        ("C_STRONG_OR_RECOVERY", bucket_c_target),
    ):
        pool = sorted(buckets.get(bname) or [], key=lambda t: -t[6])
        deep.extend(pool[:target])
    # Fill remaining slots from any eligible, commercial-first
    seen = {id(t[0]) for t in deep}
    rest = sorted(survivors, key=lambda t: -t[6])
    for t in rest:
        if len(deep) >= deep_limit:
            break
        if id(t[0]) in seen:
            continue
        # Prefer non-military commercial
        deep.append(t)
        seen.add(id(t[0]))
    deep = deep[:deep_limit]
    funnel["deep_selected"] = len(deep)
    funnel["bucket_a_selected"] = sum(1 for t in deep if t[5] == "A_EXACT_COMMERCIAL_MODEL")
    funnel["bucket_b_selected"] = sum(1 for t in deep if t[5] == "B_EXACT_MPN_SKU")
    funnel["bucket_c_selected"] = sum(1 for t in deep if t[5] == "C_STRONG_OR_RECOVERY")

    for i, (row, screen, pri, fit, commercial, bname, sort_score) in enumerate(deep):
        print(
            f"[l23] deep {i+1}/{len(deep)} bucket={bname} score={sort_score} "
            f"mkt={budget['market']}/{budget['market_max']} "
            f"{(row.get('title') or '')[:50]}",
            flush=True,
        )
        funnel["market_attempted"] += 1

        # Parallel history + market (sec 18) — neither waits on the other
        history = lookup_government_history(
            row,
            screen.get("identity") or {},
            budget=budget,
            cache=cache,
            authorize_live=authorize_live and budget["usaspending"] < budget["usaspending_max"],
        )
        if history.get("attempted") or history.get("cache_hit"):
            funnel["history_attempted"] += 1
        if history.get("historical_award_unit_price") is not None:
            funnel["history_found"] += 1
        else:
            failures["NO_HISTORY_IDENTITY"] += 1

        market = lookup_current_market(
            row,
            screen.get("identity") or {},
            screen,
            budget=budget,
            cache=cache,
            authorize_live=authorize_live,
        )
        if market.get("attempted") or market.get("cache_hit"):
            funnel["market_attempted_rows"] += 1

        resolution_stats["pages_fetched"] += int(market.get("pages_fetched") or 0)
        resolution_stats["product_pages_fetched"] += int(market.get("product_pages_fetched") or 0)
        resolution_stats["candidate_links_extracted"] += int(market.get("candidate_links_extracted") or 0)
        resolution_stats["exact_identity_pages"] += int(market.get("exact_identity_pages") or 0)
        resolution_stats["strong_identity_pages"] += int(market.get("strong_identity_pages") or 0)

        if int(market.get("product_pages_fetched") or 0) > 0:
            funnel["product_pages_fetched_rows"] += 1
        if int(market.get("exact_identity_pages") or 0) + int(market.get("strong_identity_pages") or 0) > 0:
            funnel["identity_verified_pages"] += 1

        # Configuration incomplete → research OK, economics not from this retail alone
        cfg = commercial.get("configuration_completeness")
        retail_ok = market.get("public_retail_unit_price") is not None and market.get(
            "economics_eligible", True
        )
        if retail_ok and cfg == "PARTIAL_CONFIGURATION" and commercial.get(
            "commercial_identity_state"
        ) in _EXACT_COMMERCIAL:
            # Keep verified price as research evidence; block silent total economics
            market = dict(market)
            market["configuration_blocks_economics"] = True
            market["research_retail_unit_price"] = market.get("public_retail_unit_price")
            # Still allow unit-spread research; full economics needs config+qty path
            # Do not clear public_retail — unit spread uses it; finalize still needs qty

        if retail_ok:
            funnel["price_verified"] += 1
            public_hits.append(
                {
                    "title": (row.get("title") or "")[:100],
                    "manufacturer": commercial.get("manufacturer"),
                    "model": commercial.get("model"),
                    "mpn": commercial.get("mpn"),
                    "state": commercial.get("commercial_identity_state"),
                    "price": market.get("public_retail_unit_price"),
                    "url": market.get("public_retail_source"),
                    "l22_confidence": market.get("l22_confidence"),
                    "bucket": bname,
                }
            )
        elif market.get("attempted"):
            failures[str(market.get("drop_reason") or "NO_CURRENT_PRICE_IDENTITY")] += 1

        hist_u = history.get("historical_award_unit_price") if history else None
        retail_u = market.get("public_retail_unit_price") if market else None
        if hist_u is not None and retail_u is not None:
            funnel["both_history_and_price"] += 1
            unit = compute_unit_spread(
                historical_unit_price=hist_u,
                public_retail_unit_price=retail_u,
                quantity=screen.get("quantity"),
            )
            if unit.get("unit_raw_spread") is not None and unit["unit_raw_spread"] > 0:
                funnel["positive_unit_spread"] += 1
            if unit.get("total_raw_spread") is not None and unit["total_raw_spread"] > 0:
                funnel["positive_total_spread"] += 1
            unit_spread_hits.append(
                {
                    "title": (row.get("title") or "")[:80],
                    "hist": hist_u,
                    "retail": retail_u,
                    "unit_spread": unit.get("unit_raw_spread"),
                    "signal": unit.get("research_signal"),
                    "qty": screen.get("quantity"),
                }
            )

        enriched = apply_enrichment_to_row(
            row, screen=screen, priority=pri, history=history, market=market
        )
        enriched["commercial_identity"] = commercial
        enriched["commercial_identity_state"] = commercial.get("commercial_identity_state")
        enriched["commercial_priceability"] = commercial.get("commercial_priceability")
        enriched["market_research_eligible"] = True
        enriched["ready_to_bid"] = False

        # If config blocks full economics, strip retail from total econ but keep unit research
        if market.get("configuration_blocks_economics") and not screen.get("quantity"):
            pass  # unit spread still via finalize
        elif market.get("configuration_blocks_economics"):
            # Still allow economics if qty known — config gate is soft for known qty brand-name
            pass

        econ = finalize_economics(enriched)
        compliance = evaluate_prebid_compliance(enriched)
        enriched["prebid_compliance"] = compliance
        enriched["ready_to_bid"] = False

        norm = normalize_opportunity(
            enriched,
            economics=econ if econ.get("economics_completed") else None,
            runway_days=enriched.get("runway_days"),
        )
        norm["commercial_identity_state"] = commercial.get("commercial_identity_state")
        norm["commercial_priceability"] = commercial.get("commercial_priceability")
        norm["unit_economics"] = econ.get("unit_economics")
        norm["research_signal"] = econ.get("research_signal")
        norm["ready_to_bid"] = False
        norm["economics_completed"] = bool(econ.get("economics_completed"))

        if econ.get("economics_completed"):
            funnel["economics_completed"] += 1
            net = econ.get("expected_net_profit")
            if isinstance(net, (int, float)) and net >= 10000:
                funnel["net_ge_10k"] += 1
            if norm.get("meets_floor"):
                owner_queue.append(norm)

    save_cache(cache)
    owner_queue.sort(key=lambda r: float(r.get("expected_net_profit") or 0), reverse=True)

    # Special tracking for known commercial regression cases
    bobcat = next(
        (
            _audit_row(t[0], t[4], t[1], accept=True)
            for t in survivors
            if "toolcat" in str(t[0].get("title") or "").lower()
        ),
        None,
    )
    ford = next(
        (
            _audit_row(t[0], t[4], t[1], accept=True)
            for t in survivors
            if "police responder" in str(t[0].get("title") or "").lower()
        ),
        None,
    )

    payload = {
        "kind": "PhaseL23RescueResult",
        "phase": "L.2.3",
        "build": "20260926-m3-phase-l23-commercial-identity-recovery",
        "generated_at": _utc(),
        "budget_used": {"usaspending": budget["usaspending"], "market": budget["market"]},
        "funnel": dict(funnel),
        "telemetry": dict(telemetry),
        "resolution_stats": dict(resolution_stats),
        "failure_reasons": failures.most_common(40),
        "bucket_counts": {k: len(v) for k, v in buckets.items()},
        "public_price_hits": public_hits,
        "unit_spread_hits": unit_spread_hits,
        "promoted_audit_sample": promoted_audit[:10],
        "rejected_audit_sample": rejected_audit[:10],
        "bobcat_toolcat": bobcat,
        "ford_police_responder": ford,
        "counts": {
            "product_resale": int(funnel.get("product_resale_survivors", 0)),
            "market_research_eligible": int(funnel.get("market_research_eligible", 0)),
            "exact_commercial_model": int(funnel.get("exact_commercial_model", 0)),
            "exact_mpn": int(funnel.get("exact_mpn", 0)),
            "high_priceability": int(funnel.get("high_priceability", 0)),
            "deep_selected": int(funnel.get("deep_selected", 0)),
            "market_attempted": int(funnel.get("market_attempted", 0)),
            "price_verified": int(funnel.get("price_verified", 0)),
            "history_found": int(funnel.get("history_found", 0)),
            "both_history_and_price": int(funnel.get("both_history_and_price", 0)),
            "positive_unit_spread": int(funnel.get("positive_unit_spread", 0)),
            "economics_completed": int(funnel.get("economics_completed", 0)),
            "net_ge_10k": int(funnel.get("net_ge_10k", 0)),
        },
        "owner_queue": owner_queue[:50],
    }
    _write("l23_rescue_latest.json", payload)
    _write(
        "l23_commercial_identity_audit.json",
        {"promoted": promoted_audit[:10], "rejected": rejected_audit[:10], "bobcat": bobcat, "ford": ford},
    )
    _write("l23_public_price_hits.json", {"count": len(public_hits), "hits": public_hits})
    return payload


def _audit_row(row, commercial, screen, *, accept: bool) -> dict[str, Any]:
    return {
        "solicitation": row.get("solicitation_id") or row.get("notice_id"),
        "title": (row.get("title") or "")[:120],
        "manufacturer": commercial.get("manufacturer"),
        "model": commercial.get("model"),
        "mpn": commercial.get("mpn"),
        "identity_state": commercial.get("commercial_identity_state"),
        "phase_j_state": screen.get("identity_research_state"),
        "priceability": commercial.get("commercial_priceability"),
        "market_research_eligible": commercial.get("market_research_eligible"),
        "configuration": commercial.get("configuration_completeness"),
        "accept": accept,
        "rationale": commercial.get("market_research_reason") or commercial.get("drop_reason"),
        "bucket": commercial_bucket(commercial),
    }

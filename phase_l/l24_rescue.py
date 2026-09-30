"""Phase L.2.4 — exhaust history + current-price convergence on L.2.3 candidates."""

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
from phase_l.prebid_compliance import evaluate_prebid_compliance
from phase_l.product_fitness import classify_product_fitness

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _collect_eligible(rows: list[dict[str, Any]]) -> list[tuple]:
    """Rebuild the L.2.3 commercially researchable set (expect ~23)."""
    out: list[tuple] = []
    for row in rows:
        if str(row.get("our_bid_access") or "") != "YES":
            continue
        if not classify_deadline_freshness(row).get("deep_enrichment_allowed"):
            continue
        if not classify_product_fitness(row).get("market_price_research_allowed"):
            continue
        screen0 = stage_a_identity(row)
        screen = apply_commercial_overlay_to_screen(screen0, row)
        if not screen.get("market_research_eligible"):
            continue
        commercial = screen.get("commercial_identity") or {}
        pri = enrichment_priority(row, screen)
        score = int(pri.get("priority_score") or 0)
        if commercial.get("commercial_priceability") == "HIGH":
            score += 50
        st = commercial.get("commercial_identity_state") or ""
        if "COMMERCIAL" in st or "VEHICLE" in st or "EQUIPMENT" in st:
            score += 40
        if commercial.get("mpn"):
            score += 15
        # Prefer commercial brand/model over military MPN for reporting order
        if commercial.get("nsn") and not commercial.get("model"):
            score -= 20
        out.append((dict(row), screen, commercial, score))
    out.sort(key=lambda t: -t[3])
    return out


def run_phase_l24_convergence(
    rows: list[dict[str, Any]],
    *,
    authorize_live: bool = True,
    usaspending_max: int = 80,
    market_max: int = 160,
    expand_after_23: bool = False,
    expand_limit: int = 100,
) -> dict[str, Any]:
    """Force history + current price convergence on all L.2.3-eligible candidates."""
    cache = load_cache()
    budget = {
        "usaspending": 0,
        "usaspending_max": usaspending_max,
        "market": 0,
        "market_max": market_max,
    }

    eligible = _collect_eligible(rows)
    primary = eligible  # exhaust all eligible first (the ~23)
    funnel = Counter()
    funnel["market_research_eligible"] = len(eligible)
    funnel["primary_candidates"] = len(primary)

    queues: dict[str, list] = {
        QUEUE_PROFITABLE_ANY: [],
        QUEUE_PROFITABLE_10K: [],
        QUEUE_PROMISING_UNIT: [],
        QUEUE_NEGATIVE: [],
        QUEUE_INCOMPLETE: [],
    }
    candidate_rows: list[dict[str, Any]] = []
    states = Counter()
    code_counts = Counter()

    for i, (row, screen, commercial, score) in enumerate(primary):
        funnel["researched"] += 1
        title = (row.get("title") or "")[:70]
        print(
            f"[l24] {i+1}/{len(primary)} hist={budget['usaspending']}/{budget['usaspending_max']} "
            f"mkt={budget['market']}/{budget['market_max']} {title}",
            flush=True,
        )

        # Branch A — history (independent)
        history = lookup_government_history(
            row,
            screen.get("identity") or {},
            budget=budget,
            cache=cache,
            authorize_live=authorize_live,
        )
        hist_approaches = len(history.get("approach_logs") or history.get("query_approaches") or [])
        funnel["history_approaches_total"] += hist_approaches
        if history.get("attempted") or history.get("cache_hit"):
            funnel["history_attempted"] += 1
        if history.get("historical_award_unit_price") is not None:
            funnel["history_found"] += 1

        # Branch B — current price (independent; L.2.2 verification intact)
        market = lookup_current_market(
            row,
            screen.get("identity") or {},
            screen,
            budget=budget,
            cache=cache,
            authorize_live=authorize_live,
        )
        # Count source approaches roughly from queries + pages
        mkt_approaches = len((market.get("provider_attempts") or [])) + int(
            bool(market.get("pages_fetched"))
        )
        if market.get("queries"):
            mkt_approaches = max(mkt_approaches, min(5, len(market.get("queries") or [])))
        funnel["price_approaches_total"] += mkt_approaches
        if market.get("attempted") or market.get("cache_hit"):
            funnel["market_attempted"] += 1
        if market.get("public_retail_unit_price") is not None and market.get("economics_eligible", True):
            funnel["current_price_found_raw"] += 1
        funnel["pages_fetched"] += int(market.get("pages_fetched") or 0)
        funnel["product_pages_fetched"] += int(market.get("product_pages_fetched") or 0)

        joined = join_history_and_price(
            row=row,
            history=history,
            market=market,
            commercial=commercial,
            quantity=screen.get("quantity"),
        )
        state = joined["convergence_state"]
        states[state] += 1
        for c in joined.get("codes") or []:
            code_counts[c] += 1

        if state in {BOTH_FOUND_POSITIVE_SPREAD, BOTH_FOUND_NEGATIVE_SPREAD}:
            funnel["both_found"] += 1
        if state == BOTH_FOUND_POSITIVE_SPREAD:
            funnel["both_positive"] += 1
        if state == HISTORY_ONLY:
            funnel["history_only"] += 1
        if state == CURRENT_PRICE_ONLY:
            funnel["current_only"] += 1
        if state == NEITHER_FOUND:
            funnel["neither"] += 1
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
                if net >= 75000:
                    funnel["net_ge_75k"] += 1
                if net >= 100000:
                    funnel["net_ge_100k"] += 1

        enriched = apply_enrichment_to_row(
            row, screen=screen, priority=enrichment_priority(row, screen), history=history, market=market
        )
        # Apply access gate: strip inaccessible prices from economics fields
        if joined.get("price_access") in {"NO", "CONDITIONAL"} and not joined.get("current_unit"):
            enriched.pop("public_retail_unit_price", None)
            enriched.pop("public_retail_price", None)
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

        audit = {
            "solicitation": row.get("solicitation_id") or row.get("notice_id"),
            "title": (row.get("title") or "")[:120],
            "identity_state": commercial.get("commercial_identity_state"),
            "manufacturer": commercial.get("manufacturer"),
            "model": commercial.get("model"),
            "mpn": commercial.get("mpn"),
            "nsn": commercial.get("nsn"),
            "history_queries": history.get("keywords") or history.get("query_approaches"),
            "history_approaches": hist_approaches,
            "history_result": history.get("history_research_state"),
            "history_unit": joined.get("history_unit"),
            "history_confidence": joined.get("history_confidence_l24"),
            "price_queries": (market.get("queries") or [])[:6],
            "price_pages_fetched": market.get("pages_fetched"),
            "product_pages_fetched": market.get("product_pages_fetched"),
            "price_result": market.get("market_research_state"),
            "current_unit": joined.get("current_unit"),
            "price_access": joined.get("price_access"),
            "acquisition_price_type": joined.get("acquisition_price_type"),
            "price_url": (joined.get("acquisition") or {}).get("public_retail_source")
            or market.get("public_retail_source"),
            "configuration": commercial.get("configuration_completeness"),
            "freight": joined.get("freight"),
            "unit_spread": (joined.get("unit_economics") or {}).get("unit_raw_spread"),
            "expected_net": joined.get("expected_net_profit"),
            "convergence_state": state,
            "queue": joined.get("queue"),
            "codes": joined.get("codes"),
            "ready_to_bid": False,
        }
        candidate_rows.append(audit)
        queues[joined.get("queue") or QUEUE_INCOMPLETE].append(audit)

    # Optional expansion after exhausting primary set
    expanded_n = 0
    if expand_after_23 and len(primary) < expand_limit:
        # Already processed all eligible; nothing more without loosening gates
        expanded_n = 0

    save_cache(cache)

    researched = int(funnel.get("researched", 0)) or 1
    profitable = int(funnel.get("positive_total_profit", 0))
    rate = round(profitable / researched, 4)

    payload = {
        "kind": "PhaseL24ConvergenceResult",
        "phase": "L.2.4",
        "build": "20260927-m3-phase-l24-price-history-convergence",
        "generated_at": _utc(),
        "budget_used": {"usaspending": budget["usaspending"], "market": budget["market"]},
        "funnel": dict(funnel),
        "convergence_states": dict(states),
        "code_counts": code_counts.most_common(30),
        "candidate_table": candidate_rows,
        "queues": {k: v for k, v in queues.items()},
        "queue_counts": {k: len(v) for k, v in queues.items()},
        "counts": {
            "researched": int(funnel.get("researched", 0)),
            "history_found": int(funnel.get("history_found", 0)),
            "current_price_found": int(funnel.get("current_price_found_raw", 0)),
            "both_found": int(funnel.get("both_found", 0)),
            "both_positive": int(funnel.get("both_positive", 0)),
            "positive_unit_spread": int(funnel.get("positive_unit_spread", 0)),
            "positive_total_profit": int(funnel.get("positive_total_profit", 0)),
            "economics_completed": int(funnel.get("economics_completed", 0)),
            "net_ge_10k": int(funnel.get("net_ge_10k", 0)),
            "expanded": expanded_n,
        },
        "profitable_accessible_rate": {
            "researched": researched,
            "positive_expected_net": profitable,
            "ge_10k": int(funnel.get("net_ge_10k", 0)),
            "rate": rate,
        },
    }
    _write("l24_convergence_latest.json", payload)
    _write("l24_candidate_table.json", {"rows": candidate_rows})
    _write(
        "l24_queues.json",
        {
            "PROFITABLE_ANY_AMOUNT": queues[QUEUE_PROFITABLE_ANY],
            "PROFITABLE_GE_10K": queues[QUEUE_PROFITABLE_10K],
            "PROMISING_UNIT_ECONOMICS": queues[QUEUE_PROMISING_UNIT],
            "NEGATIVE_ECONOMICS": queues[QUEUE_NEGATIVE],
            "RESEARCH_INCOMPLETE": queues[QUEUE_INCOMPLETE],
        },
    )
    return payload

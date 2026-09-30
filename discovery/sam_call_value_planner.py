"""SamCallValuePlanner — allocate the shared 10-call daily SAM budget by value.

Extends discovery.sam_budgeted_client (does not replace it).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.sam_budgeted_client import (
    ART,
    BUILD as SAM_CLIENT_BUILD,
    SAM_SEARCH_URL,
    build_daily_query_plan,
    dashboard,
    execute_query_plan,
    load_ledger,
    query_fingerprint,
    reserve_calls,
    sam_daily_call_budget,
    search_opportunities,
)

BUILD = "20260929-m3-national-source-expansion-product-density-sam-budget-allocation"

CALL_DISCOVERY = "DISCOVERY"
CALL_DETAIL_ENRICHMENT = "DETAIL_ENRICHMENT"
CALL_AMENDMENT_CHECK = "AMENDMENT_CHECK"
CALL_DEADLINE_REFRESH = "DEADLINE_REFRESH"
CALL_PAGINATION = "PAGINATION"
CALL_RECOVERY = "RECOVERY"

DETAIL_CANDIDATES_PATH = ART / "sam_detail_call_candidates.json"
VALUE_PLAN_PATH = ART / "sam_call_value_plan.json"
DENSITY_PROFILE_PATH = ART / "sam_query_product_density_profile.json"


def _utc() -> str:
    return now_utc().isoformat()


def score_sam_call_candidate(cand: dict[str, Any]) -> float:
    """Higher = more deserving of a scarce credit."""
    score = 0.0
    score += float(cand.get("expected_unique_yield") or 0) * 0.05
    score += float(cand.get("expected_product_density") or 0) * 40.0
    if cand.get("already_high_priority"):
        score += 15.0
    if cand.get("missing_detail_blocks_promotion"):
        score += 20.0
    score += min(20.0, float(cand.get("deadline_urgency") or 0) * 2.0)
    score += min(15.0, float(cand.get("estimated_profit_potential") or 0) / 1000.0)
    if cand.get("equivalent_public_data_exists"):
        score -= 25.0
    if cand.get("cache_hit"):
        score -= 100.0  # never spend live for cache
    if cand.get("call_type") == CALL_DISCOVERY:
        score += float(cand.get("historical_unique_per_call") or 50) * 0.1
    return round(score, 2)


def build_value_plan(
    *,
    max_discovery: int = 5,
    max_detail: int = 3,
    min_reserve: int | None = None,
    detail_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Plan today's SAM calls without executing. Does not force spending."""
    dash = dashboard()
    limit = sam_daily_call_budget()
    used = int(dash["calls_used"])
    remaining = int(dash["calls_remaining"])
    res = min_reserve if min_reserve is not None else max(reserve_calls(), 1)
    available_for_spend = max(0, remaining - res)

    base = build_daily_query_plan(include_validation=used == 0)
    discovery_candidates: list[dict[str, Any]] = []
    for item in base.get("planned_calls") or []:
        if item.get("cache_status") == "HIT":
            continue
        cand = {
            "call_type": CALL_DISCOVERY if "page_0" in str(item.get("purpose")) or "validation" in str(item.get("purpose")) else CALL_PAGINATION,
            "purpose": item.get("purpose"),
            "params": item.get("params"),
            "fingerprint": item.get("fingerprint"),
            "expected_unique_yield": 800 if item.get("cache_status") != "HIT" else 0,
            "expected_product_density": 0.25,
            "historical_unique_per_call": 96.0,  # learned from prior run
            "cache_hit": item.get("cache_status") == "HIT",
            "already_high_priority": False,
            "missing_detail_blocks_promotion": False,
            "deadline_urgency": 5,
            "estimated_profit_potential": 0,
            "equivalent_public_data_exists": False,
        }
        cand["value_score"] = score_sam_call_candidate(cand)
        discovery_candidates.append(cand)

    discovery_candidates.sort(key=lambda x: -x["value_score"])

    detail_cands = []
    for d in detail_candidates or []:
        d = dict(d)
        d.setdefault("call_type", CALL_DETAIL_ENRICHMENT)
        d["value_score"] = score_sam_call_candidate(d)
        # Only if missing detail blocks and no public equivalent
        if d.get("missing_detail_blocks_promotion") and not d.get("equivalent_public_data_exists"):
            detail_cands.append(d)
    detail_cands.sort(key=lambda x: -x["value_score"])

    planned: list[dict[str, Any]] = []
    budget_left = available_for_spend

    # Cap discovery 3–5
    disc_budget = min(max_discovery, budget_left, 5)
    for cand in discovery_candidates:
        if disc_budget <= 0:
            break
        # Skip low-value discovery when prior same-day refresh already happened
        if used >= 2 and cand.get("value_score", 0) < 30:
            continue
        planned.append({**cand, "execute": True, "allocation": "discovery"})
        disc_budget -= 1
        budget_left -= 1

    # Detail 2–3 max
    det_budget = min(max_detail, budget_left, 3)
    for cand in detail_cands:
        if det_budget <= 0:
            break
        if cand.get("value_score", 0) < 25:
            continue
        planned.append({**cand, "execute": True, "allocation": "detail"})
        det_budget -= 1
        budget_left -= 1

    plan = {
        "kind": "SamCallValuePlan",
        "build": BUILD,
        "generated_at": _utc(),
        "daily_limit": limit,
        "calls_used": used,
        "calls_remaining": remaining,
        "reserve_held": res,
        "available_for_spend": available_for_spend,
        "planned_live_calls": len(planned),
        "planned": planned,
        "skipped_low_value_discovery": max(0, len(discovery_candidates) - sum(1 for p in planned if p["allocation"] == "discovery")),
        "detail_candidates_considered": len(detail_cands),
        "rule": "Do not spend credits just because available; prefer unused reserve",
        "envelope": {"discovery_max": 5, "detail_max": 3, "reserve_min": res},
    }
    VALUE_PLAN_PATH.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    return plan


def call_type_breakdown(ledger: dict[str, Any] | None = None) -> dict[str, int]:
    from collections import Counter

    led = ledger or load_ledger()
    day = dashboard()["date"]
    c = Counter()
    for e in led.get("entries") or []:
        if e.get("date") != day:
            continue
        if e.get("cache_hit"):
            continue
        if int(e.get("credits_consumed") or 0) <= 0 and not e.get("success"):
            # pending/blocked
            if "pending" in str(e.get("reason") or ""):
                continue
        reason = str(e.get("reason") or "")
        ctype = CALL_DISCOVERY
        if "detail" in reason.lower():
            ctype = CALL_DETAIL_ENRICHMENT
        elif "amend" in reason.lower():
            ctype = CALL_AMENDMENT_CHECK
        elif "page_" in reason or "pagination" in reason.lower():
            ctype = CALL_PAGINATION
        elif "recovery" in reason.lower():
            ctype = CALL_RECOVERY
        elif "validation" in reason.lower() or "window_" in reason or "primary_yield" in reason:
            ctype = CALL_DISCOVERY
        if int(e.get("credits_consumed") or 0) > 0 or e.get("success"):
            c[ctype] += int(e.get("credits_consumed") or (1 if e.get("success") else 0))
    return dict(c)


def enriched_dashboard() -> dict[str, Any]:
    d = dashboard()
    breakdown = call_type_breakdown()
    d["by_type"] = breakdown
    d["display_detailed"] = (
        f"SAM {d['calls_used']}/{d['daily_limit']} used today | "
        f"discovery:{breakdown.get(CALL_DISCOVERY, 0)} "
        f"detail:{breakdown.get(CALL_DETAIL_ENRICHMENT, 0)} "
        f"amendment:{breakdown.get(CALL_AMENDMENT_CHECK, 0)} "
        f"remaining:{d['calls_remaining']}"
    )
    return d


def build_detail_candidates(store: dict[str, dict[str, Any]], *, limit: int = 25) -> list[dict[str, Any]]:
    """High-value federal rows missing fields — candidates only; public-first required before spend."""
    cands = []
    for cid, rec in store.items():
        if rec.get("current_funnel_state") != "WATCH_FEDERAL_ACCESS":
            continue
        deep = rec.get("deep_research") or {}
        missing = []
        if not deep.get("quantity"):
            missing.append("quantity")
        if (deep.get("product_identity") or "WEAK") == "WEAK":
            missing.append("product_identity")
        if not rec.get("deadline"):
            missing.append("deadline")
        if not missing:
            continue
        title = str(rec.get("title") or "")
        # Only if looks product-ish
        from discovery.product_density import PRODUCT_LIKELY, PRODUCT_STRONG, classify_product_confidence

        conf = classify_product_confidence({"title": title, "description": rec.get("description")})
        if conf["product_confidence"] not in {PRODUCT_STRONG, PRODUCT_LIKELY}:
            continue
        # Public alternative: authoritative URL already present → try public first
        public_alt = bool(rec.get("authoritative_url"))
        cands.append(
            {
                "canonical_opportunity_id": cid,
                "solicitation": rec.get("solicitation_event_id"),
                "title": title[:120],
                "missing_field": ",".join(missing),
                "why_it_matters": "blocks supplier-path / call readiness research quality",
                "deadline": rec.get("deadline"),
                "priority_score": rec.get("priority_score") or 0,
                "public_alternatives_attempted": public_alt,
                "equivalent_public_data_exists": public_alt,
                "missing_detail_blocks_promotion": True,
                "already_high_priority": int(rec.get("priority_score") or 0) >= 50,
                "expected_unique_yield": 1,
                "expected_product_density": 1.0,
                "deadline_urgency": 8,
                "estimated_profit_potential": 5000,
                "call_type": CALL_DETAIL_ENRICHMENT,
                "expected_value_of_call": "medium — only if public URL fails",
            }
        )
    cands.sort(key=lambda x: (-int(x.get("priority_score") or 0), x.get("title") or ""))
    out = cands[:limit]
    DETAIL_CANDIDATES_PATH.write_text(json.dumps({"kind": "SamDetailCallCandidates", "build": BUILD, "candidates": out}, indent=2), encoding="utf-8")
    return out


def execute_value_plan(
    plan: dict[str, Any] | None = None,
    *,
    authorize_live: bool = False,
    transport: Any | None = None,
) -> dict[str, Any]:
    """Execute only planned discovery calls via canonical client. Detail calls gated separately."""
    plan = plan or build_value_plan()
    live = 0
    rows: list[dict[str, Any]] = []
    results = []
    for item in plan.get("planned") or []:
        if item.get("allocation") != "discovery":
            results.append({"item": item, "skipped": "detail_requires_public_first_fail"})
            continue
        params = item.get("params") or {}
        if not params:
            continue
        resp = search_opportunities(
            params,
            reason=f"discovery:{item.get('purpose')}",
            authorize_live=authorize_live,
            use_reserve=False,
            transport=transport,
        )
        meta = resp.get("_meta") or {}
        live += int(meta.get("credits_consumed") or 0)
        batch = list(resp.get("opportunitiesData") or [])
        rows.extend(batch)
        results.append({"purpose": item.get("purpose"), "meta": meta, "rows": len(batch)})
        if meta.get("blocked") or meta.get("status") == "SAM_DAILY_BUDGET_EXHAUSTED":
            break
        # Early stop empty
        if meta.get("status") == "LIVE_OK" and len(batch) == 0:
            break

    return {
        "kind": "SamValuePlanExecution",
        "build": BUILD,
        "live_calls": live,
        "records": len(rows),
        "opportunities": rows,
        "results": results,
        "dashboard": enriched_dashboard(),
        "plan_planned_live": plan.get("planned_live_calls"),
    }


def update_density_profile(fetched: list[dict[str, Any]], *, live_calls: int) -> dict[str, Any]:
    from discovery.product_density import classify_product_confidence, product_density

    annotated = []
    for r in fetched:
        conf = classify_product_confidence(
            {"title": r.get("title"), "description": r.get("description"), "naics": r.get("naicsCode")}
        )
        annotated.append({**conf, "noticeId": r.get("noticeId")})
    dens = product_density(annotated)
    hist: dict[str, Any] = {}
    if DENSITY_PROFILE_PATH.exists():
        try:
            hist = json.loads(DENSITY_PROFILE_PATH.read_text(encoding="utf-8"))
        except Exception:
            hist = {}
    hist.setdefault("kind", "SamQueryProductDensityProfile")
    hist.setdefault("runs", [])
    per = max(1, live_calls)
    hist["runs"].append(
        {
            "at": _utc(),
            "live_calls": live_calls,
            "total": dens["total"],
            "product_density": dens["product_density"],
            "counts": dens["counts"],
            "product_per_call": round(
                (dens["counts"].get("PRODUCT_STRONG", 0) + dens["counts"].get("PRODUCT_LIKELY", 0)) / per,
                2,
            ),
        }
    )
    hist["runs"] = hist["runs"][-40:]
    DENSITY_PROFILE_PATH.write_text(json.dumps(hist, indent=2), encoding="utf-8")
    return hist

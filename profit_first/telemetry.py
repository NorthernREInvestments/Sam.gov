"""Profit-first funnel telemetry and learning loop."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from application_clock import now_utc


def _path(name: str):
    from m3_data_root import data_path

    return data_path(name)


def record_disposition(opportunity_id: str, evaluation: dict[str, Any], *, disposition: str | None = None) -> dict[str, Any]:
    """Save what mattered when an opportunity reaches a disposition."""
    econ = evaluation.get("economics") or {}
    entry = {
        "recorded_at": now_utc().isoformat(),
        "opportunity_id": opportunity_id,
        "disposition": disposition or evaluation.get("route") or econ.get("profit_status"),
        "profit_status": econ.get("profit_status"),
        "expected_profit": econ.get("expected_profit"),
        "post_financing_profit": econ.get("post_financing_profit"),
        "gross_spread": econ.get("gross_spread"),
        "price_basis": econ.get("price_basis"),
        "proof_signals": econ.get("proof_signals"),
        "missing_facts": econ.get("missing_facts"),
        "freight": econ.get("freight"),
        "financing": econ.get("financing"),
        "product_class": (evaluation.get("product") or {}).get("class"),
        "ranking_score": (evaluation.get("ranking") or {}).get("profit_probability_score"),
        "title": evaluation.get("title"),
        "buyer": evaluation.get("buyer"),
        "lessons": _infer_lessons(evaluation),
    }
    path = _path("m3_profit_first_learning.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"kind": "ProfitFirstLearning", "events": []}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {"kind": "ProfitFirstLearning", "events": []}
    events = list(payload.get("events") or [])
    events.insert(0, entry)
    payload["events"] = events[:2000]
    payload["updated_at"] = now_utc().isoformat()
    payload["count"] = len(payload["events"])
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return entry


def _infer_lessons(evaluation: dict[str, Any]) -> list[str]:
    lessons: list[str] = []
    econ = evaluation.get("economics") or {}
    status = econ.get("profit_status")
    signals = econ.get("proof_signals") or []
    if "PROFITABLE_AT_PUBLIC_RETAIL" in signals:
        lessons.append("public_retail_already_worked")
    if status == "UNPROFITABLE" and econ.get("gross_spread") and (econ.get("gross_spread") or 0) > 0:
        if econ.get("freight") and econ.get("freight") > (econ.get("gross_spread") or 0):
            lessons.append("freight_killed_deal")
        if econ.get("financing") and (econ.get("post_freight_profit") or 0) > 0 and (econ.get("post_financing_profit") or 0) <= 0:
            lessons.append("financing_killed_deal")
    if status in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE"} and evaluation.get("line_item_ref"):
        lessons.append("multiline_aggregation_created_profit")
    if status == "EXECUTION_BLOCKED":
        lessons.append("execution_blocked_despite_economics")
    if status == "UNPROVEN" and "GOVERNMENT_VALUE" in (econ.get("missing_facts") or []):
        lessons.append("need_historical_value")
    if status == "UNPROVEN" and "ACQUISITION_COST" in (econ.get("missing_facts") or []):
        lessons.append("need_acquisition_cost")
    return lessons


def profit_funnel_telemetry(evaluations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Aggregate funnel counts from evaluations or stored index."""
    if evaluations is None:
        evaluations = _load_index_evals()
    status_c: Counter = Counter()
    route_c: Counter = Counter()
    retail_profit = 0
    both_sides = 0
    with_cost = 0
    with_gov = 0
    product_n = 0
    profits: list[float] = []
    by_buyer: Counter = Counter()
    by_portal: Counter = Counter()

    for ev in evaluations:
        product = ev.get("product") or {}
        if product.get("is_tangible_product"):
            product_n += 1
        econ = ev.get("economics") or {}
        st = str(econ.get("profit_status") or "UNPROVEN")
        status_c[st] += 1
        route_c[str(ev.get("route") or "")] += 1
        if econ.get("product_cost") is not None:
            with_cost += 1
        if econ.get("expected_revenue") is not None:
            with_gov += 1
        if econ.get("product_cost") is not None and econ.get("expected_revenue") is not None:
            both_sides += 1
        if "PROFITABLE_AT_PUBLIC_RETAIL" in (econ.get("proof_signals") or []):
            retail_profit += 1
        p = econ.get("post_financing_profit")
        if p is None:
            p = econ.get("expected_profit")
        if p is not None:
            profits.append(float(p))
        if ev.get("buyer"):
            if st in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE"}:
                by_buyer[str(ev.get("buyer"))] += 1
        portal = (ev.get("source_portal") or ev.get("platform") or "")
        if portal and st in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE"}:
            by_portal[str(portal)] += 1

    profits_sorted = sorted(profits)
    mid = profits_sorted[len(profits_sorted) // 2] if profits_sorted else None
    return {
        "kind": "ProfitFirstTelemetry",
        "updated_at": now_utc().isoformat(),
        "product_opportunities": product_n,
        "with_acquisition_cost": with_cost,
        "with_government_value": with_gov,
        "both_sides_known": both_sides,
        "profitable_at_public_retail": retail_profit,
        "by_profit_status": dict(status_c),
        "by_route": dict(route_c),
        "proven_profitable": status_c.get("PROVEN_PROFITABLE", 0),
        "likely_profitable": status_c.get("LIKELY_PROFITABLE", 0),
        "possible_profit": status_c.get("POSSIBLE_PROFIT", 0),
        "unproven": status_c.get("UNPROVEN", 0),
        "unprofitable": status_c.get("UNPROFITABLE", 0),
        "execution_blocked": status_c.get("EXECUTION_BLOCKED", 0),
        "average_expected_profit": round(sum(profits) / len(profits), 2) if profits else None,
        "median_expected_profit": mid,
        "total_potential_profit_identified": round(sum(p for p in profits if p > 0), 2) if profits else 0,
        "buyers_producing_profitable": dict(by_buyer.most_common(25)),
        "portals_producing_profitable": dict(by_portal.most_common(25)),
        "evaluated_count": len(evaluations),
    }


def _load_index_evals() -> list[dict[str, Any]]:
    path = _path("m3_profit_first_index.json")
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        by = data.get("by_opportunity") or {}
        return list(by.values())
    except Exception:
        return []


def save_evaluation_index(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    path = _path("m3_profit_first_index.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    by = {str(e.get("opportunity_id")): e for e in evaluations if e.get("opportunity_id")}
    payload = {
        "kind": "ProfitFirstIndex",
        "updated_at": now_utc().isoformat(),
        "count": len(by),
        "by_opportunity": by,
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    # also write telemetry snapshot
    telem = profit_funnel_telemetry(evaluations)
    tpath = _path("m3_profit_first_telemetry.json")
    tpath.write_text(json.dumps(telem, indent=2, default=str), encoding="utf-8")
    return payload

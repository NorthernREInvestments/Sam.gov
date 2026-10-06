"""Owner UI priority — quote reserve hidden by default.

Build: 20261004-m3-evidence-exhaustion-v1
"""

from __future__ import annotations

from typing import Any

from evidence_exhaustion.models import (
    DEFAULT_STRONG_LEAD_THRESHOLD,
    LENDER_READY,
    LIKELY_PROFITABLE,
    NEAR_READY_24H,
    OWNER_PIPELINE_PRIORITY,
    POSSIBLE_PROFIT,
    PROVEN_PROFITABLE,
    QUOTE_OUTREACH_RESERVE,
)


def count_strong_leads(opportunities: list[dict[str, Any]]) -> int:
    n = 0
    for o in opportunities:
        pipe = o.get("pipeline") or o.get("readiness") or o.get("profit_status")
        readiness = o.get("readiness") or (o.get("pipeline") or {}).get("readiness")
        profit = o.get("profit_status") or (o.get("pipeline") or {}).get("profit_status")
        if readiness in {LENDER_READY, NEAR_READY_24H}:
            n += 1
        elif profit in {PROVEN_PROFITABLE, LIKELY_PROFITABLE}:
            n += 1
    return n


def should_surface_quote_reserve(
    *,
    strong_lead_count: int,
    threshold: int = DEFAULT_STRONG_LEAD_THRESHOLD,
) -> bool:
    return strong_lead_count < threshold


def rank_reserve_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(c: dict[str, Any]) -> tuple:
        return (
            -float(c.get("expected_award_value") or c.get("solicitation_value") or 0),
            -float(c.get("possible_profit") or 0),
            -int(bool(c.get("open_reseller_channel"))),
            -int(c.get("bidder_count") or 0),
            -int(bool(c.get("prior_reseller_success"))),
            float(c.get("days_remaining") or 0),
            -int(bool(c.get("quote_ease"))),
        )

    return sorted(cases, key=key)


def owner_pipeline_view(
    opportunities: list[dict[str, Any]],
    *,
    reserve_cases: list[dict[str, Any]] | None = None,
    strong_threshold: int = DEFAULT_STRONG_LEAD_THRESHOLD,
) -> dict[str, Any]:
    """Default owner ordering; reserve collapsed unless pipeline thin."""
    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in OWNER_PIPELINE_PRIORITY}
    buckets.setdefault("OTHER", [])
    for o in opportunities:
        readiness = o.get("readiness") or (o.get("pipeline") or {}).get("readiness")
        profit = o.get("profit_status") or (o.get("pipeline") or {}).get("profit_status")
        status = o.get("terminal_status") or o.get("status")
        if readiness == LENDER_READY:
            buckets[LENDER_READY].append(o)
        elif readiness == NEAR_READY_24H:
            buckets[NEAR_READY_24H].append(o)
        elif profit == PROVEN_PROFITABLE or status == PROVEN_PROFITABLE:
            buckets[PROVEN_PROFITABLE].append(o)
        elif profit == LIKELY_PROFITABLE or status == LIKELY_PROFITABLE:
            buckets[LIKELY_PROFITABLE].append(o)
        elif profit == POSSIBLE_PROFIT or status == POSSIBLE_PROFIT:
            buckets[POSSIBLE_PROFIT].append(o)
        elif status == "HIGH_VALUE_RESEARCH_IN_PROGRESS" or o.get("research_in_progress"):
            buckets["HIGH_VALUE_RESEARCH_IN_PROGRESS"].append(o)
        elif status == QUOTE_OUTREACH_RESERVE:
            buckets[QUOTE_OUTREACH_RESERVE].append(o)
        else:
            buckets["OTHER"].append(o)

    reserve = reserve_cases or buckets.get(QUOTE_OUTREACH_RESERVE) or []
    strong = (
        len(buckets[LENDER_READY])
        + len(buckets[NEAR_READY_24H])
        + len(buckets[PROVEN_PROFITABLE])
        + len(buckets[LIKELY_PROFITABLE])
    )
    surface = should_surface_quote_reserve(strong_lead_count=strong, threshold=strong_threshold)
    ranked_reserve = rank_reserve_cases(reserve) if surface else []

    main_items: list[dict[str, Any]] = []
    for k in (
        LENDER_READY,
        NEAR_READY_24H,
        PROVEN_PROFITABLE,
        LIKELY_PROFITABLE,
        POSSIBLE_PROFIT,
        "HIGH_VALUE_RESEARCH_IN_PROGRESS",
    ):
        main_items.extend(buckets.get(k) or [])

    return {
        "kind": "OwnerPipelineView",
        "main_pipeline": main_items,
        "quote_outreach_reserve_count": len(reserve),
        "quote_outreach_reserve_collapsed": True,
        "quote_outreach_reserve_label": f"Quote Outreach Reserve: {len(reserve)}",
        "quote_outreach_reserve_surfaced": surface,
        "quote_outreach_reserve_items": ranked_reserve if surface else [],
        "strong_lead_count": strong,
        "strong_lead_threshold": strong_threshold,
        "buckets": {k: len(v) for k, v in buckets.items()},
    }

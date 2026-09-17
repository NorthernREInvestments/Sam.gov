"""Discovery funnel metrics service."""

from __future__ import annotations

from typing import Any


def build_discovery_funnel_metrics(session: Any) -> dict[str, Any]:
    """
    RAW → UNIQUE → CORE PRODUCT → … → WON
    Uses discovered opportunities + deal states. Zero external calls.
    """
    from models import Contract, DealState, DiscoveredOpportunity, DiscoveryRun

    total = session.query(DiscoveredOpportunity).count()
    core = session.query(DiscoveredOpportunity).filter_by(product_classification="CORE_PRODUCT").count()
    pps = session.query(DiscoveredOpportunity).filter_by(product_classification="PRODUCT_PLUS_SERVICE").count()
    unknown = session.query(DiscoveredOpportunity).filter_by(product_classification="UNKNOWN").count()
    service = session.query(DiscoveredOpportunity).filter_by(product_classification="SERVICE").count()
    rejected = session.query(DiscoveredOpportunity).filter_by(operator_status="REJECTED").count()
    research = session.query(DiscoveredOpportunity).filter_by(operator_status="RESEARCH_CANDIDATE").count()
    active = session.query(DiscoveredOpportunity).filter_by(operator_status="ACTIVE_DEAL").count()
    watch = session.query(DiscoveredOpportunity).filter_by(operator_status="WATCH").count()

    deals = session.query(DealState).all()
    deal_ready = sum(1 for d in deals if (d.funnel_checkpoint_json or {}).get("deal_ready") or d.pipeline_stage == "deal_ready")
    # Prefer readiness from checkpoint if present
    from deal_lifecycle import LIFECYCLE_BID_READY, LIFECYCLE_SUBMITTED, LIFECYCLE_AWARDED

    bid_ready = 0
    submitted = 0
    won = 0
    for d in deals:
        if d.decision == "BID" or d.pipeline_stage == "bid_ready":
            bid_ready += 1
        if d.decision in {"SUBMITTED"} or d.pipeline_stage == "submitted":
            submitted += 1
        if d.decision in {"AWARDED", "WON"}:
            won += 1

    last_run = session.query(DiscoveryRun).order_by(DiscoveryRun.id.desc()).first()

    return {
        "funnel": {
            "RAW_NOTICES": (last_run.raw_notices_seen if last_run else 0),
            "UNIQUE_OPPORTUNITIES": total,
            "CORE_PRODUCT": core,
            "PRODUCT_PLUS_SERVICE": pps,
            "UNKNOWN": unknown,
            "SERVICE": service,
            "ELIGIBLE": core + pps,  # cheap gate
            "RESEARCH_CANDIDATES": research,
            "ACTIVE_DEALS": active,
            "WATCH": watch,
            "REJECTED": rejected,
            "DEAL_READY": deal_ready,
            "BID_READY": bid_ready,
            "SUBMITTED": submitted,
            "WON": won,
        },
        "last_run_id": last_run.run_id if last_run else None,
        "note": "Funnel metrics for measuring path to ~5 qualified bids/day",
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
    }


def list_discovered_opportunities(
    session: Any,
    *,
    classification: str | None = None,
    operator_status: str | None = None,
    jurisdiction: str | None = None,
    buyer_type: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    from discovery.deadline_viability import enrich_opportunity_deadline, sort_operator_queue
    from models import DiscoveredOpportunity

    q = session.query(DiscoveredOpportunity)
    if classification:
        q = q.filter_by(product_classification=classification.upper())
    if operator_status:
        q = q.filter_by(operator_status=operator_status.upper())
    if jurisdiction:
        q = q.filter_by(jurisdiction=jurisdiction.upper())
    if buyer_type:
        q = q.filter_by(buyer_type=buyer_type.upper())
    rows = q.order_by(DiscoveredOpportunity.id.desc()).limit(max(limit * 3, 100)).all()
    items = []
    for r in rows:
        base = {
            "id": r.id,
            "title": r.title,
            "buyer": r.agency,
            "agency": r.agency,
            "jurisdiction": r.jurisdiction,
            "state_code": r.state_code,
            "deadline": r.deadline_raw or (r.response_deadline.isoformat() if r.response_deadline else None),
            "deadline_raw": r.deadline_raw,
            "response_deadline": r.response_deadline.isoformat() if r.response_deadline else None,
            "deadline_timezone": r.deadline_timezone,
            "deadline_tz_confidence": r.deadline_tz_confidence,
            "product_classification": r.product_classification,
            "research_priority": r.research_priority,
            "research_priority_label": r.research_priority_label,
            "source": r.preferred_source_id,
            "interesting_reason": r.interesting_reason,
            "obvious_blocker": r.obvious_blocker,
            "operator_status": r.operator_status,
            "contract_id": r.contract_id,
            "trust_tier": r.trust_tier,
            "raw_metadata": r.raw_metadata_json or {},
            "deadline_manual_override": (r.raw_metadata_json or {}).get("deadline_manual_override"),
            "deal_failed": bool(r.obvious_blocker) or r.operator_status == "REJECTED",
        }
        items.append(enrich_opportunity_deadline(base))

    queued = sort_operator_queue(items)
    ordered = queued["ordered_for_operator"][:limit]
    return {
        "opportunities": ordered,
        "count": len(ordered),
        "queue": {
            "needs_deadline_review": queued["needs_deadline_review"][:limit],
            "too_late": queued["too_late"][:limit],
            "rush_exceptions": queued["rush_exceptions"][:limit],
            "counts": queued["counts"],
        },
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
    }

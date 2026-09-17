"""Start Deal from discovered opportunity — source-agnostic downstream."""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any

from discovery.schema import CanonicalOpportunity


def discovered_to_canonical(row: Any) -> CanonicalOpportunity:
    return CanonicalOpportunity(
        external_id=row.external_id or str(row.id),
        source_id=row.preferred_source_id or "unknown",
        source_url=row.preferred_source_url,
        detail_url=row.detail_url,
        title=row.title,
        solicitation_number=row.solicitation_number,
        agency=row.agency,
        subagency=row.subagency,
        jurisdiction=row.jurisdiction,
        buyer_type=row.buyer_type,
        state_code=row.state_code,
        city=row.city,
        posted_date=row.posted_date,
        response_deadline=row.response_deadline,
        deadline_raw=row.deadline_raw,
        deadline_timezone=row.deadline_timezone,
        deadline_tz_confidence=row.deadline_tz_confidence,
        status=row.status,
        set_aside=row.set_aside,
        naics=row.naics,
        psc=row.psc,
        description=row.description,
        estimated_value=row.estimated_value,
        estimated_value_status=row.estimated_value_status or "UNKNOWN",
        document_links=list(row.document_links_json or []),
        amendment_links=list(row.amendment_links_json or []),
        trust_tier=row.trust_tier or 3,
        raw_metadata=dict(row.raw_metadata_json or {}),
    )


def start_deal_from_discovered(
    session: Any,
    discovered_id: int,
    *,
    operator: str | None = None,
) -> dict[str, Any]:
    """
    Promote discovered opportunity → Contract (+ DealState shell).
    Downstream workflow identical regardless of source.
    """
    from models import Contract, DealState, DiscoveredOpportunity

    row = session.query(DiscoveredOpportunity).filter_by(id=discovered_id).first()
    if not row:
        return {"error": "discovered_opportunity_not_found", "LIVE_API_REQUESTS": 0}

    if row.contract_id:
        return {
            "contract_id": row.contract_id,
            "discovered_id": row.id,
            "reused": True,
            "message": "Deal already linked",
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
            "SAM": 0,
        }

    canon = discovered_to_canonical(row)
    fields = canon.to_gt_contract_fields()
    notice = fields["notice_id"]

    existing = session.query(Contract).filter_by(notice_id=notice).first()
    if existing:
        row.contract_id = existing.id
        row.operator_status = "ACTIVE_DEAL"
        session.flush()
        return {
            "contract_id": existing.id,
            "discovered_id": row.id,
            "reused": True,
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
            "SAM": 0,
        }

    due = None
    if fields.get("due_date"):
        try:
            due = date_cls.fromisoformat(str(fields["due_date"])[:10])
        except ValueError:
            due = None

    contract = Contract(
        notice_id=notice,
        title=fields["title"],
        agency=fields.get("agency"),
        location=fields.get("location"),
        naics_code=fields.get("naics_code"),
        set_aside=fields.get("set_aside"),
        due_date=due,
        link=fields.get("link"),
        description=fields.get("description"),
        estimated_value=fields.get("estimated_value"),
        sam_raw=fields.get("sam_raw"),
        status="new",
    )
    session.add(contract)
    session.flush()

    deal = session.query(DealState).filter_by(contract_id=contract.id).first()
    if deal is None:
        core = "CORE_PRODUCT" if row.product_classification == "CORE_PRODUCT" else "UNKNOWN"
        deal = DealState(
            contract_id=contract.id,
            core_fit=core,
            pipeline_stage="discovered",
            decision=None,
            funnel_checkpoint_json={
                "discovery": {
                    "discovered_opportunity_id": row.id,
                    "source_id": row.preferred_source_id,
                    "classification": row.product_classification,
                    "research_priority": row.research_priority,
                    "operator": operator,
                    "buyer_type": row.buyer_type,
                    "jurisdiction": row.jurisdiction,
                }
            },
        )
        session.add(deal)

    row.contract_id = contract.id
    row.operator_status = "ACTIVE_DEAL"
    session.flush()
    return {
        "contract_id": contract.id,
        "discovered_id": row.id,
        "notice_id": contract.notice_id,
        "reused": False,
        "source_independent": True,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
    }


def set_operator_status(session: Any, discovered_id: int, status: str) -> dict[str, Any]:
    from models import DiscoveredOpportunity

    row = session.query(DiscoveredOpportunity).filter_by(id=discovered_id).first()
    if not row:
        return {"error": "not_found", "LIVE_API_REQUESTS": 0}
    allowed = {"NEW", "NEEDS_REVIEW", "RESEARCH_CANDIDATE", "REJECTED", "WATCH", "ACTIVE_DEAL"}
    st = status.upper()
    if st not in allowed:
        st = "NEEDS_REVIEW"
    row.operator_status = st
    session.flush()
    return {"id": row.id, "operator_status": st, "LIVE_API_REQUESTS": 0}

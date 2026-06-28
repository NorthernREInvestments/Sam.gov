"""Serialize sub records for API responses."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from models import Contract, ContractSub, Sub
from sub_constants import SUB_STATUSES


def _dec(value: Decimal | float | None) -> float | None:
    if value is None:
        return None
    return float(value)


def sub_to_dict(row: Sub, *, stats: dict[str, Any] | None = None, outreach: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "id": row.id,
        "place_id": row.place_id,
        "business_name": row.business_name,
        "phone": row.phone,
        "rating": _dec(row.rating),
        "review_count": row.review_count,
        "address": row.address,
        "city": row.city,
        "state": row.state,
        "zip": row.zip,
        "website": row.website,
        "google_maps_url": row.google_maps_url,
        "sub_type": row.sub_type,
        "notes": row.notes,
        "date_first_found": row.date_first_found.isoformat() if row.date_first_found else None,
        "date_last_updated": row.date_last_updated.isoformat() if row.date_last_updated else None,
    }
    if stats:
        payload.update(stats)
    if outreach:
        payload.update(outreach)
    return payload


def contract_sub_to_dict(link: ContractSub) -> dict[str, Any]:
    sub = link.sub
    return {
        "id": link.id,
        "contract_id": link.contract_id,
        "sub_id": link.sub_id,
        "status": link.status,
        "quote_amount": _dec(link.quote_amount),
        "quote_date": link.quote_date.isoformat() if link.quote_date else None,
        "contact_notes": link.contact_notes,
        "claude_score": link.claude_score,
        "claude_reason": link.claude_reason,
        "distance_miles": _dec(link.distance_miles),
        "date_status_updated": link.date_status_updated.isoformat() if link.date_status_updated else None,
        "date_added": link.date_added.isoformat() if link.date_added else None,
        "business_name": sub.business_name if sub else None,
        "phone": sub.phone if sub else None,
        "rating": _dec(sub.rating) if sub else None,
        "review_count": sub.review_count if sub else None,
        "address": sub.address if sub else None,
        "city": sub.city if sub else None,
        "state": sub.state if sub else None,
        "zip": sub.zip if sub else None,
        "website": sub.website if sub else None,
        "google_maps_url": sub.google_maps_url if sub else None,
        "sub_type": sub.sub_type if sub else None,
        "place_id": sub.place_id if sub else None,
        "is_selected": link.status == "Selected",
    }


def contract_sub_summary(contract: Contract, session) -> dict[str, Any]:
    from models import ContractSub

    links = session.query(ContractSub).filter_by(contract_id=contract.id).all()
    recommended = len(links)
    return {
        "count": len(links),
        "recommended_count": recommended,
        "radius_miles": contract.sub_search_radius_miles,
        "status": contract.sub_search_status or "none",
        "selected_sub_quote": _dec(contract.selected_sub_quote),
    }

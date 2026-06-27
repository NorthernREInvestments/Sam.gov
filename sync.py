"""Sync SAM.gov contract opportunities into PostgreSQL."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from database import SessionLocal
from models import Contract
from sam_client import fetch_opportunities


def _parse_due_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _fields_from_opportunity(opp: dict[str, Any]) -> dict[str, Any]:
    return {
        "notice_id": str(opp["notice_id"]),
        "title": (opp.get("title") or "Untitled")[:512],
        "agency": (opp.get("agency") or None),
        "location": (opp.get("location") or None),
        "naics_code": str(opp.get("naics_code") or "")[:16] or None,
        "set_aside": (opp.get("set_aside") or None),
        "due_date": _parse_due_date(opp.get("due_date")),
        "link": (opp.get("link") or None),
    }


def upsert_contracts(session: Session, opportunities: list[dict[str, Any]]) -> tuple[int, int]:
    """Insert new contracts or update existing ones. Preserves status and analysis."""
    new_count = 0
    updated_count = 0
    now = datetime.now(timezone.utc)

    for opp in opportunities:
        notice_id = str(opp.get("notice_id") or "")
        if not notice_id:
            continue

        fields = _fields_from_opportunity(opp)
        existing = session.query(Contract).filter_by(notice_id=notice_id).first()

        if existing:
            for key, value in fields.items():
                if key == "notice_id":
                    continue
                setattr(existing, key, value)
            existing.last_updated_at = now
            updated_count += 1
        else:
            session.add(Contract(**fields, status="new"))
            new_count += 1

    session.commit()
    return new_count, updated_count


def sync_from_sam() -> dict[str, Any]:
    """Pull from SAM.gov (1 API call) and upsert into the database."""
    opportunities, api_calls, fetch_status = fetch_opportunities()

    session = SessionLocal()
    try:
        new_count, updated_count = upsert_contracts(session, opportunities)
        total = session.query(Contract).count()
    finally:
        session.close()

    return {
        "api_calls": api_calls,
        "fetch_status": fetch_status,
        "synced_from_sam": len(opportunities),
        "new": new_count,
        "updated": updated_count,
        "total_in_db": total,
    }


def main() -> None:
    print("Syncing SAM.gov -> PostgreSQL...")
    result = sync_from_sam()
    print(f"Used {result['api_calls']} SAM.gov API call(s)")
    print(result["fetch_status"])
    print(
        f"Synced {result['synced_from_sam']} contract(s) - "
        f"{result['new']} new, {result['updated']} updated. "
        f"{result['total_in_db']} total in database."
    )


if __name__ == "__main__":
    main()

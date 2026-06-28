"""Sync SAM.gov contract opportunities into PostgreSQL."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from database import SessionLocal
from models import AppSetting, Contract
from sam_client import fetch_naics_from_sam, min_days_from_env, naics_from_env


def _parse_due_date(value: str | None) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _get_setting(session: Session, key: str, default: str = "") -> str:
    row = session.get(AppSetting, key)
    return row.value if row else default


def _set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row:
        row.value = value
    else:
        session.add(AppSetting(key=key, value=value))


def _fields_from_opportunity(opp: dict[str, Any]) -> dict[str, Any]:
    raw = opp.get("sam_raw") if isinstance(opp.get("sam_raw"), dict) else {}
    description = (
        raw.get("descriptionText")
        or raw.get("description")
        or opp.get("description")
    )
    estimated = raw.get("award") or raw.get("estimatedValue")
    if isinstance(estimated, dict):
        estimated = estimated.get("amount") or estimated.get("value")
    return {
        "notice_id": str(opp["notice_id"]),
        "title": (opp.get("title") or "Untitled")[:512],
        "agency": (opp.get("agency") or None),
        "location": (opp.get("location") or None),
        "naics_code": str(opp.get("naics_code") or "")[:16] or None,
        "set_aside": (opp.get("set_aside") or None),
        "due_date": _parse_due_date(opp.get("due_date")),
        "link": (opp.get("link") or None),
        "description": str(description)[:8000] if description else None,
        "estimated_value": str(estimated)[:128] if estimated else None,
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
        sam_raw = opp.get("sam_raw")
        existing = session.query(Contract).filter_by(notice_id=notice_id).first()

        if existing:
            for key, value in fields.items():
                if key == "notice_id":
                    continue
                setattr(existing, key, value)
            if isinstance(sam_raw, dict):
                existing.sam_raw = sam_raw
            existing.last_updated_at = now
            updated_count += 1
        else:
            session.add(Contract(**fields, sam_raw=sam_raw, status="new"))
            new_count += 1

    session.commit()
    return new_count, updated_count


def list_contracts(
    session: Session,
    naics_codes: list[str] | None = None,
    min_days_until_due: int | None = None,
    min_score: int | None = None,
    agency: str | None = None,
    pursue_only: bool = False,
) -> list[Contract]:
    """Return contracts from PostgreSQL matching filters."""
    from settings_store import get_min_score_threshold

    naics_codes = naics_codes or naics_from_env()
    min_days = min_days_until_due if min_days_until_due is not None else min_days_from_env()
    min_score = min_score if min_score is not None else get_min_score_threshold()
    naics_set = set(naics_codes)
    today = date.today()
    agency_query = agency.strip().lower() if agency else None

    rows = session.query(Contract).filter(Contract.naics_code.in_(naics_set)).all()
    results: list[Contract] = []
    for row in rows:
        if row.due_date is not None:
            days_left = (row.due_date - today).days
            if days_left < min_days:
                continue
        if agency_query and (not row.agency or agency_query not in row.agency.lower()):
            continue
        analysis = row.analysis or {}
        score = analysis.get("score")
        if score is not None and int(score) < min_score:
            continue
        if pursue_only and analysis.get("pursue") is not True:
            continue
        results.append(row)

    results.sort(
        key=lambda r: (
            r.due_date is None,
            (r.due_date - today).days if r.due_date else 9999,
        )
    )
    return results


def contract_to_dict(row: Contract) -> dict[str, Any]:
    from naics_labels import naics_display, naics_label

    today = date.today()
    days_left = (row.due_date - today).days if row.due_date else None
    analysis = row.analysis or {}
    sam_raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
    attachments = sam_raw.get("opportunityAttachments")
    if isinstance(attachments, list):
        doc_access = sam_raw.get("documentAccess") or {}
        external_links = sam_raw.get("opportunityLinks") or []
        sam_attachments = attachments
    else:
        doc_access = {}
        external_links = []
        sam_attachments = []
    return {
        "notice_id": row.notice_id,
        "title": row.title,
        "agency": row.agency,
        "location": row.location,
        "naics_code": row.naics_code,
        "naics_label": naics_label(row.naics_code),
        "naics_display": naics_display(row.naics_code),
        "set_aside": row.set_aside,
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "days_until_due": days_left,
        "link": row.link,
        "estimated_value": row.estimated_value,
        "description": row.description,
        "status": row.status,
        "analysis": analysis,
        "pursue": analysis.get("pursue"),
        "score": analysis.get("score"),
        "reason": analysis.get("reason"),
        "plain_english_summary": analysis.get("plain_english_summary") or analysis.get("executive_summary"),
        "executive_summary": analysis.get("executive_summary"),
        "pricing_intelligence": analysis.get("pricing_intelligence"),
        "pricing_intel": row.pricing_intel,
        "sub_type_needed": analysis.get("sub_type_needed"),
        "red_flags": analysis.get("red_flags") or [],
        "document_access": doc_access,
        "external_links": external_links,
        "sam_attachments": sam_attachments,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_updated_at": row.last_updated_at.isoformat() if row.last_updated_at else None,
    }


def refresh_stale_sam_raw(session: Session, limit: int | None = None) -> int:
    """Refresh SAM.gov attachment metadata for contracts missing the v3 attachment list."""
    from api_budget import can_spend_sam, enrich_on_sync_limit
    from sam_enrich import (
        enrich_opportunity,
        fetch_opportunity_raw,
        needs_attachment_refresh,
        refresh_opportunity_attachments,
    )

    if limit is None:
        limit = enrich_on_sync_limit()
    limit = max(0, limit)

    rows = session.query(Contract).order_by(Contract.last_updated_at.desc()).limit(250).all()
    refreshed = 0
    for row in rows:
        if refreshed >= limit:
            break
        raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
        if not needs_attachment_refresh(raw):
            continue

        notice_id = str(row.notice_id or raw.get("noticeId") or "")
        if not notice_id:
            continue

        if not raw or not raw.get("noticeId"):
            if not can_spend_sam(1):
                break
            fresh = fetch_opportunity_raw(notice_id)
            raw = fresh or raw

        if not raw:
            continue

        if raw.get("descriptionText") or raw.get("descriptionHtml"):
            if not can_spend_sam(1):
                break
            raw = refresh_opportunity_attachments(raw)
            if not raw.get("descriptionText") and can_spend_sam(2):
                raw = enrich_opportunity(raw)
        else:
            if not can_spend_sam(2):
                break
            raw = enrich_opportunity(raw)

        row.sam_raw = raw
        if raw.get("descriptionText"):
            row.description = raw["descriptionText"][:8000]
        refreshed += 1

    if refreshed:
        session.commit()
    return refreshed


def sync_from_sam() -> dict[str, Any]:
    """Pull one NAICS code from SAM.gov (1 API call) and upsert into PostgreSQL."""
    naics_codes = naics_from_env()
    session = SessionLocal()
    intake_result: dict[str, Any] = {}
    try:
        index = int(_get_setting(session, "naics_rotation_index", "0")) % len(naics_codes)
        naics_today = naics_codes[index]

        batch = fetch_naics_from_sam(naics_today)
        new_count, updated_count = upsert_contracts(session, batch)
        batch_notice_ids = [str(o.get("notice_id") or "") for o in batch if o.get("notice_id")]
        from intake import intake_matching_contracts

        intake_result = intake_matching_contracts(session, batch_notice_ids)

        synced_map = json.loads(_get_setting(session, "naics_last_synced", "{}"))
        synced_map[naics_today] = date.today().isoformat()
        _set_setting(session, "naics_last_synced", json.dumps(synced_map))
        _set_setting(session, "naics_rotation_index", str((index + 1) % len(naics_codes)))
        session.commit()

        total = session.query(Contract).count()
        loaded = len(synced_map)
        next_naics = naics_codes[(index + 1) % len(naics_codes)]
        fetch_status = (
            f"Searched NAICS {naics_today} ({index + 1} of {len(naics_codes)}). "
            f"Fetched {len(batch)} from SAM.gov. Coverage: {loaded}/{len(naics_codes)} NAICS codes."
        )
        if loaded < len(naics_codes):
            fetch_status += f" Run sync again for {next_naics}, or use --all."
    finally:
        session.close()

    from api_budget import get_usage_snapshot, intake_on_sync_enabled
    from intake import start_background_intake

    if intake_on_sync_enabled():
        start_background_intake()

    return {
        "api_calls": 1,
        "fetch_status": fetch_status,
        "fetched_from_sam": len(batch),
        "new": new_count,
        "updated": updated_count,
        "intake": intake_result,
        "total_in_db": total,
        "naics_synced": loaded,
        "naics_total": len(naics_codes),
        "api_budget": get_usage_snapshot(),
    }


def sync_all_naics() -> dict[str, Any]:
    """Pull every configured NAICS code (1 API call each) and upsert into PostgreSQL."""
    naics_codes = naics_from_env()
    api_calls = 0
    fetched_total = 0
    new_total = 0
    updated_total = 0
    per_naics: list[dict[str, Any]] = []
    synced_map: dict[str, str] = {}
    intake_result: dict[str, Any] = {}
    all_notice_ids: list[str] = []

    status_session = SessionLocal()
    try:
        synced_map = json.loads(_get_setting(status_session, "naics_last_synced", "{}"))
    finally:
        status_session.close()

    for naics in naics_codes:
        batch = fetch_naics_from_sam(naics)
        api_calls += 1
        fetched_total += len(batch)
        all_notice_ids.extend(str(o.get("notice_id") or "") for o in batch if o.get("notice_id"))

        session = SessionLocal()
        try:
            new_count, updated_count = upsert_contracts(session, batch)
            synced_map[naics] = date.today().isoformat()
            _set_setting(session, "naics_last_synced", json.dumps(synced_map))
            session.commit()
        finally:
            session.close()

        new_total += new_count
        updated_total += updated_count
        per_naics.append({"naics": naics, "fetched": len(batch), "new": new_count, "updated": updated_count})

    session = SessionLocal()
    try:
        _set_setting(session, "naics_rotation_index", "0")
        from intake import intake_matching_contracts

        intake_result = intake_matching_contracts(session, all_notice_ids)
        session.commit()
        total = session.query(Contract).count()
    finally:
        session.close()

    fetch_status = (
        f"Synced all {len(naics_codes)} NAICS codes. "
        f"Fetched {fetched_total} opportunities from SAM.gov."
    )

    from api_budget import get_usage_snapshot, intake_on_sync_enabled
    from intake import start_background_intake

    if intake_on_sync_enabled():
        start_background_intake()

    return {
        "api_calls": api_calls,
        "fetch_status": fetch_status,
        "fetched_from_sam": fetched_total,
        "new": new_total,
        "updated": updated_total,
        "intake": intake_result,
        "total_in_db": total,
        "naics_synced": len(naics_codes),
        "naics_total": len(naics_codes),
        "per_naics": per_naics,
        "api_budget": get_usage_snapshot(),
    }


def get_naics_sync_status() -> dict[str, Any]:
    naics_codes = naics_from_env()
    session = SessionLocal()
    try:
        synced_map = json.loads(_get_setting(session, "naics_last_synced", "{}"))
    finally:
        session.close()
    return {
        "naics_codes": naics_codes,
        "last_synced": synced_map,
        "synced_count": sum(1 for code in naics_codes if code in synced_map),
        "total_count": len(naics_codes),
    }


def main() -> None:
    import sys

    all_naics = "--all" in sys.argv
    print("Syncing SAM.gov -> PostgreSQL...")
    result = sync_all_naics() if all_naics else sync_from_sam()
    print(f"Used {result['api_calls']} SAM.gov API call(s)")
    print(result["fetch_status"])
    print(
        f"Saved to database - {result['new']} new, {result['updated']} updated. "
        f"{result['total_in_db']} total in database."
    )
    if result.get("per_naics"):
        for row in result["per_naics"]:
            print(f"  NAICS {row['naics']}: {row['fetched']} fetched, {row['new']} new")


if __name__ == "__main__":
    main()

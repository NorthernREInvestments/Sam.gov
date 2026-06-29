"""Sync SAM.gov contract opportunities into PostgreSQL."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from database import SessionLocal
from models import AppSetting, Contract
from sam_client import fetch_naics_from_sam, min_days_from_env, naics_from_env

SCHEDULED_ROTATION_KEY = "naics_scheduled_rotation_index"
FOCUS_NAICS_KEY = "sam_focus_naics"


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
    from naics_labels import naics_tier

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
        "tier": naics_tier(opp.get("naics_code")),
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
                from sam_enrich import is_scrape_complete

                existing_raw = existing.sam_raw if isinstance(existing.sam_raw, dict) else {}
                if is_scrape_complete(existing_raw) and not is_scrape_complete(sam_raw):
                    pass
                else:
                    existing.sam_raw = sam_raw
            existing.last_updated_at = now
            updated_count += 1
        else:
            session.add(Contract(**fields, sam_raw=sam_raw, status="new"))
            new_count += 1

    session.commit()
    return new_count, updated_count


def opportunity_passes_filters(
    opp: dict[str, Any],
    *,
    naics_codes: list[str] | None = None,
    min_days_until_due: int | None = None,
    min_score: int | None = None,
    analysis: dict[str, Any] | None = None,
) -> bool:
    """Same matching rules as the dashboard — apply before spending SAM credits on attachments."""
    from settings_store import get_min_score_threshold

    naics_set = set(naics_codes or naics_from_env())
    min_days = min_days_until_due if min_days_until_due is not None else min_days_from_env()
    min_score_threshold = min_score if min_score is not None else get_min_score_threshold()

    code = str(opp.get("naics_code") or "").strip()
    if code and code not in naics_set:
        return False

    due = _parse_due_date(opp.get("due_date"))
    if due is not None:
        if (due - date.today()).days < min_days:
            return False

    if analysis:
        score = analysis.get("score")
        if score is not None and int(score) < min_score_threshold:
            return False

    return True


def filter_search_results(
    opportunities: list[dict[str, Any]],
    session: Session | None = None,
    *,
    naics_codes: list[str] | None = None,
    min_days_until_due: int | None = None,
    min_score: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Drop search hits that fail dashboard criteria before attachment scrape."""
    analysis_by_notice: dict[str, dict[str, Any]] = {}
    if session is not None:
        notice_ids = [str(o.get("notice_id") or "") for o in opportunities if o.get("notice_id")]
        if notice_ids:
            rows = session.query(Contract).filter(Contract.notice_id.in_(notice_ids)).all()
            analysis_by_notice = {
                row.notice_id: row.analysis
                for row in rows
                if isinstance(row.analysis, dict)
            }

    matched: list[dict[str, Any]] = []
    for opp in opportunities:
        notice_id = str(opp.get("notice_id") or "")
        if opportunity_passes_filters(
            opp,
            naics_codes=naics_codes,
            min_days_until_due=min_days_until_due,
            min_score=min_score,
            analysis=analysis_by_notice.get(notice_id),
        ):
            matched.append(opp)

    return matched, {
        "search_results": len(opportunities),
        "matched_filters": len(matched),
        "filtered_out": len(opportunities) - len(matched),
    }


def list_contracts(
    session: Session,
    naics_codes: list[str] | None = None,
    min_days_until_due: int | None = None,
    min_score: int | None = None,
    agency: str | None = None,
    pursue_only: bool = False,
    tier: int | None = None,
    require_scrape_complete: bool = False,
    require_dashboard_ready: bool = True,
    notice_ids: list[str] | None = None,
) -> list[Contract]:
    """Return contracts matching dashboard search filters.

    By default only rows with attachments downloaded, PDFs read, and PWS scope extracted.
    """
    from screening_pipeline import is_dashboard_ready
    from settings_store import get_min_score_threshold

    if naics_codes is None:
        naics_codes = naics_from_env()
    if not naics_codes:
        return []
    min_days = min_days_until_due if min_days_until_due is not None else min_days_from_env()
    min_score = min_score if min_score is not None else get_min_score_threshold()
    naics_set = set(naics_codes)
    id_set = set(notice_ids) if notice_ids else None
    today = date.today()
    agency_query = agency.strip().lower() if agency else None

    rows = session.query(Contract).filter(Contract.naics_code.in_(naics_set)).all()
    results: list[Contract] = []
    for row in rows:
        if id_set is not None and row.notice_id not in id_set:
            continue
        if tier is not None and row.tier != tier:
            continue
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
        if require_dashboard_ready:
            if not is_dashboard_ready(row):
                continue
        elif require_scrape_complete:
            from sam_enrich import is_scrape_complete

            raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
            if not is_scrape_complete(raw):
                continue
        results.append(row)

    results.sort(
        key=lambda r: (
            0
            if (r.analysis or {}).get("screening_stage") == "full"
            and (r.analysis or {}).get("score") is not None
            else 1,
            -int((r.analysis or {}).get("score") or 0),
            r.due_date is None,
            (r.due_date - today).days if r.due_date else 9999,
        )
    )
    return results


def contract_to_dict(row: Contract) -> dict[str, Any]:
    from naics_labels import naics_display, naics_label, tier_label

    today = date.today()
    days_left = (row.due_date - today).days if row.due_date else None
    analysis = row.analysis if isinstance(row.analysis, dict) else {}
    sam_raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
    from sam_enrich import is_scrape_complete

    clearance_flag = analysis.get("security_clearance_required")
    if clearance_flag is True:
        security_clearance_required = True
    elif clearance_flag is False:
        security_clearance_required = False
    else:
        from comparable_scope import detect_clearance_level

        security_clearance_required = bool(detect_clearance_level(row.description or ""))
    attachments = sam_raw.get("opportunityAttachments")
    piee_attachments = sam_raw.get("pieeAttachments") if isinstance(sam_raw.get("pieeAttachments"), list) else []
    if isinstance(attachments, list) and attachments:
        doc_access = sam_raw.get("documentAccess") or {}
        external_links = sam_raw.get("opportunityLinks") or []
        sam_attachments = attachments + piee_attachments
    elif piee_attachments:
        doc_access = sam_raw.get("documentAccess") or {}
        external_links = sam_raw.get("opportunityLinks") or []
        sam_attachments = piee_attachments
    else:
        sam_attachments = analysis.get("sam_attachments") or []
        doc_access = sam_raw.get("documentAccess") or analysis.get("document_access") or {}
        external_links = sam_raw.get("opportunityLinks") or analysis.get("external_links") or []

    from sub_serializers import contract_sub_summary

    session = SessionLocal()
    try:
        sub_summary = contract_sub_summary(row, session)
        from sub_finder import nearby_network_subs
        from usaspending_client import extract_work_location

        work = extract_work_location(
            row.location,
            row.sam_raw if isinstance(row.sam_raw, dict) else None,
        )
        sub_summary["city"] = work.get("city") or work.get("label")
        try:
            network = nearby_network_subs(session, row.notice_id)
            nearby_network_count = network.get("count", 0)
        except Exception:
            nearby_network_count = 0
        from workflow_status import compute_card_pipeline, compute_workflow_status

        workflow = compute_workflow_status(row, session)
        pipeline = compute_card_pipeline(row, session)
    finally:
        session.close()

    selected_quote = float(row.selected_sub_quote) if row.selected_sub_quote is not None else None
    from proposal_defaults import resolve_contract_margin

    effective_margin = resolve_contract_margin(row)
    estimated_annual_bid = None
    if selected_quote and selected_quote > 0:
        estimated_annual_bid = round(selected_quote / (1 - effective_margin / 100.0), 2)
    from pws_fields import pws_snapshot

    pws = pws_snapshot(row)
    return {
        "notice_id": row.notice_id,
        "title": row.title,
        "agency": row.agency,
        "location": row.location,
        "naics_code": row.naics_code,
        "naics_label": naics_label(row.naics_code),
        "naics_display": naics_display(row.naics_code),
        "tier": row.tier,
        "tier_label": tier_label(row.tier),
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
        "text_score": analysis.get("text_score") or analysis.get("score"),
        "screening_stage": analysis.get("screening_stage") or ("full" if analysis.get("plain_english_summary") else None),
        "skip_reason": analysis.get("skip_reason"),
        "reason": analysis.get("reason"),
        "plain_english_summary": analysis.get("plain_english_summary") or analysis.get("executive_summary"),
        "executive_summary": analysis.get("executive_summary"),
        "pricing_intelligence": analysis.get("pricing_intelligence"),
        "pricing_intel": row.pricing_intel,
        "pws": pws,
        "square_footage": row.square_footage,
        "building_type": row.building_type,
        "cleaning_frequency_per_week": float(row.cleaning_frequency_per_week)
        if row.cleaning_frequency_per_week is not None
        else None,
        "awarded_amount": float(row.awarded_amount) if row.awarded_amount is not None else None,
        "sub_type_needed": analysis.get("sub_type_needed"),
        "sub_summary": sub_summary,
        "nearby_network_count": nearby_network_count,
        "selected_sub_quote": selected_quote,
        "margin_percentage": float(row.margin_percentage) if row.margin_percentage is not None else None,
        "effective_margin_pct": effective_margin,
        "estimated_annual_bid": estimated_annual_bid,
        "sub_search_status": row.sub_search_status,
        "red_flags": analysis.get("red_flags") or [],
        "security_clearance_required": security_clearance_required,
        "document_access": doc_access,
        "external_links": external_links,
        "sam_attachments": sam_attachments,
        "scrape_complete": is_scrape_complete(sam_raw),
        "workflow": workflow,
        "pipeline": pipeline,
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


def get_focus_naics(session: Session) -> str | None:
    code = _get_setting(session, FOCUS_NAICS_KEY).strip()
    return code or None


def sync_from_sam(naics_code: str | None = None, *, search_only: bool = False) -> dict[str, Any]:
    """Pull one enabled NAICS code from SAM.gov, save filter-matching contracts, enrich attachments, run Claude."""
    naics_codes = naics_from_env()
    if not naics_codes:
        raise ValueError("No NAICS codes enabled — turn on at least one code in Settings.")
    session = SessionLocal()
    intake_result: dict[str, Any] = {}
    scrape_result: dict[str, Any] = {}
    filter_stats: dict[str, int] = {}
    search_count = 0
    new_count = 0
    updated_count = 0
    total = 0
    loaded = 0
    fetch_status = ""
    try:
        index = int(_get_setting(session, "naics_rotation_index", "0")) % len(naics_codes)
        if naics_code:
            naics_today = str(naics_code).strip()
            if naics_today not in naics_codes:
                raise ValueError(f"NAICS {naics_today} is not in your configured list.")
        else:
            naics_today = naics_codes[index]

        batch = fetch_naics_from_sam(naics_today)
        search_count = len(batch)
        batch, filter_stats = filter_search_results(batch, session)
        batch_notice_ids: list[str] = []
        scrape_result: dict[str, Any] = {"scraped_complete": 0, "scraped_skipped": 0}
        intake_result: dict[str, Any] = {}

        if search_only:
            new_count, updated_count = upsert_contracts(session, batch)
            batch_notice_ids = [str(o.get("notice_id") or "") for o in batch if o.get("notice_id")]
        else:
            from api_budget import attachment_enrich_per_sync_limit
            from intake import enrich_matching_attachments, intake_matching_contracts

            batch_ids = [str(o.get("notice_id") or "") for o in batch if o.get("notice_id")]
            new_count, updated_count = upsert_contracts(session, batch)
            enrich_limit = attachment_enrich_per_sync_limit()
            scrape_result = enrich_matching_attachments(
                session,
                batch_ids,
                limit=enrich_limit,
                naics_code=naics_today if enrich_limit is None else None,
            )
            if enrich_limit is None:
                _set_setting(session, FOCUS_NAICS_KEY, naics_today)
            batch_notice_ids = batch_ids
            intake_result = intake_matching_contracts(session, batch_notice_ids)

        synced_map = json.loads(_get_setting(session, "naics_last_synced", "{}"))
        synced_map[naics_today] = date.today().isoformat()
        _set_setting(session, "naics_last_synced", json.dumps(synced_map))
        if not naics_code:
            _set_setting(session, "naics_rotation_index", str((index + 1) % len(naics_codes)))
        session.commit()

        total = session.query(Contract).count()
        loaded = len(synced_map)
        next_naics = naics_codes[(index + 1) % len(naics_codes)]
        filter_note = ""
        if filter_stats.get("filtered_out"):
            filter_note = (
                f" {filter_stats['filtered_out']} search result(s) dropped "
                f"(failed min-days/NAICS/score filters before attachment scrape)."
            )
        if search_only:
            fetch_status = (
                f"Search only — NAICS {naics_today}. "
                f"{filter_stats.get('matched_filters', len(batch))} matching opportunities "
                f"from {search_count} SAM result(s) (1 API call).{filter_note} "
                f"Attachments were not scraped."
            )
        else:
            fetch_status = (
                f"NAICS {naics_today}: saved {filter_stats.get('matched_filters', len(batch))} matching contract(s) "
                f"from {search_count} SAM result(s) (1 search call).{filter_note} "
                f"Attachments ready: {scrape_result.get('attachments_enriched', 0)}; "
                f"pending: {scrape_result.get('attachments_pending', 0)}. "
                f"Claude analysis runs when attachments are complete."
            )
            if loaded < len(naics_codes):
                fetch_status += f" Coverage: {loaded}/{len(naics_codes)} NAICS codes."
                fetch_status += f" Next rotation: {next_naics}."
    finally:
        session.close()

    from api_budget import get_usage_snapshot, intake_on_sync_enabled
    from intake import start_background_attachment_enrich, start_background_intake

    if not search_only:
        if intake_on_sync_enabled():
            start_background_intake()
        start_background_attachment_enrich()

    return {
        "api_calls": 1,
        "naics_code": naics_today,
        "search_only": search_only,
        "fetch_status": fetch_status,
        "fetched_from_sam": search_count,
        "filter": filter_stats,
        "new": new_count,
        "updated": updated_count,
        "scrape": scrape_result,
        "intake": intake_result,
        "total_in_db": total,
        "naics_synced": loaded,
        "naics_total": len(naics_codes),
        "api_budget": get_usage_snapshot(),
    }


def _rotated_code_batch(codes: list[str], index: int, count: int) -> tuple[list[str], int]:
    """Pick the next `count` codes from a rotating pool."""
    if not codes or count <= 0:
        return [], index
    selected = [codes[(index + i) % len(codes)] for i in range(min(count, len(codes)))]
    next_index = (index + len(selected)) % len(codes)
    return selected, next_index


def _pending_scrape_notice_ids(session: Session, naics_code: str) -> list[str]:
    """Filter-matching contracts for this NAICS that still need SAM attachment scrape."""
    from sam_enrich import is_scrape_complete

    rows = list_contracts(session, require_scrape_complete=False, naics_codes=[naics_code])
    return [
        row.notice_id
        for row in rows
        if not is_scrape_complete(row.sam_raw if isinstance(row.sam_raw, dict) else {})
    ]


def _sync_naics_code_list(
    naics_codes: list[str],
    *,
    scheduled_tiers: list[int] | None = None,
    manual_all_tiers: bool = False,
    use_all_sam_budget_for_attachments: bool = False,
) -> dict[str, Any]:
    """Pull a list of NAICS codes from SAM.gov and run two-step intake."""
    if not naics_codes:
        raise ValueError("No NAICS codes enabled — turn on at least one code in Settings.")

    api_calls = 0
    fetched_total = 0
    matched_total = 0
    filtered_total = 0
    scraped_total = 0
    scrape_skipped_total = 0
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

    from intake import intake_matching_contracts
    from api_budget import can_spend_sam

    budget_skipped: list[str] = []
    for naics in naics_codes:
        if not can_spend_sam(1):
            budget_skipped.extend(naics_codes[len(per_naics):])
            break
        batch = fetch_naics_from_sam(naics)
        api_calls += 1
        fetched_total += len(batch)

        session = SessionLocal()
        filter_stats: dict[str, int] = {}
        scrape_result: dict[str, Any] = {"scraped_complete": 0, "scraped_skipped": 0}
        attach_result: dict[str, Any] = {"attachments_enriched": 0, "attachments_pending": 0}
        new_count = 0
        updated_count = 0
        try:
            batch, filter_stats = filter_search_results(batch, session)
            matched_total += filter_stats.get("matched_filters", len(batch))
            filtered_total += filter_stats.get("filtered_out", 0)

            batch_ids = [str(o.get("notice_id") or "") for o in batch if o.get("notice_id")]
            new_count, updated_count = upsert_contracts(session, batch)

            from api_budget import attachment_enrich_per_sync_limit
            from intake import enrich_matching_attachments

            enrich_limit = None if use_all_sam_budget_for_attachments else attachment_enrich_per_sync_limit()
            attach_result = enrich_matching_attachments(
                session,
                batch_ids,
                limit=enrich_limit,
                naics_code=naics if use_all_sam_budget_for_attachments else None,
            )
            scraped_total += attach_result.get("attachments_enriched", 0)
            scrape_skipped_total += attach_result.get("attachments_pending", 0)

            all_notice_ids.extend(batch_ids)
            synced_map[naics] = date.today().isoformat()
            _set_setting(session, "naics_last_synced", json.dumps(synced_map))
            session.commit()
        finally:
            session.close()

        new_total += new_count
        updated_total += updated_count
        per_naics.append({
            "naics": naics,
            "fetched": len(batch) + filter_stats.get("filtered_out", 0),
            "matched_filters": filter_stats.get("matched_filters", len(batch)),
            "filtered_out": filter_stats.get("filtered_out", 0),
            "attachments_enriched": attach_result.get("attachments_enriched", 0),
            "attachments_pending": attach_result.get("attachments_pending", 0),
            "new": new_count,
            "updated": updated_count,
        })

    session = SessionLocal()
    try:
        _set_setting(session, "naics_rotation_index", "0")
        from intake import intake_matching_contracts

        intake_result = intake_matching_contracts(session, all_notice_ids)
        session.commit()
        total = session.query(Contract).count()
    finally:
        session.close()

    scrape_result = {
        "attachments_enriched": scraped_total,
        "attachments_pending": scrape_skipped_total,
    }

    if manual_all_tiers:
        scope = f"Manual search all tiers — {len(naics_codes)} enabled code(s)"
    elif scheduled_tiers:
        searched = len(per_naics)
        scope = (
            f"Scheduled sync — tiers {', '.join(str(t) for t in scheduled_tiers)}: "
            f"searched {searched} code(s) this run"
        )
        if budget_skipped:
            scope += f" ({len(budget_skipped)} skipped — SAM budget)"
    else:
        scope = f"Synced {len(naics_codes)} NAICS code(s)"

    fetch_status = (
        f"{scope}. Saved {matched_total} filter-matching contract(s); "
        f"attachments pulled for {scraped_total} this run "
        f"({scrape_skipped_total} still pending — retried on next sync). "
        f"Claude full analysis runs on every contract once attachments are ready (ranking score)."
    )

    from api_budget import get_usage_snapshot, intake_on_sync_enabled
    from intake import start_background_attachment_enrich, start_background_intake

    if intake_on_sync_enabled():
        start_background_intake()
    start_background_attachment_enrich()

    return {
        "api_calls": api_calls,
        "fetch_status": fetch_status,
        "fetched_from_sam": fetched_total,
        "matched_filters": matched_total,
        "filtered_out": filtered_total,
        "new": new_total,
        "updated": updated_total,
        "intake": intake_result,
        "scrape": scrape_result,
        "total_in_db": total,
        "naics_synced": len(naics_codes),
        "naics_total": len(naics_from_env()),
        "scheduled_tiers": scheduled_tiers,
        "manual_all_tiers": manual_all_tiers,
        "budget_skipped": budget_skipped,
        "per_naics": per_naics,
        "api_budget": get_usage_snapshot(),
    }


def _pool_index(pool: list[str], naics_code: str) -> int:
    try:
        return pool.index(naics_code)
    except ValueError:
        return 0


def _sync_single_naics_scheduled(naics: str) -> dict[str, Any]:
    """One SAM search + attachment pulls for filter-matching contracts (uses remaining budget)."""
    from api_budget import can_spend_sam
    from intake import enrich_matching_attachments

    if not can_spend_sam(1):
        return {"skipped": True, "reason": "no_budget"}

    batch = fetch_naics_from_sam(naics)
    session = SessionLocal()
    try:
        batch, filter_stats = filter_search_results(batch, session)
        batch_ids = [str(o.get("notice_id") or "") for o in batch if o.get("notice_id")]
        new_count, updated_count = upsert_contracts(session, batch)
        attach_result = enrich_matching_attachments(session, batch_ids, limit=None, naics_code=naics)
        synced_map = json.loads(_get_setting(session, "naics_last_synced", "{}"))
        synced_map[naics] = date.today().isoformat()
        _set_setting(session, "naics_last_synced", json.dumps(synced_map))
        session.commit()
        pending_after = _pending_scrape_notice_ids(session, naics)
        return {
            "naics": naics,
            "search_results": len(batch) + filter_stats.get("filtered_out", 0),
            "matched": filter_stats.get("matched_filters", len(batch)),
            "filtered_out": filter_stats.get("filtered_out", 0),
            "new": new_count,
            "updated": updated_count,
            "attachments_enriched": attach_result.get("attachments_enriched", 0),
            "attachments_pending": len(pending_after),
            "batch_ids": batch_ids,
        }
    finally:
        session.close()


def sync_scheduled_naics() -> dict[str, Any]:
    """Use the full daily SAM budget: finish current NAICS, then search/enrich the next."""
    from api_budget import can_spend_sam, get_usage_snapshot
    from intake import (
        enrich_matching_attachments,
        intake_matching_contracts,
        start_background_attachment_enrich,
        start_background_intake,
    )
    from naics_labels import tiers_for_scheduled_sync
    from settings_store import get_naics_codes_for_tiers

    tiers = tiers_for_scheduled_sync()
    pool = get_naics_codes_for_tiers(tiers)
    if not pool:
        raise ValueError("No NAICS codes enabled — turn on at least one code in Settings.")

    if not can_spend_sam(1):
        raise ValueError("SAM.gov daily API budget exhausted — scheduled sync skipped until tomorrow.")

    session = SessionLocal()
    try:
        focus = get_focus_naics(session)
        current_index = int(_get_setting(session, SCHEDULED_ROTATION_KEY, "0")) % max(len(pool), 1)
    finally:
        session.close()

    focus_code = focus if focus in pool else None
    phases: list[dict[str, Any]] = []
    all_notice_ids: list[str] = []
    searches_run = 0
    attachments_enriched = 0

    while can_spend_sam(1):
        # 1) Finish pending attachments on the focus NAICS before searching another.
        if focus_code:
            session = SessionLocal()
            try:
                pending = _pending_scrape_notice_ids(session, focus_code)
            finally:
                session.close()
            if pending:
                session = SessionLocal()
                try:
                    attach_result = enrich_matching_attachments(
                        session, pending, limit=None, naics_code=focus_code
                    )
                    session.commit()
                finally:
                    session.close()
                attachments_enriched += attach_result.get("attachments_enriched", 0)
                all_notice_ids.extend(pending)
                phases.append({
                    "mode": "enrich",
                    "naics": focus_code,
                    "attachments_enriched": attach_result.get("attachments_enriched", 0),
                })
                session = SessionLocal()
                try:
                    pending_after = _pending_scrape_notice_ids(session, focus_code)
                finally:
                    session.close()
                if pending_after:
                    break
                completed = focus_code
                focus_code = None
                current_index = (_pool_index(pool, completed) + 1) % len(pool)
                if not can_spend_sam(1):
                    break
                continue

        # 2) Search the next NAICS and spend remaining budget on its attachments.
        codes, _ = _rotated_code_batch(pool, current_index, 1)
        naics = codes[0]
        result = _sync_single_naics_scheduled(naics)
        if result.get("skipped"):
            break
        searches_run += 1
        attachments_enriched += result.get("attachments_enriched", 0)
        all_notice_ids.extend(result.get("batch_ids", []))
        phases.append({"mode": "search", **result})
        focus_code = naics

        if result.get("attachments_pending", 0) > 0:
            current_index = _pool_index(pool, naics)
            break

        current_index = (current_index + 1) % len(pool)
        focus_code = None

    session = SessionLocal()
    try:
        _set_setting(session, FOCUS_NAICS_KEY, focus_code or "")
        _set_setting(session, SCHEDULED_ROTATION_KEY, str(current_index))
        if all_notice_ids:
            intake_matching_contracts(session, list(dict.fromkeys(all_notice_ids)))
        session.commit()
        pending_focus = len(_pending_scrape_notice_ids(session, focus_code)) if focus_code else 0
    finally:
        session.close()

    start_background_attachment_enrich()
    start_background_intake()

    budget = get_usage_snapshot()
    searched_naics = [p["naics"] for p in phases if p.get("mode") == "search"]
    status_parts = [
        f"SAM.gov: {budget['sam_used_today']}/{budget['sam_daily_limit']} API calls used today",
        f"{searches_run} NAICS search(es)",
        f"{attachments_enriched} attachment pull(s)",
    ]
    if focus_code and pending_focus:
        status_parts.append(f"staying on NAICS {focus_code} ({pending_focus} pending tomorrow)")
    elif searches_run:
        status_parts.append("rotation advanced — no pending attachments on last NAICS searched")

    return {
        "mode": "budget_loop",
        "scheduled_tiers": tiers,
        "scheduled_pool": pool,
        "scheduled_pool_size": len(pool),
        "scheduled_batch": searched_naics,
        "scheduled_next_index": current_index,
        "focus_naics": focus_code,
        "attachments_enriched": attachments_enriched,
        "attachments_pending": pending_focus,
        "phases": phases,
        "api_calls": budget["sam_used_today"],
        "fetch_status": ". ".join(status_parts) + ".",
        "api_budget": budget,
    }


def sync_all_naics() -> dict[str, Any]:
    """Manual full search — all enabled NAICS codes across every tier."""
    naics_codes = naics_from_env()
    return _sync_naics_code_list(naics_codes, manual_all_tiers=True)


def get_naics_sync_status() -> dict[str, Any]:
    from api_budget import scheduled_naics_per_sync, scheduled_sync_batch_size
    from naics_labels import tiers_for_scheduled_sync
    from settings_store import get_naics_codes_for_tiers

    naics_codes = naics_from_env()
    scheduled_tiers = tiers_for_scheduled_sync()
    scheduled_pool = get_naics_codes_for_tiers(scheduled_tiers)
    batch_size = scheduled_sync_batch_size() or scheduled_naics_per_sync()
    session = SessionLocal()
    try:
        synced_map = json.loads(_get_setting(session, "naics_last_synced", "{}"))
        index = int(_get_setting(session, "naics_rotation_index", "0")) % max(len(naics_codes), 1)
        sched_index = int(_get_setting(session, SCHEDULED_ROTATION_KEY, "0")) % max(len(scheduled_pool), 1)
        focus_naics = get_focus_naics(session)
        focus_pending = len(_pending_scrape_notice_ids(session, focus_naics)) if focus_naics else 0
    finally:
        session.close()
    next_naics = naics_codes[index] if naics_codes else None
    next_batch, _ = _rotated_code_batch(scheduled_pool, sched_index, batch_size)
    return {
        "naics_codes": naics_codes,
        "last_synced": synced_map,
        "synced_count": sum(1 for code in naics_codes if code in synced_map),
        "total_count": len(naics_codes),
        "next_naics": next_naics,
        "rotation_index": index,
        "scheduled_tiers": scheduled_tiers,
        "scheduled_pool_size": len(scheduled_pool),
        "scheduled_batch_size": batch_size,
        "scheduled_per_sync": scheduled_naics_per_sync(),
        "scheduled_next_batch": next_batch,
        "scheduled_rotation_index": sched_index,
        "focus_naics": focus_naics,
        "focus_pending_attachments": focus_pending,
    }


def main() -> None:
    import sys

    all_naics = "--all" in sys.argv
    search_only = "--search-only" in sys.argv
    naics_arg = None
    for arg in sys.argv[1:]:
        if arg.startswith("--naics="):
            naics_arg = arg.split("=", 1)[1].strip()
    print("Syncing SAM.gov -> PostgreSQL...")
    if all_naics:
        result = sync_all_naics()
    else:
        result = sync_from_sam(naics_arg, search_only=search_only)
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

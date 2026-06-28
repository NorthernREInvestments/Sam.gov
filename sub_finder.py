"""Find and track potential subcontractors via Google Places."""

from __future__ import annotations

import logging
import re
import threading
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from claude_client import analyze_subcontractors
from database import SessionLocal
from geo import haversine_miles, resolve_coordinates
from models import Contract, ContractSub, Sub
from places_client import PlacesApiError, search_text
from settings_store import get_sub_search_settings
from sub_constants import (
    AUTO_SUB_SEARCH_MIN_SCORE,
    DEFAULT_SUB_STATUS,
    SUB_STATUSES,
    classify_sub_type,
    search_terms_for_sub_type,
)
from sub_serializers import contract_sub_summary, contract_sub_to_dict, sub_to_dict
from usaspending_client import extract_work_location

logger = logging.getLogger("govtracker.sub_finder")
_search_lock = threading.Lock()
_search_ids: set[int] = set()


def _try_begin_search(contract_id: int) -> bool:
    with _search_lock:
        if contract_id in _search_ids:
            return False
        _search_ids.add(contract_id)
        return True


def _end_search(contract_id: int) -> None:
    with _search_lock:
        _search_ids.discard(contract_id)


def _contract_coords(contract: Contract) -> tuple[float, float, dict[str, Any]]:
    work = extract_work_location(
        contract.location,
        contract.sam_raw if isinstance(contract.sam_raw, dict) else None,
    )
    coords = resolve_coordinates(work.get("city"), work.get("state_code"), work.get("zip"))
    if not coords:
        raise ValueError(
            f"Could not geocode contract location ({work.get('label') or contract.location})."
        )
    return coords[0], coords[1], work


def _passes_filters(place: dict[str, Any], settings: dict[str, Any]) -> bool:
    rating = place.get("rating")
    reviews = place.get("review_count")
    if rating is not None and rating < settings["min_rating"]:
        return False
    if reviews is not None and reviews < settings["min_review_count"]:
        return False
    return True


def _google_search_candidates(
    *,
    sub_type_needed: str | None,
    latitude: float,
    longitude: float,
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    terms = search_terms_for_sub_type(sub_type_needed)
    primary_radius = settings["search_radius_miles"]
    radii = [primary_radius]
    if primary_radius < 50:
        radii.append(50)

    merged: dict[str, dict[str, Any]] = {}
    used_radius = primary_radius
    for radius in radii:
        used_radius = radius
        for term in terms:
            try:
                hits = search_text(
                    term,
                    latitude=latitude,
                    longitude=longitude,
                    radius_miles=radius,
                    max_results=20,
                )
            except PlacesApiError as exc:
                logger.warning("Places search failed for %r: %s", term, exc)
                continue
            for hit in hits:
                if not _passes_filters(hit, settings):
                    continue
                pid = hit.get("place_id")
                if pid and pid not in merged:
                    merged[pid] = hit
        if len(merged) >= 5 or radius == radii[-1]:
            break

    ranked = sorted(
        merged.values(),
        key=lambda row: (row.get("rating") or 0, row.get("review_count") or 0),
        reverse=True,
    )
    return ranked[:10], used_radius


def _outreach_block(contract: Contract, link: ContractSub) -> str:
    title = (contract.title or "Contract")[:100]
    lines = [f"[{date.today().isoformat()} · {title}]"]
    if link.status and link.status != DEFAULT_SUB_STATUS:
        lines.append(f"Status: {link.status}")
    if link.contact_notes and link.contact_notes.strip():
        lines.append(link.contact_notes.strip())
    if link.quote_amount is not None:
        lines.append(f"Quote: ${link.quote_amount:,.2f}")
    if link.quote_date:
        lines.append(f"Quote date: {link.quote_date.isoformat()}")
    return "\n".join(lines)


def _sync_sub_outreach_log(sub: Sub, contract: Contract, link: ContractSub) -> None:
    """Persist outreach on the master sub record for reuse on future contracts."""
    marker_start = f"<!-- outreach:{contract.notice_id} -->"
    marker_end = f"<!-- /outreach:{contract.notice_id} -->"
    block = _outreach_block(contract, link)
    wrapped = f"{marker_start}\n{block}\n{marker_end}"

    existing = sub.notes or ""
    pattern = re.compile(
        re.escape(marker_start) + r".*?" + re.escape(marker_end),
        re.DOTALL,
    )
    if pattern.search(existing):
        sub.notes = pattern.sub(wrapped, existing).strip()
    elif existing.strip():
        sub.notes = f"{existing.strip()}\n\n{wrapped}"
    else:
        sub.notes = wrapped
    sub.date_last_updated = datetime.now(timezone.utc)


def _latest_outreach(session: Session, sub_id: int) -> dict[str, Any]:
    link = (
        session.query(ContractSub)
        .filter(
            ContractSub.sub_id == sub_id,
            or_(
                ContractSub.status != DEFAULT_SUB_STATUS,
                ContractSub.contact_notes.isnot(None),
                ContractSub.quote_amount.isnot(None),
            ),
        )
        .order_by(ContractSub.date_status_updated.desc())
        .first()
    )
    if not link:
        return {}
    return {
        "latest_outreach_notes": link.contact_notes,
        "latest_quote_amount": float(link.quote_amount) if link.quote_amount else None,
        "latest_status": link.status,
    }


def upsert_sub(session: Session, place: dict[str, Any], *, sub_type: str) -> Sub:
    place_id = place["place_id"]
    row = session.query(Sub).filter_by(place_id=place_id).first()
    now = datetime.now(timezone.utc)
    rating = place.get("rating")
    values = {
        "business_name": place.get("business_name") or "Unknown business",
        "phone": place.get("phone"),
        "rating": Decimal(str(rating)) if rating is not None else None,
        "review_count": place.get("review_count"),
        "address": place.get("address"),
        "city": place.get("city"),
        "state": place.get("state"),
        "zip": place.get("zip"),
        "website": place.get("website"),
        "google_maps_url": place.get("google_maps_url"),
        "sub_type": sub_type,
        "latitude": Decimal(str(place["latitude"])) if place.get("latitude") is not None else None,
        "longitude": Decimal(str(place["longitude"])) if place.get("longitude") is not None else None,
        "date_last_updated": now,
    }
    if row:
        for key, val in values.items():
            setattr(row, key, val)
        return row

    row = Sub(place_id=place_id, date_first_found=now, **values)
    session.add(row)
    session.flush()
    return row


def link_sub_to_contract(
    session: Session,
    contract: Contract,
    sub: Sub,
    *,
    distance_miles: float | None = None,
) -> ContractSub:
    link = (
        session.query(ContractSub)
        .filter_by(contract_id=contract.id, sub_id=sub.id)
        .first()
    )
    if link:
        if distance_miles is not None:
            link.distance_miles = Decimal(str(round(distance_miles, 1)))
        return link
    link = ContractSub(
        contract_id=contract.id,
        sub_id=sub.id,
        status=DEFAULT_SUB_STATUS,
        distance_miles=Decimal(str(round(distance_miles, 1))) if distance_miles is not None else None,
    )
    session.add(link)
    session.flush()
    return link


def find_subs_for_contract(
    notice_id: str,
    *,
    force: bool = False,
    sub_ids: list[int] | None = None,
) -> dict[str, Any]:
    session = SessionLocal()
    try:
        contract = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not contract:
            raise ValueError("Contract not found")
        if sub_ids:
            return _add_existing_subs(session, contract, sub_ids)
        return _run_places_search(session, contract, force=force)
    finally:
        session.close()


def _add_existing_subs(session: Session, contract: Contract, sub_ids: list[int]) -> dict[str, Any]:
    lat, lng, work = _contract_coords(contract)
    added = 0
    for sub_id in sub_ids:
        sub = session.get(Sub, sub_id)
        if not sub:
            continue
        dist = None
        if sub.latitude is not None and sub.longitude is not None:
            dist = haversine_miles(lat, lng, float(sub.latitude), float(sub.longitude))
        link_sub_to_contract(session, contract, sub, distance_miles=dist)
        added += 1
    session.commit()
    return {
        "notice_id": contract.notice_id,
        "added_from_network": added,
        "summary": contract_sub_summary(contract, session),
    }


def _run_places_search(session: Session, contract: Contract, *, force: bool) -> dict[str, Any]:
    if not _try_begin_search(contract.id):
        return {
            "notice_id": contract.notice_id,
            "in_progress": True,
            "summary": contract_sub_summary(contract, session),
        }

    contract.sub_search_status = "searching"
    session.commit()

    try:
        analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
        sub_type_needed = analysis.get("sub_type_needed")
        settings = get_sub_search_settings()
        lat, lng, work = _contract_coords(contract)
        candidates, used_radius = _google_search_candidates(
            sub_type_needed=sub_type_needed,
            latitude=lat,
            longitude=lng,
            settings=settings,
        )

        if not candidates:
            contract.sub_search_status = "complete"
            contract.sub_search_radius_miles = used_radius
            session.commit()
            return {
                "notice_id": contract.notice_id,
                "message": "No subs found automatically. You can add subs manually.",
                "results_count": 0,
                "summary": contract_sub_summary(contract, session),
            }

        sub_type = classify_sub_type(sub_type_needed)
        links: list[ContractSub] = []
        for place in candidates:
            sub = upsert_sub(session, place, sub_type=sub_type)
            link = link_sub_to_contract(
                session,
                contract,
                sub,
                distance_miles=place.get("distance_miles"),
            )
            links.append(link)
        session.flush()

        claude_rows = analyze_subcontractors(contract, candidates)
        claude_by_place = {row["place_id"]: row for row in claude_rows}
        for link in links:
            sub = link.sub
            insight = claude_by_place.get(sub.place_id) if sub else None
            if insight:
                link.claude_score = insight["score"]
                link.claude_reason = insight["reason"]

        contract.sub_search_status = "complete"
        contract.sub_search_radius_miles = used_radius
        session.commit()

        return {
            "notice_id": contract.notice_id,
            "results_count": len(candidates),
            "radius_miles": used_radius,
            "city": work.get("city") or work.get("label"),
            "summary": contract_sub_summary(contract, session),
            "subs": list_contract_subs(session, contract.notice_id).get("subs", []),
        }
    except PlacesApiError as exc:
        contract.sub_search_status = "error"
        session.commit()
        return {"notice_id": contract.notice_id, "error": str(exc)}
    except Exception as exc:
        contract.sub_search_status = "error"
        session.commit()
        logger.exception("Sub search failed for %s", contract.notice_id)
        return {"notice_id": contract.notice_id, "error": str(exc)}
    finally:
        _end_search(contract.id)


def start_background_sub_search(notice_id: str, *, force: bool = False) -> None:
    def _worker() -> None:
        find_subs_for_contract(notice_id, force=force)

    threading.Thread(target=_worker, daemon=True, name=f"sub-search-{notice_id[:8]}").start()


def maybe_auto_sub_search(contract: Contract) -> None:
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    score = analysis.get("score")
    try:
        score_int = int(score)
    except (TypeError, ValueError):
        return
    if score_int < AUTO_SUB_SEARCH_MIN_SCORE:
        return
    if contract.sub_search_status == "searching":
        return

    session = SessionLocal()
    try:
        existing = (
            session.query(ContractSub)
            .filter_by(contract_id=contract.id)
            .count()
        )
        if existing > 0 or contract.sub_search_status == "complete":
            return
    finally:
        session.close()

    start_background_sub_search(contract.notice_id)


def list_contract_subs(session: Session, notice_id: str) -> dict[str, Any]:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")
    links = (
        session.query(ContractSub)
        .options(joinedload(ContractSub.sub))
        .filter_by(contract_id=contract.id)
        .order_by(
            ContractSub.claude_score.desc().nulls_last(),
            ContractSub.distance_miles.asc().nulls_last(),
        )
        .all()
    )
    work = extract_work_location(
        contract.location,
        contract.sam_raw if isinstance(contract.sam_raw, dict) else None,
    )
    summary = contract_sub_summary(contract, session)
    summary["city"] = work.get("city") or work.get("label")
    return {
        "notice_id": notice_id,
        "contract_title": contract.title,
        "agency": contract.agency,
        "summary": summary,
        "selected_sub_quote": float(contract.selected_sub_quote) if contract.selected_sub_quote else None,
        "subs": [contract_sub_to_dict(link) for link in links],
    }


def update_contract_sub(session: Session, link_id: int, payload: dict[str, Any]) -> ContractSub:
    link = session.get(ContractSub, link_id)
    if not link:
        raise ValueError("Contract sub link not found")

    if "status" in payload:
        status = payload["status"]
        if status not in SUB_STATUSES:
            raise ValueError(f"Invalid status. Choose one of: {', '.join(SUB_STATUSES)}")
        link.status = status
        link.date_status_updated = datetime.now(timezone.utc)

        if status == "Selected":
            others = (
                session.query(ContractSub)
                .filter(
                    ContractSub.contract_id == link.contract_id,
                    ContractSub.id != link.id,
                    ContractSub.status == "Selected",
                )
                .all()
            )
            for other in others:
                other.status = "Quote Received" if other.quote_amount is not None else "Spoke With — Interested"
            contract = session.get(Contract, link.contract_id)
            if contract and link.quote_amount is not None:
                contract.selected_sub_quote = link.quote_amount
        else:
            contract = session.get(Contract, link.contract_id)
            if contract and contract.selected_sub_quote is not None:
                selected = (
                    session.query(ContractSub)
                    .filter_by(contract_id=link.contract_id, status="Selected")
                    .first()
                )
                if not selected:
                    contract.selected_sub_quote = None

    if "contact_notes" in payload:
        link.contact_notes = payload["contact_notes"]
    if "quote_amount" in payload:
        val = payload["quote_amount"]
        link.quote_amount = Decimal(str(val)) if val not in (None, "") else None
        if link.status == "Selected" and link.quote_amount is not None:
            contract = session.get(Contract, link.contract_id)
            if contract:
                contract.selected_sub_quote = link.quote_amount
    if "quote_date" in payload:
        raw = payload["quote_date"]
        link.quote_date = date.fromisoformat(raw) if raw else None

    outreach_fields = {"contact_notes", "quote_amount", "quote_date", "status"}
    if outreach_fields.intersection(payload.keys()):
        link.date_status_updated = datetime.now(timezone.utc)
        sub = session.get(Sub, link.sub_id)
        contract = session.get(Contract, link.contract_id)
        if sub and contract:
            _sync_sub_outreach_log(sub, contract, link)

    session.commit()
    link = (
        session.query(ContractSub)
        .options(joinedload(ContractSub.sub))
        .filter_by(id=link_id)
        .first()
    )
    return link


def list_master_subs(
    session: Session,
    *,
    search: str | None = None,
    sub_type: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    query = session.query(Sub)
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(
            (Sub.business_name.ilike(like))
            | (Sub.city.ilike(like))
            | (Sub.state.ilike(like))
            | (Sub.sub_type.ilike(like))
        )
    if sub_type:
        query = query.filter(Sub.sub_type == sub_type)
    if state:
        query = query.filter(Sub.state == state.upper())
    rows = query.order_by(Sub.business_name.asc()).all()
    results: list[dict[str, Any]] = []
    for row in rows:
        stats = _sub_stats(session, row.id)
        outreach = _latest_outreach(session, row.id)
        results.append(sub_to_dict(row, stats=stats, outreach=outreach))
    return results


def _sub_stats(session: Session, sub_id: int) -> dict[str, Any]:
    total = session.query(func.count(ContractSub.id)).filter(ContractSub.sub_id == sub_id).scalar() or 0
    selected = (
        session.query(func.count(ContractSub.id))
        .filter(ContractSub.sub_id == sub_id, ContractSub.status == "Selected")
        .scalar()
        or 0
    )
    contacted = (
        session.query(func.count(ContractSub.id))
        .filter(
            ContractSub.sub_id == sub_id,
            or_(
                ContractSub.status != DEFAULT_SUB_STATUS,
                ContractSub.contact_notes.isnot(None),
                ContractSub.quote_amount.isnot(None),
            ),
        )
        .scalar()
        or 0
    )
    last = (
        session.query(func.max(ContractSub.date_status_updated))
        .filter(
            ContractSub.sub_id == sub_id,
            or_(
                ContractSub.status != DEFAULT_SUB_STATUS,
                ContractSub.contact_notes.isnot(None),
                ContractSub.quote_amount.isnot(None),
            ),
        )
        .scalar()
    )
    return {
        "times_contacted": contacted,
        "times_selected": selected,
        "times_linked": total,
        "last_contacted_at": last.isoformat() if last else None,
    }


def get_sub_history(session: Session, sub_id: int) -> dict[str, Any]:
    sub = session.get(Sub, sub_id)
    if not sub:
        raise ValueError("Sub not found")
    links = (
        session.query(ContractSub)
        .options(joinedload(ContractSub.contract))
        .filter_by(sub_id=sub_id)
        .order_by(ContractSub.date_added.desc())
        .all()
    )
    history = []
    for link in links:
        contract = link.contract
        history.append(
            {
                **contract_sub_to_dict(link),
                "contract_notice_id": contract.notice_id if contract else None,
                "contract_title": contract.title if contract else None,
                "contract_agency": contract.agency if contract else None,
            }
        )
    return {"sub": sub_to_dict(sub, stats=_sub_stats(session, sub_id), outreach=_latest_outreach(session, sub_id)), "history": history}


def create_manual_sub(session: Session, payload: dict[str, Any]) -> Sub:
    place_id = (payload.get("place_id") or "").strip() or f"manual_{uuid.uuid4().hex}"
    existing = session.query(Sub).filter_by(place_id=place_id).first()
    if existing:
        raise ValueError("A sub with this place ID already exists")

    sub_type = classify_sub_type(payload.get("sub_type") or payload.get("business_name"))
    row = Sub(
        place_id=place_id,
        business_name=payload.get("business_name") or "Unnamed business",
        phone=payload.get("phone"),
        rating=Decimal(str(payload["rating"])) if payload.get("rating") not in (None, "") else None,
        review_count=int(payload["review_count"]) if payload.get("review_count") not in (None, "") else None,
        address=payload.get("address"),
        city=payload.get("city"),
        state=(payload.get("state") or "").upper() or None,
        zip=payload.get("zip"),
        website=payload.get("website"),
        google_maps_url=payload.get("google_maps_url"),
        sub_type=sub_type,
        notes=payload.get("notes"),
    )
    coords = resolve_coordinates(row.city, row.state, row.zip)
    if coords:
        row.latitude = Decimal(str(coords[0]))
        row.longitude = Decimal(str(coords[1]))
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def update_sub_notes(session: Session, sub_id: int, notes: str | None) -> Sub:
    sub = session.get(Sub, sub_id)
    if not sub:
        raise ValueError("Sub not found")
    sub.notes = notes
    session.commit()
    session.refresh(sub)
    return sub


def nearby_network_subs(session: Session, notice_id: str) -> dict[str, Any]:
    contract = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not contract:
        raise ValueError("Contract not found")
    lat, lng, work = _contract_coords(contract)
    settings = get_sub_search_settings()
    radius = settings["search_radius_miles"]
    existing_ids = {
        link.sub_id
        for link in session.query(ContractSub.sub_id).filter_by(contract_id=contract.id).all()
    }
    matches: list[dict[str, Any]] = []
    for sub in session.query(Sub).all():
        if sub.id in existing_ids:
            continue
        if sub.latitude is None or sub.longitude is None:
            continue
        dist = haversine_miles(lat, lng, float(sub.latitude), float(sub.longitude))
        if dist <= radius:
            matches.append({**sub_to_dict(sub), "distance_miles": round(dist, 1)})
    return {
        "notice_id": notice_id,
        "count": len(matches),
        "radius_miles": radius,
        "city": work.get("city") or work.get("label"),
        "subs": sorted(matches, key=lambda row: row.get("distance_miles") or 999),
    }

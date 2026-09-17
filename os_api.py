"""GovCon OS API — dashboard, pipeline, today, lifecycle (zero external on GET)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from database import SessionLocal
from os_service import build_active_deals_pipeline, build_global_today_queue, build_os_dashboard

router = APIRouter(prefix="/api/os", tags=["govcon-os"])


@router.get("/dashboard")
def get_os_dashboard() -> dict[str, Any]:
    session = SessionLocal()
    try:
        return build_os_dashboard(session)
    finally:
        session.close()


@router.get("/today")
def get_os_today() -> dict[str, Any]:
    session = SessionLocal()
    try:
        return build_global_today_queue(session)
    finally:
        session.close()


@router.get("/pipeline")
def get_os_pipeline(bucket: str | None = Query(None)) -> dict[str, Any]:
    session = SessionLocal()
    try:
        return build_active_deals_pipeline(session, bucket=bucket)
    finally:
        session.close()


@router.get("/lifecycle-states")
def get_lifecycle_states() -> dict[str, Any]:
    from deal_lifecycle import (
        LIFECYCLE_AWARDED,
        LIFECYCLE_BID_READY,
        LIFECYCLE_BUILDING_BID,
        LIFECYCLE_CLOSED,
        LIFECYCLE_DEAL_READY,
        LIFECYCLE_FUNDING,
        LIFECYCLE_INVOICED,
        LIFECYCLE_LOST,
        LIFECYCLE_NEW,
        LIFECYCLE_PAID,
        LIFECYCLE_PERFORMING,
        LIFECYCLE_PRICING,
        LIFECYCLE_QUALIFYING,
        LIFECYCLE_REJECTED,
        LIFECYCLE_RESEARCHING,
        LIFECYCLE_SOURCING,
        LIFECYCLE_SUBMITTED,
        LIFECYCLE_WATCH,
    )

    return {
        "states": [
            LIFECYCLE_NEW,
            LIFECYCLE_QUALIFYING,
            LIFECYCLE_RESEARCHING,
            LIFECYCLE_SOURCING,
            LIFECYCLE_FUNDING,
            LIFECYCLE_PRICING,
            LIFECYCLE_DEAL_READY,
            LIFECYCLE_BUILDING_BID,
            LIFECYCLE_BID_READY,
            LIFECYCLE_SUBMITTED,
            LIFECYCLE_AWARDED,
            LIFECYCLE_PERFORMING,
            LIFECYCLE_INVOICED,
            LIFECYCLE_PAID,
            LIFECYCLE_CLOSED,
            LIFECYCLE_WATCH,
            LIFECYCLE_REJECTED,
            LIFECYCLE_LOST,
        ],
        "LIVE_API_REQUESTS": 0,
    }


@router.get("/suppliers")
def get_global_suppliers(search: str | None = Query(None)) -> dict[str, Any]:
    from global_views import build_suppliers_view

    session = SessionLocal()
    try:
        return build_suppliers_view(session, search=search)
    finally:
        session.close()


@router.get("/contacts")
def get_global_contacts(search: str | None = Query(None)) -> dict[str, Any]:
    from global_views import build_contacts_view

    session = SessionLocal()
    try:
        return build_contacts_view(session, search=search)
    finally:
        session.close()


@router.get("/funding")
def get_global_funding() -> dict[str, Any]:
    from global_views import build_funding_work_view

    session = SessionLocal()
    try:
        return build_funding_work_view(session)
    finally:
        session.close()


@router.get("/bids")
def get_global_bids() -> dict[str, Any]:
    from global_views import build_bids_view

    session = SessionLocal()
    try:
        return build_bids_view(session)
    finally:
        session.close()


@router.get("/awards")
def get_global_awards() -> dict[str, Any]:
    from global_views import build_awards_view

    session = SessionLocal()
    try:
        return build_awards_view(session)
    finally:
        session.close()


@router.get("/ai-mode")
def get_ai_operating_mode() -> dict[str, Any]:
    from ai_operating_modes import get_operating_mode, mode_config

    return {**mode_config(), "LIVE_API_REQUESTS": 0, "OpenAI": 0}

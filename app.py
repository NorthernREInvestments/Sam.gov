"""GovTracker web API and dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from auth import (
    COOKIE_NAME,
    auth_enabled,
    create_auth_token,
    is_public_path,
    verify_auth_token,
    verify_login,
)
from database import SessionLocal
from sam_client import min_days_from_env, naics_from_env
from scheduler import configure_scheduler, scheduler_status, start_scheduler, stop_scheduler
from settings_store import get_all_settings, reset_screening_prompt, save_settings
from pricing import get_contract_pricing_intel
from sync import contract_to_dict, get_naics_sync_status, list_contracts, sync_all_naics, sync_from_sam
from screen import screen_one, screen_pending

STATIC_DIR = Path(__file__).resolve().parent / "static"


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth_enabled() or is_public_path(request.url.path):
            return await call_next(request)
        token = request.cookies.get(COOKIE_NAME)
        if verify_auth_token(token):
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Login required"}, status_code=401)
        return RedirectResponse("/login.html")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from database import init_db

    init_db()
    start_scheduler()
    from api_budget import intake_on_sync_enabled

    if intake_on_sync_enabled():
        from intake import start_background_intake

        start_background_intake()
    yield
    stop_scheduler()


app = FastAPI(title="GovTracker", version="0.1.0", lifespan=lifespan)
app.add_middleware(AuthMiddleware)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=1)


class SettingsUpdate(BaseModel):
    naics_codes: list[str] = Field(..., min_length=1)
    min_days_until_due: int = Field(..., ge=0, le=365)
    min_score_threshold: int = Field(..., ge=1, le=10)
    screening_prompt: str | None = Field(None, min_length=20)
    scheduler_enabled: bool = True
    scheduler_hour: int = Field(6, ge=0, le=23)
    scheduler_minute: int = Field(0, ge=0, le=59)
    scheduler_timezone: str = Field("America/Denver", min_length=3)
    sub_search_radius_miles: int = Field(25, ge=10, le=100)
    sub_min_rating: float = Field(3.5, ge=0, le=5)
    sub_min_review_count: int = Field(5, ge=0, le=1000)


class ContractSubUpdate(BaseModel):
    status: str | None = None
    contact_notes: str | None = None
    quote_amount: float | None = None
    quote_date: str | None = None


class ManualSubCreate(BaseModel):
    business_name: str = Field(..., min_length=2, max_length=512)
    phone: str | None = None
    rating: float | None = Field(None, ge=0, le=5)
    review_count: int | None = Field(None, ge=0)
    address: str | None = None
    city: str | None = None
    state: str | None = Field(None, max_length=2)
    zip: str | None = None
    website: str | None = None
    google_maps_url: str | None = None
    sub_type: str | None = None
    notes: str | None = None
    place_id: str | None = None


class SubNotesUpdate(BaseModel):
    notes: str | None = None


class AddNetworkSubsRequest(BaseModel):
    sub_ids: list[int] = Field(..., min_length=1)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "GovTracker", "auth_enabled": auth_enabled()}


@app.get("/api/auth/status")
def auth_status(request: Request):
    if not auth_enabled():
        return {"auth_enabled": False, "authenticated": True}
    token = request.cookies.get(COOKIE_NAME)
    return {"auth_enabled": True, "authenticated": verify_auth_token(token)}


@app.post("/api/login")
def login(body: LoginRequest, response: Response):
    if not auth_enabled():
        return {"ok": True, "message": "Auth disabled in this environment"}
    if not verify_login(body.email, body.password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    response.set_cookie(
        COOKIE_NAME,
        create_auth_token(),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/config")
def config():
    sync_status = get_naics_sync_status()
    settings = get_all_settings()
    return {
        "naics_codes": settings["naics_codes"],
        "naics_labels": settings["naics_labels"],
        "default_min_days": settings["min_days_until_due"],
        "default_min_score": settings["min_score_threshold"],
        "naics_sync": sync_status,
        "auth_enabled": auth_enabled(),
    }


@app.get("/api/contracts")
def get_contracts(
    naics: str | None = Query(None, description="Comma-separated NAICS codes"),
    min_days: int | None = Query(None, ge=0, le=365),
    min_score: int | None = Query(None, ge=1, le=10),
    agency: str | None = Query(None),
    pursue_only: bool = Query(False),
):
    naics_codes = [c.strip() for c in naics.split(",") if c.strip()] if naics else None
    session = SessionLocal()
    try:
        rows = list_contracts(
            session,
            naics_codes=naics_codes,
            min_days_until_due=min_days,
            min_score=min_score,
            agency=agency,
            pursue_only=pursue_only,
        )
        from api_budget import get_usage_snapshot
        from intake import enrich_matching_attachments, start_background_attachment_enrich

        notice_ids = [r.notice_id for r in rows]
        attachment_refresh = enrich_matching_attachments(session, notice_ids, limit=None)
        if attachment_refresh.get("attachments_pending", 0) > 0:
            start_background_attachment_enrich()

        return {
            "count": len(rows),
            "contracts": [contract_to_dict(r) for r in rows],
            "api_budget": get_usage_snapshot(),
            "attachments_refreshed": attachment_refresh.get("attachments_enriched", 0),
        }
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}")
def get_contract(notice_id: str):
    session = SessionLocal()
    try:
        from models import Contract

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        from sam_enrich import ensure_enriched_sam_raw, needs_enrichment
        from sam_client import normalize_opportunity

        ensure_enriched_sam_raw(row, force=needs_enrichment(row.sam_raw if isinstance(row.sam_raw, dict) else None))
        if isinstance(row.sam_raw, dict):
            if row.sam_raw.get("descriptionText"):
                row.description = row.sam_raw["descriptionText"][:8000]
            refreshed = normalize_opportunity(row.sam_raw)
            if refreshed.get("location"):
                row.location = refreshed["location"]
        session.commit()
        data = contract_to_dict(row)
        data["sam_raw"] = row.sam_raw
        return data
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}/pricing")
def get_contract_pricing(notice_id: str, refresh: bool = Query(False)):
    session = SessionLocal()
    try:
        from models import Contract

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        intel = get_contract_pricing_intel(row, force_refresh=refresh)
        session.commit()
        return intel
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=502, detail=f"Pricing lookup failed: {exc}") from exc
    finally:
        session.close()


@app.post("/api/sync")
def run_sync(
    all_naics: bool = Query(False),
    naics: str | None = Query(None, description="Specific NAICS to search (defaults to next in rotation)"),
    search_only: bool = Query(False, description="Only pull opportunities — 1 SAM.gov API call, no enrich/intake"),
):
    try:
        if all_naics:
            result = sync_all_naics()
        else:
            result = sync_from_sam(naics, search_only=search_only)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SAM.gov sync failed: {exc}") from exc


@app.post("/api/screen")
def run_screen(
    limit: int = Query(5, ge=1, le=25),
    force: bool = Query(False),
    matching_only: bool = Query(True),
):
    try:
        from api_budget import ScreenBudgetExceeded

        return screen_pending(limit=limit, force=force, matching_only=matching_only)
    except ScreenBudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Screening failed: {exc}") from exc


@app.post("/api/contracts/{notice_id}/screen")
def run_screen_one(notice_id: str, force: bool = Query(False)):
    try:
        from api_budget import ScreenBudgetExceeded

        return screen_one(notice_id, force=force)
    except ScreenBudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Screening failed: {exc}") from exc


@app.get("/api/scheduler")
def read_scheduler():
    return scheduler_status()


@app.get("/api/settings")
def read_settings():
    return get_all_settings()


@app.put("/api/settings")
def update_settings(body: SettingsUpdate):
    try:
        result = save_settings(
            naics_codes=body.naics_codes,
            min_days_until_due=body.min_days_until_due,
            min_score_threshold=body.min_score_threshold,
            screening_prompt=body.screening_prompt,
            scheduler_enabled=body.scheduler_enabled,
            scheduler_hour=body.scheduler_hour,
            scheduler_minute=body.scheduler_minute,
            scheduler_timezone=body.scheduler_timezone,
            sub_search_radius_miles=body.sub_search_radius_miles,
            sub_min_rating=body.sub_min_rating,
            sub_min_review_count=body.sub_min_review_count,
        )
        configure_scheduler()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/screening-prompt/reset")
def restore_default_prompt():
    prompt = reset_screening_prompt()
    return {"screening_prompt": prompt, "screening_prompt_custom": False}


@app.post("/api/contracts/{notice_id}/find-subs")
def run_find_subs(notice_id: str, force: bool = Query(False)):
    from sub_finder import start_background_sub_search

    session = SessionLocal()
    try:
        from models import Contract

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
    finally:
        session.close()
    start_background_sub_search(notice_id, force=force)
    return {"notice_id": notice_id, "started": True}


@app.get("/api/contracts/{notice_id}/subs")
def get_contract_subs(notice_id: str):
    session = SessionLocal()
    try:
        from sub_finder import list_contract_subs

        return list_contract_subs(session, notice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}/nearby-subs")
def get_nearby_network_subs(notice_id: str):
    session = SessionLocal()
    try:
        from sub_finder import nearby_network_subs

        return nearby_network_subs(session, notice_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/subs/add-network")
def add_network_subs(notice_id: str, body: AddNetworkSubsRequest):
    try:
        from sub_finder import find_subs_for_contract

        return find_subs_for_contract(notice_id, sub_ids=body.sub_ids)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/contract-subs/{link_id}")
def patch_contract_sub(link_id: int, body: ContractSubUpdate):
    session = SessionLocal()
    try:
        from sub_finder import update_contract_sub
        from sub_serializers import contract_sub_to_dict

        link = update_contract_sub(
            session,
            link_id,
            body.model_dump(exclude_unset=True),
        )
        return contract_sub_to_dict(link)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/subs")
def get_subs(
    search: str | None = Query(None),
    sub_type: str | None = Query(None),
    state: str | None = Query(None),
):
    session = SessionLocal()
    try:
        from sub_finder import list_master_subs

        return {"subs": list_master_subs(session, search=search, sub_type=sub_type, state=state)}
    finally:
        session.close()


@app.get("/api/subs/{sub_id}")
def get_sub_detail(sub_id: int):
    session = SessionLocal()
    try:
        from sub_finder import get_sub_history

        return get_sub_history(session, sub_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/subs")
def create_sub(body: ManualSubCreate):
    session = SessionLocal()
    try:
        from sub_finder import create_manual_sub
        from sub_serializers import sub_to_dict

        row = create_manual_sub(session, body.model_dump())
        return sub_to_dict(row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.patch("/api/subs/{sub_id}")
def patch_sub_notes(sub_id: int, body: SubNotesUpdate):
    session = SessionLocal()
    try:
        from sub_finder import update_sub_notes
        from sub_serializers import sub_to_dict

        row = update_sub_notes(session, sub_id, body.notes)
        return sub_to_dict(row)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

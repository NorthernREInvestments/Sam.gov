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
    from screen import start_background_screening

    start_background_screening()
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
        return {"count": len(rows), "contracts": [contract_to_dict(r) for r in rows]}
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
def run_sync(all_naics: bool = Query(False)):
    try:
        result = sync_all_naics() if all_naics else sync_from_sam()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SAM.gov sync failed: {exc}") from exc


@app.post("/api/screen")
def run_screen(
    limit: int = Query(25, ge=1, le=100),
    force: bool = Query(False),
    matching_only: bool = Query(True),
):
    try:
        return screen_pending(limit=limit, force=force, matching_only=matching_only)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Screening failed: {exc}") from exc


@app.post("/api/contracts/{notice_id}/screen")
def run_screen_one(notice_id: str, force: bool = Query(False)):
    try:
        return screen_one(notice_id, force=force)
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
        )
        configure_scheduler()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/screening-prompt/reset")
def restore_default_prompt():
    prompt = reset_screening_prompt()
    return {"screening_prompt": prompt, "screening_prompt_custom": False}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

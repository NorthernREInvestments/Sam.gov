"""GovTracker web API and dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from typing import Any, Literal

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
from pricing import get_full_pricing_intel, get_pricing_dashboard
from sync import contract_to_dict, get_naics_sync_status, list_contracts, sync_all_naics, sync_from_sam
from screen import force_full_analysis, screen_one, screen_pending

STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_BUILD_VERSION = "20260701-card-v3"


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
        from intake import start_background_attachment_enrich, start_background_intake

        start_background_intake()
        start_background_attachment_enrich()
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
    agreement_signature_status: str | None = None


class SubContactUpdate(BaseModel):
    company_name: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = Field(None, max_length=8)
    rating: float | None = Field(None, ge=0, le=5)
    source: str | None = None
    called: bool | None = None
    call_date: str | None = None
    reached: bool | None = None
    voicemail_left: bool | None = None
    email_sent: bool | None = None
    email_sent_date: str | None = None
    quote_received: bool | None = None
    quote_amount: float | None = None
    quote_date: str | None = None
    payment_terms_confirmed: bool | None = None
    insurance_verified: bool | None = None
    insurance_expiration_date: str | None = None
    insurance_coverage_amount: float | None = None
    references_requested: bool | None = None
    references_received: bool | None = None
    references: list[dict[str, Any]] | None = None
    is_selected: bool | None = None
    select: bool | None = None
    status: str | None = None
    notes: str | None = None


class MarkEmailSentRequest(BaseModel):
    sent: bool = True


class ChecklistItemUpdate(BaseModel):
    checked: bool | None = None
    na: bool | None = None
    na_reason: str | None = None
    notes: str | None = None


class CoQuestionUpdate(BaseModel):
    text: str | None = None
    asked: bool | None = None
    response: str | None = None
    resolved: bool | None = None


class SubmissionMetaUpdate(BaseModel):
    submission_method_confirmed: bool | None = None
    submission_method_notes: str | None = None
    submission_method: str | None = None
    submission_email: str | None = None


class ContractOutcomeUpdate(BaseModel):
    status: Literal[
        "won",
        "lost",
        "bidding",
        "reviewing",
        "new",
        "skipped",
        "awarded",
        "active",
        "option_year",
        "stop_work",
        "completed",
        "not_awarded",
        "submitted",
    ] | None = None
    awarded_amount: float | None = Field(None, ge=0)
    margin_percentage: float | None = Field(None, ge=10, le=35)


class PerformanceUpdate(BaseModel):
    award_date: str | None = None
    period_of_performance_start: str | None = None
    period_of_performance_end: str | None = None
    option_years_remaining: int | None = Field(None, ge=0)
    government_contract_number: str | None = None
    invoicing_system: str | None = None
    invoicing_system_confirmed: bool | None = None
    cor_name: str | None = None
    cor_email: str | None = None
    cor_phone: str | None = None
    co_name: str | None = None
    co_email: str | None = None
    co_phone: str | None = None
    stop_work_issued: bool | None = None
    stop_work_issued_date: str | None = None
    cpars_rating: str | None = None
    cpars_comments: str | None = None
    cpars_expected_date: str | None = None
    status: str | None = None
    mark_awarded: bool | None = None


class InvoiceCreate(BaseModel):
    billing_period_start: str | None = None
    billing_period_end: str | None = None
    invoice_amount: float | None = None
    invoice_submission_method: str | None = None
    notes: str | None = None


class InvoiceUpdate(BaseModel):
    billing_period_start: str | None = None
    billing_period_end: str | None = None
    invoice_amount: float | None = None
    invoice_submitted_date: str | None = None
    invoice_submission_method: str | None = None
    invoice_accepted_date: str | None = None
    payment_received_date: str | None = None
    payment_amount: float | None = None
    status: str | None = None
    notes: str | None = None


class SubPaymentCreate(BaseModel):
    invoice_id: int | None = None
    sub_contact_id: int | None = None
    sub_invoice_received_date: str | None = None
    sub_invoice_amount: float | None = None


class SubPaymentUpdate(BaseModel):
    sub_invoice_received_date: str | None = None
    sub_invoice_amount: float | None = None
    government_signoff_received: bool | None = None
    government_signoff_date: str | None = None
    government_signoff_notes: str | None = None
    payment_released_date: str | None = None
    payment_amount: float | None = None
    payment_method: str | None = None
    notes: str | None = None


class PerformanceSettingsUpdate(BaseModel):
    wawf_last_password_change: str | None = None
    ipp_registered: bool | None = None


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


class SubProfileUpdate(BaseModel):
    owner_name: str | None = None
    owner_title: str | None = None
    license_number: str | None = None
    insurance_carrier: str | None = None
    business_email: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = Field(None, max_length=8)
    zip: str | None = None
    phone: str | None = None
    notes: str | None = None


class AgreementSignatureUpdate(BaseModel):
    agreement_signature_status: str = Field(..., min_length=3, max_length=64)


class AddNetworkSubsRequest(BaseModel):
    sub_ids: list[int] = Field(..., min_length=1)


class OwnerSettingsUpdate(BaseModel):
    legal_business_name: str | None = None
    owner_name: str | None = None
    owner_title: str | None = None
    business_phone: str | None = None
    business_email: str | None = None
    address_line_1: str | None = None
    address_line_2: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    uei: str | None = None
    cage_code: str | None = None
    ein: str | None = None
    sam_expiration: str | None = None
    default_margin_pct: float | None = Field(None, ge=10, le=35)
    default_option_year_increase_pct: float | None = Field(None, ge=0, le=15)
    commercial_experience: str | None = None
    certifications: str | None = None
    past_performance: str | None = None


class ProposalConfigRequest(BaseModel):
    contract_sub_id: int
    margin_pct: float | None = Field(None, ge=10, le=35)
    option_increase_pct: float | None = Field(None, ge=0, le=15)
    section_d: dict | None = None
    section_a_overrides: dict | None = None
    section_b_overrides: dict | None = None


class ProposalGenerateRequest(BaseModel):
    config: dict


class ProposalSaveRequest(BaseModel):
    proposal_html: str | None = None
    sections_json: dict | None = None
    notes: str | None = None
    status: str | None = None
    winning_bid_amount: float | None = None


class HumanizeRequest(BaseModel):
    text: str = Field(..., min_length=1)


class RegenerateSectionRequest(BaseModel):
    section_key: str


class RestoreVersionRequest(BaseModel):
    version_index: int = Field(..., ge=0)


class ProposalExportRequest(BaseModel):
    sections_json: dict[str, str] | None = None


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
        "all_naics_codes": settings["all_naics_codes"],
        "naics_tiers": settings["naics_tiers"],
        "naics_groups": settings["naics_groups"],
        "naics_tier_schedule": settings["naics_tier_schedule"],
        "naics_labels": settings["naics_labels"],
        "default_min_days": settings["min_days_until_due"],
        "default_min_score": settings["min_score_threshold"],
        "naics_sync": sync_status,
        "auth_enabled": auth_enabled(),
        "build_version": APP_BUILD_VERSION,
    }


@app.get("/api/contracts")
def get_contracts(
    naics: str | None = Query(None, description="Comma-separated NAICS codes"),
    min_days: int | None = Query(None, ge=0, le=365),
    min_score: int | None = Query(None, ge=1, le=10),
    agency: str | None = Query(None),
    pursue_only: bool = Query(False),
    tier: int | None = Query(None, ge=1, le=3, description="Filter by search tier"),
    status: str | None = Query(None, description="needs_subs, ready_to_bid, submitted, awarded, active"),
    set_aside: str | None = Query(None, description="Set-aside type filter"),
):
    if naics == "__none__":
        naics_codes: list[str] | None = []
    elif naics:
        naics_codes = [c.strip() for c in naics.split(",") if c.strip()]
    else:
        naics_codes = None
    session = SessionLocal()
    try:
        rows = list_contracts(
            session,
            naics_codes=naics_codes,
            min_days_until_due=min_days,
            min_score=min_score,
            agency=agency,
            pursue_only=pursue_only,
            tier=tier,
            status_filter=status,
            set_aside_filter=set_aside,
        )
        from api_budget import get_usage_snapshot
        from intake import enrich_matching_attachments, start_background_attachment_enrich

        notice_ids = [r.notice_id for r in rows]
        from api_budget import attachment_enrich_on_list_limit

        attachment_refresh = enrich_matching_attachments(
            session, notice_ids, limit=attachment_enrich_on_list_limit()
        )
        if attachment_refresh.get("attachments_pending", 0) > 0:
            start_background_attachment_enrich()

        from screening_pipeline import is_dashboard_ready

        processing_rows = list_contracts(
            session,
            naics_codes=naics_codes,
            min_days_until_due=min_days,
            min_score=min_score,
            agency=agency,
            pursue_only=pursue_only,
            tier=tier,
            status_filter=status,
            set_aside_filter=set_aside,
            require_dashboard_ready=False,
            require_scrape_complete=False,
        )
        processing_count = sum(1 for r in processing_rows if not is_dashboard_ready(r))

        ready_pool = list_contracts(
            session,
            naics_codes=naics_codes,
            min_days_until_due=0,
            min_score=min_score,
            agency=agency,
            pursue_only=pursue_only,
            tier=tier,
            status_filter=status,
            set_aside_filter=set_aside,
        )
        hidden_by_min_days = max(0, len(ready_pool) - len(rows)) if min_days else 0

        return {
            "count": len(rows),
            "processing_count": processing_count,
            "contracts": [contract_to_dict(r) for r in rows],
            "api_budget": get_usage_snapshot(),
            "attachments_refreshed": attachment_refresh.get("attachments_enriched", 0),
            "filter_stats": {
                "ready_eligible": len(ready_pool),
                "hidden_by_min_days": hidden_by_min_days,
                "total_matching_naics": len(processing_rows),
            },
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


@app.post("/api/contracts/{notice_id}/extract-solicitation")
def extract_contract_solicitation(notice_id: str, force: bool = Query(False)):
    """Pull CO, dates, and PWS scope from bid PDFs via Claude."""
    session = SessionLocal()
    try:
        from models import Contract
        from proposal_service import ensure_solicitation_meta

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        from api_budget import ScreenBudgetExceeded

        try:
            meta = ensure_solicitation_meta(session, row, force=force)
        except ScreenBudgetExceeded as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        session.refresh(row)
        data = contract_to_dict(row)
        analysis = row.analysis if isinstance(row.analysis, dict) else {}
        sol = analysis.get("solicitation_meta") if isinstance(analysis.get("solicitation_meta"), dict) else {}
        pws = analysis.get("pws_extraction") if isinstance(analysis.get("pws_extraction"), dict) else {}
        return {
            "solicitation_meta": meta,
            "pws_extraction": pws,
            "base_year_start": sol.get("base_year_start") or meta.get("base_year_start"),
            "base_year_end": sol.get("base_year_end") or meta.get("base_year_end"),
            "contract": data,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Extraction failed: {exc}") from exc
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
        intel = get_full_pricing_intel(row, session, force_refresh=refresh)
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


@app.patch("/api/contracts/{notice_id}")
def patch_contract(notice_id: str, body: ContractOutcomeUpdate):
    session = SessionLocal()
    try:
        from models import Contract
        from pws_fields import recalculate_pricing_derivatives

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        if body.status is not None:
            row.status = body.status
        if body.awarded_amount is not None:
            from decimal import Decimal

            row.awarded_amount = Decimal(str(body.awarded_amount))
            recalculate_pricing_derivatives(row)
        if body.margin_percentage is not None:
            from decimal import Decimal

            row.margin_percentage = Decimal(str(body.margin_percentage))
        session.commit()
        return contract_to_dict(row)
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/pricing/dashboard")
def pricing_dashboard():
    session = SessionLocal()
    try:
        return get_pricing_dashboard(session)
    finally:
        session.close()


@app.get("/api/export/claude")
def export_claude_portfolio():
    """Download a complete JSON snapshot of GovTracker for Claude Projects."""
    session = SessionLocal()
    try:
        from claude_export import export_claude_json_bytes

        data, filename = export_claude_json_bytes(session, include_attachment_text=True)
        return Response(
            content=data,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}") from exc
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


@app.post("/api/contracts/{notice_id}/full-analysis")
def run_force_full_analysis(notice_id: str):
    """Manual override — PIEE/PDF download + full Claude analysis regardless of text score."""
    try:
        from api_budget import ScreenBudgetExceeded

        return force_full_analysis(notice_id)
    except ScreenBudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Full analysis failed: {exc}") from exc


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
        from agreement_service import agreement_for_link, agreement_to_dict

        agreement_info = agreement_to_dict(agreement_for_link(session, link_id), link)
        return contract_sub_to_dict(link, agreement=agreement_info)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/sub-contacts/{contact_id}")
def get_sub_contact(contact_id: int):
    session = SessionLocal()
    try:
        from sub_contact_service import get_sub_contact_detail

        return get_sub_contact_detail(session, contact_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.patch("/api/sub-contacts/{contact_id}")
def patch_sub_contact(contact_id: int, body: SubContactUpdate):
    session = SessionLocal()
    try:
        from sub_contact_service import get_sub_contact_detail, update_sub_contact

        update_sub_contact(session, contact_id, body.model_dump(exclude_unset=True))
        return get_sub_contact_detail(session, contact_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/sub-contacts/{contact_id}/select")
def select_sub_contact_route(contact_id: int):
    session = SessionLocal()
    try:
        from sub_contact_service import get_sub_contact_detail, select_sub_contact

        select_sub_contact(session, contact_id)
        return get_sub_contact_detail(session, contact_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/sub-contacts/{contact_id}/deselect")
def deselect_sub_contact_route(contact_id: int):
    session = SessionLocal()
    try:
        from sub_contact_service import deselect_sub_contact, get_sub_contact_detail

        deselect_sub_contact(session, contact_id)
        return get_sub_contact_detail(session, contact_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/sub-contacts/{contact_id}/scope-email")
def get_scope_email(contact_id: int):
    session = SessionLocal()
    try:
        from sub_contact_service import generate_scope_email

        return generate_scope_email(session, contact_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/sub-contacts/{contact_id}/followup-email")
def get_followup_email(contact_id: int):
    session = SessionLocal()
    try:
        from sub_contact_service import generate_followup_email

        return generate_followup_email(session, contact_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/sub-contacts/{contact_id}/voicemail-script")
def get_voicemail_script(contact_id: int):
    session = SessionLocal()
    try:
        from sub_contact_service import generate_voicemail_script

        return generate_voicemail_script(session, contact_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/sub-contacts/{contact_id}/mark-email-sent")
def mark_sub_email_sent(contact_id: int, body: MarkEmailSentRequest):
    session = SessionLocal()
    try:
        from sub_contact_service import get_sub_contact_detail, mark_email_sent

        mark_email_sent(session, contact_id, sent=body.sent)
        return get_sub_contact_detail(session, contact_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/sub-checklist/bypass")
def bypass_sub_checklist(notice_id: str):
    session = SessionLocal()
    try:
        from sub_contact_service import bypass_pre_bid_checklist, list_sub_contacts_for_contract

        bypass_pre_bid_checklist(session, notice_id)
        return list_sub_contacts_for_contract(session, notice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}/quote-comparison")
def get_quote_comparison(notice_id: str):
    session = SessionLocal()
    try:
        from sub_contact_service import list_sub_contacts_for_contract

        data = list_sub_contacts_for_contract(session, notice_id)
        return {"notice_id": notice_id, "rows": data.get("quote_comparison", [])}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}/submission-checklist")
def get_submission_checklist(notice_id: str):
    session = SessionLocal()
    try:
        from models import Contract
        from submission_package import checklist_view, submission_package_dict

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        return {
            "notice_id": notice_id,
            "contract_title": row.title,
            "checklist": checklist_view(row),
            "package": submission_package_dict(row, session),
        }
    finally:
        session.close()


@app.patch("/api/contracts/{notice_id}/submission-checklist/{item_key}")
def patch_submission_checklist_item(notice_id: str, item_key: str, body: ChecklistItemUpdate):
    session = SessionLocal()
    try:
        from models import Contract
        from submission_package import checklist_view, update_checklist_item

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        update_checklist_item(row, item_key, body.model_dump(exclude_unset=True))
        session.commit()
        return {"checklist": checklist_view(row)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}/co-questions")
def get_co_questions(notice_id: str):
    session = SessionLocal()
    try:
        from models import Contract
        from submission_package import deadline_display, submission_package_dict

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        return {
            "notice_id": notice_id,
            "questions": row.co_questions or [],
            "questions_deadline": row.questions_deadline.isoformat() if row.questions_deadline else None,
            "deadline": deadline_display(row),
            "note": "Email questions to the CO listed in the solicitation before the questions deadline. "
            "Only ask questions not clearly answered in the solicitation documents.",
        }
    finally:
        session.close()


@app.patch("/api/contracts/{notice_id}/co-questions/{question_id}")
def patch_co_question(notice_id: str, question_id: str, body: CoQuestionUpdate):
    session = SessionLocal()
    try:
        from models import Contract
        from submission_package import update_co_question

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        questions = update_co_question(row, question_id, body.model_dump(exclude_unset=True))
        session.commit()
        return {"questions": questions}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/co-questions/regenerate")
def regenerate_co_questions(notice_id: str):
    session = SessionLocal()
    try:
        from models import Contract
        from submission_package import apply_submission_package, generate_co_questions

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        analysis = row.analysis if isinstance(row.analysis, dict) else {}
        row.co_questions = generate_co_questions(row, analysis)
        apply_submission_package(row, session, analysis=analysis)
        session.commit()
        return {"questions": row.co_questions}
    finally:
        session.close()


@app.patch("/api/contracts/{notice_id}/submission-meta")
def patch_submission_meta(notice_id: str, body: SubmissionMetaUpdate):
    session = SessionLocal()
    try:
        from models import Contract
        from submission_package import submission_package_dict

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        payload = body.model_dump(exclude_unset=True)
        if "submission_method_confirmed" in payload:
            row.submission_method_confirmed = bool(payload["submission_method_confirmed"])
        if "submission_method_notes" in payload:
            row.submission_method_notes = payload["submission_method_notes"]
        if "submission_method" in payload:
            row.submission_method = payload["submission_method"]
        if "submission_email" in payload:
            row.submission_email = payload["submission_email"]
        session.commit()
        return submission_package_dict(row, session)
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}/attachments/{attachment_id}/download")
def download_contract_attachment(notice_id: str, attachment_id: int):
    session = SessionLocal()
    try:
        from models import Contract
        from submission_package import get_attachment_bytes

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        att = get_attachment_bytes(session, row.id, attachment_id)
        from fastapi.responses import Response

        filename = att.filename or "attachment.pdf"
        media = att.content_type or "application/pdf"
        return Response(
            content=bytes(att.file_bytes),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
def patch_sub(sub_id: int, body: SubProfileUpdate):
    session = SessionLocal()
    try:
        from agreement_service import update_sub_profile
        from sub_serializers import sub_to_dict

        payload = body.model_dump(exclude_unset=True)
        if len(payload) == 1 and "notes" in payload:
            from sub_finder import update_sub_notes

            row = update_sub_notes(session, sub_id, payload.get("notes"))
        else:
            row = update_sub_profile(session, sub_id, payload)
        return sub_to_dict(row)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/contract-subs/{link_id}/agreement")
def get_subcontract_agreement(link_id: int):
    session = SessionLocal()
    try:
        from agreement_service import agreement_for_link, agreement_to_dict, build_agreement_config
        from models import ContractSub

        link = session.get(ContractSub, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="Contract sub link not found")
        row = agreement_for_link(session, link_id)
        config = None
        try:
            config = build_agreement_config(session, link_id)
        except ValueError:
            pass
        return {
            "agreement": agreement_to_dict(row, link),
            "preview_config": config,
        }
    finally:
        session.close()


@app.post("/api/contract-subs/{link_id}/agreement/generate")
def generate_subcontract_agreement_endpoint(link_id: int):
    session = SessionLocal()
    try:
        from agreement_service import generate_agreement
        from api_budget import ScreenBudgetExceeded

        return generate_agreement(session, link_id, resend=False)
    except ScreenBudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Agreement generation failed: {exc}") from exc
    finally:
        session.close()


@app.post("/api/contract-subs/{link_id}/agreement/resend")
def resend_subcontract_agreement(link_id: int):
    session = SessionLocal()
    try:
        from agreement_service import generate_agreement
        from api_budget import ScreenBudgetExceeded

        return generate_agreement(session, link_id, resend=True)
    except ScreenBudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Agreement generation failed: {exc}") from exc
    finally:
        session.close()


@app.get("/api/contract-subs/{link_id}/agreement/pdf")
def download_subcontract_agreement_pdf(link_id: int):
    session = SessionLocal()
    try:
        from agreement_export import agreement_meta, build_agreement_pdf
        from agreement_service import agreement_for_link
        from models import ContractSub

        link = session.get(ContractSub, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="Contract sub link not found")
        row = agreement_for_link(session, link_id)
        if not row or not row.agreement_html:
            raise HTTPException(status_code=404, detail="No agreement generated yet")
        if row.pdf_bytes:
            pdf_bytes = row.pdf_bytes
        else:
            pdf_bytes, _engine = build_agreement_pdf(row)
            row.pdf_bytes = pdf_bytes
            session.commit()
        meta = agreement_meta(row)
        filename = meta["filenames"]["pdf"]
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    finally:
        session.close()


@app.patch("/api/contract-subs/{link_id}/agreement/status")
def patch_agreement_signature_status(link_id: int, body: AgreementSignatureUpdate):
    session = SessionLocal()
    try:
        from agreement_service import agreement_for_link, agreement_to_dict, update_agreement_signature_status
        from sub_serializers import contract_sub_to_dict

        link = update_agreement_signature_status(session, link_id, body.agreement_signature_status)
        agreement_info = agreement_to_dict(agreement_for_link(session, link_id), link)
        return contract_sub_to_dict(link, agreement=agreement_info)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.patch("/api/settings/owner")
def patch_owner_settings(body: OwnerSettingsUpdate):
    from settings_store import save_owner_settings

    return save_owner_settings(body.model_dump(exclude_unset=True))


@app.get("/api/contracts/{notice_id}/proposal/subs")
def get_proposal_subs(notice_id: str):
    session = SessionLocal()
    try:
        from proposal_service import quoted_subs_for_contract

        return quoted_subs_for_contract(session, notice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/proposal/config")
def post_proposal_config(notice_id: str, body: ProposalConfigRequest):
    session = SessionLocal()
    try:
        from proposal_service import build_proposal_config

        config = build_proposal_config(
            session,
            notice_id,
            contract_sub_id=body.contract_sub_id,
            margin_pct=body.margin_pct,
            option_increase_pct=body.option_increase_pct,
        )
        if body.section_a_overrides:
            config["section_a"].update(body.section_a_overrides)
        if body.section_b_overrides:
            config["section_b"].update(body.section_b_overrides)
        if body.section_d:
            config["section_d"].update(body.section_d)
        from models import Contract
        from proposal_service import build_proposal_readiness, detect_missing_fields, sync_config_from_contract

        contract = session.query(Contract).filter_by(notice_id=notice_id).first()
        if contract:
            config = sync_config_from_contract(config, contract)
            config["readiness"] = build_proposal_readiness(contract, config)
        config["missing_fields"] = detect_missing_fields(config, contract=contract)
        return config
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/proposal/generate")
def post_generate_proposal(notice_id: str, body: ProposalGenerateRequest):
    session = SessionLocal()
    try:
        from proposal_service import generate_proposal, proposal_to_dict

        proposal = generate_proposal(session, notice_id, body.config)
        return proposal_to_dict(proposal)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=502, detail=f"Proposal generation failed: {exc}") from exc
    finally:
        session.close()


@app.get("/api/proposals/{proposal_id}")
def get_proposal(proposal_id: int):
    session = SessionLocal()
    try:
        from models import Proposal
        from proposal_service import proposal_to_dict
        from sqlalchemy.orm import joinedload

        row = (
            session.query(Proposal)
            .options(joinedload(Proposal.contract))
            .filter_by(id=proposal_id)
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="Proposal not found")
        return proposal_to_dict(row)
    finally:
        session.close()


@app.patch("/api/proposals/{proposal_id}")
def patch_proposal(proposal_id: int, body: ProposalSaveRequest):
    session = SessionLocal()
    try:
        from proposal_service import proposal_to_dict, save_proposal_draft

        row = save_proposal_draft(session, proposal_id, body.model_dump(exclude_unset=True))
        return proposal_to_dict(row)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/proposals/{proposal_id}/regenerate-section")
def post_regenerate_section(proposal_id: int, body: RegenerateSectionRequest):
    session = SessionLocal()
    try:
        from proposal_service import proposal_to_dict, regenerate_section

        row = regenerate_section(session, proposal_id, body.section_key)
        return proposal_to_dict(row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/proposals/{proposal_id}/humanize")
def post_humanize(proposal_id: int, body: HumanizeRequest):
    session = SessionLocal()
    try:
        from proposal_service import humanize_selection

        return {"html": humanize_selection(session, proposal_id, body.text)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}/proposal/latest")
def get_latest_proposal(notice_id: str):
    session = SessionLocal()
    try:
        from models import Contract, Proposal
        from proposal_service import proposal_to_dict
        from sqlalchemy.orm import joinedload

        contract = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not contract:
            raise HTTPException(status_code=404, detail="Contract not found")
        row = (
            session.query(Proposal)
            .options(joinedload(Proposal.contract))
            .filter_by(contract_id=contract.id)
            .order_by(Proposal.date_updated.desc())
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="No proposal for this contract")
        return proposal_to_dict(row)
    finally:
        session.close()


@app.post("/api/proposals/{proposal_id}/restore-version")
def post_restore_version(proposal_id: int, body: RestoreVersionRequest):
    session = SessionLocal()
    try:
        from proposal_service import proposal_to_dict, restore_proposal_version

        row = restore_proposal_version(session, proposal_id, body.version_index)
        return proposal_to_dict(row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/proposals/{proposal_id}/reduce-ai-score")
def post_reduce_ai(proposal_id: int):
    session = SessionLocal()
    try:
        from proposal_service import proposal_to_dict, reduce_ai_score_pass

        row = reduce_ai_score_pass(session, proposal_id)
        return proposal_to_dict(row)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        session.close()


def _proposal_export_response(proposal_id: int, body: ProposalExportRequest | None, exporter):
    session = SessionLocal()
    try:
        from models import Proposal
        from proposal_export import export_meta, resolve_sections
        from sqlalchemy.orm import joinedload

        row = (
            session.query(Proposal)
            .options(joinedload(Proposal.contract))
            .filter_by(id=proposal_id)
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="Proposal not found")
        sections = resolve_sections(row, body.sections_json if body else None)
        if not sections and not row.proposal_html:
            raise HTTPException(status_code=400, detail="Proposal has no content to export")
        meta = export_meta(row)
        content, media_type, filename = exporter(row, sections, meta)
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}") from exc
    finally:
        session.close()


@app.post("/api/proposals/{proposal_id}/export/docx")
def export_proposal_docx(proposal_id: int, body: ProposalExportRequest | None = None):
    from proposal_export import build_proposal_docx

    def _export(row, sections, meta):
        data = build_proposal_docx(row, sections, meta)
        return (
            data,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            meta["filenames"]["docx"],
        )

    return _proposal_export_response(proposal_id, body, _export)


@app.post("/api/proposals/{proposal_id}/export/pdf")
def export_proposal_pdf(proposal_id: int, body: ProposalExportRequest | None = None):
    from proposal_export import build_proposal_pdf

    def _export(row, sections, meta):
        data, _engine = build_proposal_pdf(row, sections, meta)
        return data, "application/pdf", meta["filenames"]["pdf"]

    return _proposal_export_response(proposal_id, body, _export)


@app.post("/api/proposals/{proposal_id}/export/capability-pdf")
def export_capability_pdf(proposal_id: int, body: ProposalExportRequest | None = None):
    from proposal_export import build_capability_pdf

    def _export(row, sections, meta):
        data, _engine = build_capability_pdf(row, sections, meta)
        return data, "application/pdf", meta["filenames"]["capability"]

    return _proposal_export_response(proposal_id, body, _export)


@app.get("/api/performance/alerts")
def performance_alerts():
    from performance_settings import ipp_reminder_active, wawf_password_status

    return {
        "wawf_warning": wawf_password_status(),
        "ipp_reminder": ipp_reminder_active(),
    }


@app.get("/api/performance/dashboard")
def get_performance_dashboard():
    session = SessionLocal()
    try:
        from performance_service import performance_dashboard

        return performance_dashboard(session)
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}/performance")
def read_contract_performance(notice_id: str):
    session = SessionLocal()
    try:
        from performance_service import get_contract_performance

        return get_contract_performance(session, notice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.patch("/api/contracts/{notice_id}/performance")
def patch_contract_performance(notice_id: str, body: PerformanceUpdate):
    session = SessionLocal()
    try:
        from performance_service import get_contract_performance, update_contract_performance

        update_contract_performance(session, notice_id, body.model_dump(exclude_unset=True))
        session.commit()
        return get_contract_performance(session, notice_id)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/invoices")
def post_contract_invoice(notice_id: str, body: InvoiceCreate):
    session = SessionLocal()
    try:
        from performance_service import create_invoice, invoice_to_dict

        row = create_invoice(session, notice_id, body.model_dump(exclude_unset=True))
        session.commit()
        return invoice_to_dict(row)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.patch("/api/invoices/{invoice_id}")
def patch_invoice(invoice_id: int, body: InvoiceUpdate):
    session = SessionLocal()
    try:
        from performance_service import invoice_to_dict, update_invoice

        row = update_invoice(session, invoice_id, body.model_dump(exclude_unset=True))
        session.commit()
        return invoice_to_dict(row)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/sub-payments")
def post_sub_payment(notice_id: str, body: SubPaymentCreate):
    session = SessionLocal()
    try:
        from performance_service import create_sub_payment, sub_payment_to_dict

        row = create_sub_payment(session, notice_id, body.model_dump(exclude_unset=True))
        session.commit()
        return sub_payment_to_dict(row)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.patch("/api/sub-payments/{payment_id}")
def patch_sub_payment(payment_id: int, body: SubPaymentUpdate):
    session = SessionLocal()
    try:
        from performance_service import sub_payment_to_dict, update_sub_payment

        row = update_sub_payment(session, payment_id, body.model_dump(exclude_unset=True))
        session.commit()
        return sub_payment_to_dict(row)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/option-year/exercise")
def exercise_option_year(notice_id: str):
    session = SessionLocal()
    try:
        from performance_service import exercise_option_year, get_contract_performance

        exercise_option_year(session, notice_id)
        session.commit()
        return get_contract_performance(session, notice_id)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}/stop-work-notice")
def stop_work_notice(notice_id: str, invoice_id: int | None = Query(None)):
    session = SessionLocal()
    try:
        from performance_service import generate_stop_work_notice

        return generate_stop_work_notice(session, notice_id, invoice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}/signoff-request/{payment_id}")
def signoff_request(notice_id: str, payment_id: int):
    session = SessionLocal()
    try:
        from performance_service import generate_signoff_request

        return generate_signoff_request(session, notice_id, payment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/amendments/dismiss")
def dismiss_amendments(notice_id: str):
    session = SessionLocal()
    try:
        from amendment_monitor import dismiss_amendment_alert
        from sync import contract_to_dict

        dismiss_amendment_alert(session, notice_id)
        session.commit()
        from models import Contract

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        return contract_to_dict(row) if row else {"notice_id": notice_id, "amendment_alert_active": False}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/settings/performance")
def read_performance_settings():
    from performance_settings import get_performance_settings

    return get_performance_settings()


@app.put("/api/settings/performance")
def update_performance_settings(body: PerformanceSettingsUpdate):
    from performance_settings import save_performance_settings

    return save_performance_settings(
        wawf_last_password_change=body.wawf_last_password_change,
        ipp_registered=body.ipp_registered,
    )


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

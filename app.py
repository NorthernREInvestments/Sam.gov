"""GovTracker web API and dashboard."""

from __future__ import annotations
from application_clock import now_utc, today_local

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
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
from pricing import get_full_pricing_intel, get_pricing_dashboard, lookup_prior_contract_by_number
from sync import contract_to_dict, get_naics_sync_status, list_contracts, sync_all_naics, sync_from_sam
from screen import force_full_analysis, screen_one, screen_pending

STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_BUILD_VERSION = "20260917-m3-competitive-intelligence"

_startup_lock = threading.Lock()
_startup_state = {"ready": False, "error": None}


def _autopilot_summary() -> dict[str, Any]:
    try:
        from autopilot_service import get_autopilot_status

        status = get_autopilot_status()
        return {
            "running": status.get("running"),
            "round": status.get("round"),
            "error": status.get("error"),
            "repair_running": (status.get("repair") or {}).get("running"),
        }
    except Exception:
        return {"running": False}


def _run_background_startup() -> None:
    global _startup_state
    log = logging.getLogger("govtracker")
    try:
        from database import init_db

        init_db()
        start_scheduler()
        with _startup_lock:
            _startup_state = {"ready": True, "error": None}
        log.info("Application startup complete (%s)", APP_BUILD_VERSION)
        print(f"govtracker: startup complete ({APP_BUILD_VERSION})", flush=True)

        from pricing_backfill_service import start_background_pricing_backfill

        start_background_pricing_backfill()
        threading.Thread(target=_deferred_background_startup, name="govtracker-startup-deferred", daemon=True).start()
        try:
            from m3_discovery_service import maybe_startup_discovery

            maybe_startup_discovery()
        except Exception:
            log.exception("M3 startup discovery check failed")
        try:
            from m3_research_service import maybe_startup_research

            maybe_startup_research()
        except Exception:
            log.exception("M3 startup research check failed")
    except Exception as exc:
        log.exception("Background startup failed")
        with _startup_lock:
            _startup_state = {"ready": False, "error": str(exc)}
        print(f"govtracker: startup failed: {exc}", flush=True)


def _deferred_background_startup() -> None:
    """Heavy deploy work — must not block health checks or mark startup ready."""
    log = logging.getLogger("govtracker")
    try:
        from autopilot_service import start_autopilot

        start_autopilot(trigger="deploy")
    except Exception:
        log.exception("Deferred autopilot startup failed")
    try:
        from api_budget import can_spend_sam
        from document_intel import repair_stored_piee_hints
        from intake import start_background_attachment_enrich

        repaired = repair_stored_piee_hints()
        if repaired:
            log.info("Stamped PIEE hints on %s existing contract(s)", repaired)
        if can_spend_sam(1):
            from sam_scarcity import sam_scarcity_mode

            if sam_scarcity_mode():
                log.info("Skipping startup SAM attachment enrich — SAM scarcity mode")
            else:
                start_background_attachment_enrich()
    except Exception:
        log.exception("Deferred attachment/PIEE startup failed")


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
    print(f"govtracker: accepting traffic ({APP_BUILD_VERSION})", flush=True)
    threading.Thread(target=_run_background_startup, name="govtracker-startup", daemon=True).start()
    yield
    stop_scheduler()


app = FastAPI(title="GovTracker", version="0.1.0", lifespan=lifespan)

from product_api import router as product_router

app.include_router(product_router)
from deals_api import router as deals_router
from os_api import router as os_router
from discovery_api import router as discovery_router

app.include_router(deals_router)
app.include_router(os_router)
app.include_router(discovery_router)
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


class PriorContractLookup(BaseModel):
    contract_number: str = Field(..., min_length=4, max_length=64)


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
    with _startup_lock:
        state = dict(_startup_state)
    status = "ok" if state.get("ready") else "starting"
    from operating_mode import mode_snapshot
    from legacy_runtime import is_m3_only_production, legacy_retirement_snapshot

    payload = {
        "status": status,
        "service": "M3",
        "application": "M3",
        "parent_holding": "Northern RE Investments",
        "m3_only_production": is_m3_only_production(),
        "auth_enabled": auth_enabled(),
        "build_version": APP_BUILD_VERSION,
        "startup_ready": state.get("ready", False),
        "operating_mode": mode_snapshot(),
    }
    if state.get("error"):
        payload["startup_error"] = state["error"]
    try:
        from m3_procurement_profile import assert_m3_isolated_from_legacy, load_m3_procurement_profile

        payload["procurement_profile"] = {
            "kind": load_m3_procurement_profile().get("kind"),
            "isolated": assert_m3_isolated_from_legacy().get("ok"),
        }
    except Exception as exc:
        payload["procurement_profile"] = {"error": str(exc)}
    try:
        from m3_pipeline_store import M3PipelineStore

        store = M3PipelineStore()
        if hasattr(store, "reload_from_durable"):
            store.reload_from_durable()
        payload["pipeline_store"] = {
            "available": True,
            "opportunity_count": len(store.all()),
            "path": str(store.path),
        }
    except Exception as exc:
        payload["pipeline_store"] = {"available": False, "error": str(exc)}
    try:
        from gs_watchlist_service import watchlist_status

        wl = watchlist_status()
        payload["watchlist"] = {
            "table": wl.get("table"),
            "priority_target_count": wl.get("priority_target_count"),
        }
    except Exception:
        payload["watchlist"] = {"table": None, "priority_target_count": 0}
    payload["legacy_retirement"] = {
        "m3_only_production": is_m3_only_production(),
        "active_application": "M3",
    }
    return payload


@app.get("/api/m3/health")
def api_m3_health():
    """Lightweight M3 readiness — no secrets."""
    from operating_mode import is_controlled_verification, is_development_no_outreach, mode_snapshot
    from legacy_runtime import is_m3_only_production, legacy_retirement_snapshot
    from m3_procurement_profile import assert_m3_isolated_from_legacy, load_m3_procurement_profile
    from m3_pipeline_store import M3PipelineStore

    with _startup_lock:
        state = dict(_startup_state)
    profile = load_m3_procurement_profile()
    store = M3PipelineStore()
    try:
        store.reload_from_durable()
    except Exception:
        pass
    return {
        "kind": "M3Health",
        "ok": True,
        "backend_running": True,
        "m3_application_loaded": True,
        "m3_only_production": is_m3_only_production(),
        "build_version": APP_BUILD_VERSION,
        "startup_ready": bool(state.get("ready")),
        "operating_mode": mode_snapshot().get("operating_mode"),
        "DEVELOPMENT_NO_OUTREACH": is_development_no_outreach(),
        "controlled_verification_active": is_controlled_verification(),
        "procurement_profile_available": profile.get("kind") == "M3ProcurementProfile",
        "procurement_isolation_ok": assert_m3_isolated_from_legacy().get("ok"),
        "pipeline_store_available": True,
        "pipeline_opportunity_count": len(store.all()),
        "legacy_runtime": legacy_retirement_snapshot(),
        "discovery": {
            "enabled": True,
            "note": "see /api/m3/discovery/status",
        },
        "research": {
            "enabled": True,
            "note": "see /api/m3/research/status",
        },
        "evidence": {
            "enabled": True,
            "note": "see /api/m3/evidence/status",
        },
        "external_action_safety": {
            "emails_sent": 0,
            "bids_submitted": 0,
            "network_transmitted": False,
        },
    }


@app.get("/api/watchlist/priority-targets")
def watchlist_priority_targets():
    from gs_watchlist_service import watchlist_status

    return watchlist_status()


@app.get("/api/national-discovery/changes-requiring-review")
def api_changes_requiring_review():
    from national_discovery_api import changes_requiring_review_payload

    return changes_requiring_review_payload()


@app.get("/api/national-discovery/tracked/{deal_id}/timeline")
def api_tracked_timeline(deal_id: str):
    from national_discovery_api import get_tracked_store

    store = get_tracked_store()
    return {"deal_id": deal_id, "timeline": store.timeline_for(deal_id), "tracked": store.get(deal_id)}


@app.post("/api/national-discovery/changes/{change_id}/acknowledge")
def api_acknowledge_change(change_id: str):
    from national_discovery_api import get_tracked_store

    store = get_tracked_store()
    result = store.acknowledge(change_id, operator_id="operator")
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/national-discovery/source-registry/summary")
def api_source_registry_summary():
    from procurement_source_registry import bootstrap_registry
    from source_network_audit import coverage_metrics_honest

    reg = bootstrap_registry()
    summary = reg.coverage_summary()
    summary["honest_coverage"] = coverage_metrics_honest(reg)
    summary["claim_100_percent_national_coverage"] = False
    return summary


@app.get("/api/national-discovery/source-network/audit")
def api_source_network_audit():
    from procurement_source_registry import ProcurementSourceRegistry
    from source_network_audit import audit_source_network

    return audit_source_network(ProcurementSourceRegistry())


@app.get("/api/national-discovery/platform-families")
def api_platform_families():
    from source_network_audit import platform_family_inventory, platform_leverage_ranking

    inv = platform_family_inventory()
    return {"inventory": inv, "leverage": platform_leverage_ranking(inv)}


@app.get("/api/national-discovery/adapter-health")
def api_adapter_health():
    from discovery.live_fetchers import list_live_capable_fetchers
    from procurement_source_registry import ProcurementSourceRegistry

    reg = ProcurementSourceRegistry()
    adapters = list_live_capable_fetchers()
    by_adapter = {}
    for s in reg.all_sources():
        aid = s.get("adapter_family") or "NONE"
        slot = by_adapter.setdefault(
            aid,
            {"adapter_id": aid, "sources": 0, "healthy": 0, "partial": 0, "unknown": 0, "last_success": None},
        )
        slot["sources"] += 1
        h = s.get("health_state")
        if h == "HEALTHY_PRODUCTION":
            slot["healthy"] += 1
        elif h == "PARTIALLY_PRODUCTIVE":
            slot["partial"] += 1
        elif h in {None, "UNKNOWN", "DISCOVERED_UNVALIDATED"}:
            slot["unknown"] += 1
        ls = s.get("last_success_at")
        if ls and (not slot["last_success"] or str(ls) > str(slot["last_success"])):
            slot["last_success"] = ls
    return {"live_fetchers": adapters, "by_adapter": list(by_adapter.values())}


@app.get("/api/national-discovery/coverage-gaps")
def api_coverage_gaps():
    from coverage_gap_intelligence import build_coverage_gap_report
    from procurement_source_registry import ProcurementSourceRegistry

    return build_coverage_gap_report(ProcurementSourceRegistry())


@app.get("/api/national-discovery/product-yield")
def api_product_yield():
    from procurement_source_registry import ProcurementSourceRegistry

    reg = ProcurementSourceRegistry()
    rows = []
    for s in reg.all_sources():
        rows.append(
            {
                "source_id": s["source_id"],
                "platform_family": s.get("platform_family"),
                "records_discovered": s.get("records_discovered") or 0,
                "health_state": s.get("health_state"),
                "product_yield_rate": s.get("product_yield_rate"),
                "resolution_state": s.get("resolution_state"),
            }
        )
    return {"sources": rows, "note": "Yield rates populated after discovery runs"}


@app.get("/api/national-discovery/unknown-resolution")
def api_unknown_resolution():
    from pathlib import Path

    path = Path(__file__).resolve().parent / "artifacts" / "unknown_source_resolution.json"
    if path.exists():
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    from collections import Counter

    from procurement_source_registry import ProcurementSourceRegistry

    reg = ProcurementSourceRegistry()
    by = Counter(s.get("resolution_state") or s.get("health_state") for s in reg.all_sources())
    return {"by_resolution": dict(by), "note": "Run round2 validation to refresh artifact"}


@app.get("/api/national-discovery/family-health")
def api_family_health():
    from unknown_source_resolution import round2_leverage_ranking
    from procurement_source_registry import ProcurementSourceRegistry

    reg = ProcurementSourceRegistry()
    return round2_leverage_ranking(reg)


@app.get("/api/national-discovery/product-category-yield")
def api_product_category_yield():
    from pathlib import Path
    import json

    path = Path(__file__).resolve().parent / "artifacts" / "product_category_yield.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"by_category": [], "note": "No live yield artifact yet"}


@app.get("/api/national-discovery/false-positive-audit")
def api_false_positive_audit():
    from pathlib import Path
    import json

    path = Path(__file__).resolve().parent / "artifacts" / "product_false_positive_audit.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"counts": {}, "note": "No audit artifact yet"}


@app.get("/api/national-discovery/entity-coverage")
def api_entity_coverage():
    from entity_geographic_coverage import entity_type_coverage
    from procurement_source_registry import ProcurementSourceRegistry

    return entity_type_coverage(ProcurementSourceRegistry())


@app.get("/api/national-discovery/geographic-coverage")
def api_geographic_coverage():
    from entity_geographic_coverage import geographic_coverage
    from procurement_source_registry import ProcurementSourceRegistry

    return geographic_coverage(ProcurementSourceRegistry())


@app.get("/api/national-discovery/live-product-examples")
def api_live_product_examples():
    from pathlib import Path
    import json

    path = Path(__file__).resolve().parent / "artifacts" / "live_product_example_set.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"examples": [], "note": "No example set yet"}


@app.get("/api/m3/discovery/status")
def api_m3_discovery_status():
    from m3_discovery_service import discovery_status

    return discovery_status()


@app.post("/api/m3/discovery/run")
def api_m3_discovery_run(body: dict | None = None):
    """Operator RUN NOW — optional body {profile, bootstrap} for national market bootstrap."""
    from m3_discovery_service import TRIGGER_MANUAL, request_discovery_run

    payload = body or {}
    profile = payload.get("profile")
    bootstrap = bool(payload.get("bootstrap"))
    return request_discovery_run(
        trigger_type=TRIGGER_MANUAL,
        profile=profile,
        bootstrap=bootstrap,
    )


@app.get("/api/m3/discovery/runs")
def api_m3_discovery_runs(limit: int = Query(10, ge=1, le=50)):
    from m3_discovery_service import list_recent_runs

    return list_recent_runs(limit=limit)


@app.get("/api/m3/coverage")
def api_m3_coverage():
    """Coverage dashboard: discovery + pipeline handoff + source recovery top 10."""
    from m3_discovery_service import discovery_status
    from source_recovery_priority import coverage_dashboard_payload

    st = discovery_status()
    per = None
    try:
        focus = st.get("last_successful_completion") or st.get("last_attempt") or {}
        per = focus.get("per_source_summary")
        if not per:
            metrics = focus.get("metrics") if isinstance(focus.get("metrics"), dict) else {}
            per = metrics.get("per_source")
    except Exception:
        per = None
    return coverage_dashboard_payload(per_source_metrics=per, discovery_status=st)


@app.get("/api/m3/source-recovery")
def api_m3_source_recovery(limit: int = Query(25, ge=1, le=100)):
    """TOP_SOURCE_RECOVERY_QUEUE — highest-return blocked source fixes."""
    from source_recovery_priority import build_source_recovery_queue

    return build_source_recovery_queue(limit=limit)


@app.get("/api/m3/handoff/status")
def api_m3_handoff_status():
    from m3_pipeline_handoff import load_handoff_checkpoint, pending_handoff_for_resume

    ckpt = load_handoff_checkpoint()
    return {
        "kind": "M3HandoffStatus",
        "checkpoint": {
            k: ckpt.get(k)
            for k in (
                "status",
                "run_id",
                "discovery_count",
                "transferred",
                "failed",
                "retries",
                "reconciliation",
                "updated_at",
                "completed_at",
            )
            if ckpt
        }
        if ckpt
        else None,
        "pending_resume": bool(pending_handoff_for_resume()),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/research/status")
def api_m3_research_status():
    from m3_research_service import research_status

    return research_status()


@app.post("/api/m3/research/run")
def api_m3_research_run():
    """Operator RUN NOW — drain RESEARCH_QUEUED via existing advance() engines."""
    from m3_research_service import TRIGGER_MANUAL, request_research_run

    return request_research_run(trigger_type=TRIGGER_MANUAL)


@app.get("/api/m3/evidence/status")
def api_m3_evidence_status():
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_evidence_acquisition import evidence_access_summary, classify_evidence_failure
    from m3_source_access import source_access_queue

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    rows = store.all()
    summary = evidence_access_summary(rows)
    reasons = {}
    for r in rows:
        fail = r.get("evidence_failure") or classify_evidence_failure(r)
        pr = fail.get("primary_reason") or "OTHER"
        reasons[pr] = reasons.get(pr, 0) + 1
    return {
        **summary,
        "primary_reason_counts": reasons,
        "source_access_queue": source_access_queue(rows),
        "openai_web_search_integration": True,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.post("/api/m3/evidence/acquire")
def api_m3_evidence_acquire(canonical_id: str | None = None, body: dict | None = None):
    """Run evidence ladder for one opportunity or all deferred (manual)."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_evidence_acquisition import acquire_evidence
    from m3_end_to_end import M3EndToEndOrchestrator
    from m3_lifecycle import derive_lifecycle, determine_next_action
    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    body = body or {}
    cid = canonical_id or body.get("canonical_id")
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    if cid:
        row = store.get(cid)
        if not row:
            raise HTTPException(status_code=404, detail="opportunity not found")
        acq = acquire_evidence(row, allow_paid=bool(body.get("allow_paid", True)))
        row = acq["row"]
        row["lifecycle"] = derive_lifecycle(row)
        row["pending_next_action"] = determine_next_action(row)
        store._rows[cid] = row
        store.save()
        # Re-advance if improved
        if acq["result"].get("improved") and not row.get("rejected"):
            orch = M3EndToEndOrchestrator(store=store)
            adv = orch.advance(cid)
            return {"acquisition": acq["result"], "failure": acq["failure"], "advance": adv}
        return {"acquisition": acq["result"], "failure": acq["failure"], "opportunity": row}
    # Kick research runner which now includes evidence ladder
    from m3_research_service import TRIGGER_MANUAL, request_research_run

    # Clear research fingerprints for deferred needing portal package resolver / incomplete ladder
    cleared = 0
    for row in store.all():
        lc = str(row.get("lifecycle") or "")
        if lc not in {"RESEARCH_QUEUED", "RESEARCH_IN_PROGRESS", "CHEAP_SCREENED", "PACKAGE_REQUIRED"}:
            continue
        ea = row.get("evidence_acquisition") or {}
        tiers = ea.get("tiers_attempted") or []
        needs_portal = "PORTAL_DOCUMENT_RESOLVER" not in tiers and not (
            row.get("line_items") or row.get("bom")
        )
        needs_ladder = "TIER_1_DIRECT" not in tiers
        if needs_portal or needs_ladder:
            row.pop("research_completed_fingerprint", None)
            row["research_queued"] = True
            store._rows[row["canonical_id"]] = row
            cleared += 1
    store.save()
    run = request_research_run(trigger_type=TRIGGER_MANUAL)
    return {"cleared_for_evidence_pass": cleared, "research_run": run}


@app.get("/api/m3/source-access/queue")
def api_m3_source_access_queue():
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_source_access import source_access_queue

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return source_access_queue(store.all())


@app.get("/api/m3/credentials")
def api_m3_credentials_list():
    from m3_source_credentials import list_credentials

    return list_credentials(include_secrets=False)


@app.post("/api/m3/credentials")
def api_m3_credentials_upsert(body: dict):
    from m3_source_credentials import upsert_credential

    portal = body.get("portal") or body.get("source_id")
    return upsert_credential(str(portal or ""), body)


@app.get("/api/m3/pipeline/status")
def api_m3_pipeline_status(canonical_id: str | None = None):
    from m3_pipeline_store import M3PipelineStore
    from m3_lifecycle import readiness_summary
    from m3_discovery_service import restore_pipeline_store_from_db

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    if canonical_id:
        row = store.get(canonical_id)
        if not row:
            raise HTTPException(status_code=404, detail="opportunity not found")
        return {
            "opportunity": row,
            "readiness": row.get("readiness_summary") or readiness_summary(row),
            "audit": store.audit_for(canonical_id)[-20:],
        }
    return {
        "count": len(store.all()),
        "opportunities": [
            {
                "canonical_id": r["canonical_id"],
                "title": r.get("title"),
                "lifecycle": r.get("lifecycle"),
                "next_action": r.get("pending_next_action"),
                "deadline": r.get("deadline"),
                "stop_reason": r.get("stop_reason"),
            }
            for r in store.all()[:100]
        ],
    }


@app.get("/api/m3/pipeline/next-action")
def api_m3_next_action(canonical_id: str):
    from m3_pipeline_store import M3PipelineStore
    from m3_lifecycle import determine_next_action

    store = M3PipelineStore()
    row = store.get(canonical_id)
    if not row:
        raise HTTPException(status_code=404, detail="opportunity not found")
    return determine_next_action(row)


@app.post("/api/m3/pipeline/advance")
def api_m3_pipeline_advance(canonical_id: str):
    from m3_end_to_end import M3EndToEndOrchestrator
    from m3_pipeline_store import M3PipelineStore
    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    orch = M3EndToEndOrchestrator(store=M3PipelineStore())
    return orch.advance(canonical_id)


@app.get("/api/m3/operator-queue")
def api_m3_operator_queue():
    from m3_pipeline_store import M3PipelineStore

    return {"queue": M3PipelineStore().operator_queue(), "DEVELOPMENT_NO_OUTREACH": True}


@app.get("/api/m3/readiness/{canonical_id}")
def api_m3_readiness(canonical_id: str):
    from m3_pipeline_store import M3PipelineStore
    from m3_lifecycle import readiness_summary

    row = M3PipelineStore().get(canonical_id)
    if not row:
        raise HTTPException(status_code=404, detail="opportunity not found")
    return readiness_summary(row)


@app.get("/api/m3/mobile/dashboard")
def api_m3_mobile_dashboard():
    from m3_mobile_read_model import mobile_dashboard_summary

    return mobile_dashboard_summary()


@app.get("/api/m3/mobile/opportunities")
def api_m3_mobile_opportunities():
    from m3_mobile_read_model import mobile_dashboard_summary

    dash = mobile_dashboard_summary()
    return {
        "opportunities": dash.get("active_opportunities") or [],
        "count": dash.get("active_count") or 0,
        "DEVELOPMENT_NO_OUTREACH": dash.get("DEVELOPMENT_NO_OUTREACH"),
    }


@app.get("/api/m3/mobile/actions")
def api_m3_mobile_actions():
    from m3_mobile_read_model import action_queue_mobile

    return action_queue_mobile()


@app.get("/api/m3/mobile/deal/{canonical_id}")
def api_m3_mobile_deal(canonical_id: str):
    from m3_pipeline_store import M3PipelineStore
    from m3_mobile_read_model import deal_room_summary
    from m3_discovery_service import restore_pipeline_store_from_db

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    row = store.get(canonical_id)
    if not row:
        raise HTTPException(status_code=404, detail="opportunity not found")
    return deal_room_summary(row)


@app.get("/api/m3/commercial/queue")
def api_m3_commercial_queue(limit: int = Query(25, ge=1, le=100)):
    """COMMERCIAL_RESEARCH_QUEUE — ranked by profit candidacy, not contract value alone."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_commercial_engine import build_commercial_research_queue

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return build_commercial_research_queue(store.all(), limit=limit)


@app.post("/api/m3/commercial/analyze")
def api_m3_commercial_analyze(body: dict | None = None):
    """Analyze top N research candidates — research only, no outreach."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_commercial_engine import analyze_top_commercial_opportunities

    payload = body or {}
    limit = int(payload.get("limit") or 25)
    limit = max(1, min(50, limit))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_top_commercial_opportunities(store, limit=limit)


@app.get("/api/m3/commercial/status")
def api_m3_commercial_status():
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_commercial_engine import build_commercial_research_queue

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    rows = store.all()
    scored = [r for r in rows if r.get("commercial_intelligence") or r.get("commercial_opportunity_score")]
    bands = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for r in scored:
        b = r.get("commercial_opportunity_score") or (r.get("commercial_intelligence") or {}).get(
            "COMMERCIAL_OPPORTUNITY_SCORE"
        )
        if b in bands:
            bands[b] += 1
    queue = build_commercial_research_queue(rows, limit=10)
    return {
        "kind": "M3CommercialStatus",
        "scored_opportunities": len(scored),
        "bands": bands,
        "TOP_10": queue.get("TOP_25", [])[:10],
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


@app.get("/api/m3/supplier/queue")
def api_m3_supplier_queue(limit: int = Query(10, ge=1, le=50)):
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_supplier_intelligence import build_supplier_research_queue

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return build_supplier_research_queue(store.all(), limit=limit)


@app.post("/api/m3/supplier/analyze")
def api_m3_supplier_analyze(body: dict | None = None):
    """TOP N supplier/acquisition research — Cost Governor gated, no outreach."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_supplier_intelligence import analyze_supplier_top_opportunities

    payload = body or {}
    limit = max(1, min(15, int(payload.get("limit") or 10)))
    allow_paid = payload.get("allow_paid_web", True)
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_supplier_top_opportunities(store, limit=limit, allow_paid_web=bool(allow_paid))


@app.get("/api/m3/supplier/status")
def api_m3_supplier_status():
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_supplier_intelligence import (
        PRICE_LEVEL_4_UNKNOWN,
        load_supplier_intel_index,
    )

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    idx = load_supplier_intel_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    if not by_id and isinstance(idx, dict):
        # tolerate flat map
        by_id = {k: v for k, v in idx.items() if isinstance(v, dict) and v.get("kind") == "M3SupplierIntelligence"}
    rows = []
    for cid, si in by_id.items():
        if isinstance(si, dict):
            rows.append({"canonical_id": cid, "supplier_intelligence": si})
    if not rows:
        rows = [r for r in store.all() if r.get("supplier_intelligence")]
    levels = {"LEVEL_1": 0, "LEVEL_2": 0, "LEVEL_3": 0, "LEVEL_4": 0}
    for r in rows:
        si = r.get("supplier_intelligence") or {}
        lvl = ((si.get("Pricing_evidence") or {}).get("primary_level") or PRICE_LEVEL_4_UNKNOWN)
        if "LEVEL_1" in str(lvl):
            levels["LEVEL_1"] += 1
        elif "LEVEL_2" in str(lvl):
            levels["LEVEL_2"] += 1
        elif "LEVEL_3" in str(lvl):
            levels["LEVEL_3"] += 1
        else:
            levels["LEVEL_4"] += 1
    return {
        "kind": "M3SupplierStatus",
        "researched": len(rows),
        "pricing_levels": levels,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.post("/api/m3/competitive/analyze")
def api_m3_competitive_analyze(body: dict | None = None):
    """TOP N new-entrant / competitive scoring — local only, no paid research."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_competitive_intelligence import analyze_competitive_top_opportunities

    payload = body or {}
    limit = max(1, min(50, int(payload.get("limit") or 25)))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_competitive_top_opportunities(store, limit=limit)


@app.get("/api/m3/competitive/status")
def api_m3_competitive_status():
    from m3_competitive_intelligence import load_competitive_index

    idx = load_competitive_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    buckets = {"A": 0, "B": 0, "C": 0, "D": 0}
    for si in by_id.values():
        if not isinstance(si, dict):
            continue
        b = str(si.get("BUCKET") or "")
        if "FIRST_DEAL" in b:
            buckets["A"] += 1
        elif "GROWTH" in b:
            buckets["B"] += 1
        elif "WATCH" in b:
            buckets["C"] += 1
        else:
            buckets["D"] += 1
    return {
        "kind": "M3CompetitiveStatus",
        "researched": len(by_id),
        "buckets": buckets,
        "bucket_a_first_deal": buckets["A"],
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/mobile/sources")
def api_m3_mobile_sources():
    from m3_mobile_read_model import mobile_sources_summary

    return mobile_sources_summary()


@app.get("/api/m3/controlled/mode")
def api_m3_controlled_mode():
    from operating_mode import mode_snapshot

    return mode_snapshot()


@app.post("/api/m3/controlled/enable")
def api_m3_controlled_enable(body: dict | None = None):
    from operating_mode import enable_controlled_real_world_verification

    body = body or {}
    return enable_controlled_real_world_verification(
        operator_id=str(body.get("operator_id") or ""),
        acknowledgment=bool(body.get("acknowledgment")),
        acknowledgment_text=body.get("acknowledgment_text"),
    )


@app.post("/api/m3/controlled/disable")
def api_m3_controlled_disable(body: dict | None = None):
    from operating_mode import disable_controlled_real_world_verification

    return disable_controlled_real_world_verification(
        operator_id=str((body or {}).get("operator_id") or "operator")
    )


@app.get("/api/m3/controlled/first-pursuits")
def api_m3_first_pursuits(limit: int = 10):
    from first_pursuit_selection import rank_first_pursuits
    from learning_feedback import apply_feedback_to_pursuit_score, compute_learning_feedback

    ranked = rank_first_pursuits(limit=max(1, min(limit, 25)))
    feedback = compute_learning_feedback()
    enriched = []
    for c in ranked.get("candidates") or []:
        adj = apply_feedback_to_pursuit_score(
            int(c.get("score") or 0),
            source_id=c.get("source_id"),
            category=c.get("category"),
            feedback=feedback,
        )
        enriched.append({**c, "learning": adj, "score_with_learning": adj["adjusted_score"]})
    enriched.sort(key=lambda x: (-int(x.get("score_with_learning") or 0), str(x.get("canonical_id"))))
    ranked["candidates"] = enriched
    ranked["learning_feedback_summary"] = {
        "records_analyzed": feedback.get("records_analyzed"),
        "adjustment_count": len(feedback.get("adjustments") or []),
    }
    return ranked


@app.get("/api/m3/controlled/review/{canonical_id}")
def api_m3_live_review(canonical_id: str):
    from live_review_package import live_opportunity_review_package

    pkg = live_opportunity_review_package(canonical_id=canonical_id)
    if pkg.get("error"):
        raise HTTPException(status_code=404, detail="opportunity not found")
    return pkg


@app.post("/api/m3/learning/start")
def api_m3_learning_start(body: dict | None = None):
    from transaction_learning import get_transaction_learning_store

    body = body or {}
    cid = str(body.get("canonical_id") or "")
    if not cid:
        raise HTTPException(status_code=400, detail="canonical_id required")
    return get_transaction_learning_store().start_pursuit(
        cid,
        source=body.get("source"),
        platform=body.get("platform"),
        buyer=body.get("buyer"),
        category=body.get("category"),
        why_discovered=body.get("why_discovered"),
        why_pursued=body.get("why_pursued"),
        estimated_acquisition_cost=body.get("estimated_acquisition_cost", "UNKNOWN"),
        estimated_freight=body.get("estimated_freight", "UNKNOWN"),
    )


@app.get("/api/m3/learning/records")
def api_m3_learning_records(canonical_id: str | None = None):
    from transaction_learning import get_transaction_learning_store

    store = get_transaction_learning_store()
    if canonical_id:
        return {"records": store.by_opportunity(canonical_id)}
    return {"records": store.all(), "count": len(store.all())}


@app.post("/api/m3/learning/supplier-verification")
def api_m3_learning_supplier(body: dict | None = None):
    from transaction_learning import get_transaction_learning_store

    body = body or {}
    rid = str(body.get("record_id") or "")
    if not rid:
        raise HTTPException(status_code=400, detail="record_id required")
    result = get_transaction_learning_store().record_supplier_verification(
        rid,
        supplier=str(body.get("supplier") or "UNKNOWN"),
        product=str(body.get("product") or "UNKNOWN"),
        quote_date=body.get("quote_date"),
        price=body.get("price", "UNKNOWN"),
        availability=str(body.get("availability") or "UNKNOWN"),
        lead_time=str(body.get("lead_time") or "UNKNOWN"),
        warranty=str(body.get("warranty") or "UNKNOWN"),
        notes=body.get("notes"),
        authorized_by=body.get("authorized_by"),
        freshness_days=int(body.get("freshness_days") or 14),
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/api/m3/learning/financing-verification")
def api_m3_learning_financing(body: dict | None = None):
    from transaction_learning import get_transaction_learning_store

    body = body or {}
    rid = str(body.get("record_id") or "")
    if not rid:
        raise HTTPException(status_code=400, detail="record_id required")
    result = get_transaction_learning_store().record_financing_verification(
        rid,
        financing_path=str(body.get("financing_path") or "UNKNOWN"),
        requirements=body.get("requirements"),
        result=str(body.get("result") or "UNKNOWN"),
        evidence=body.get("evidence"),
        transaction_structure=body.get("transaction_structure"),
        terms=body.get("terms"),
        timeline=body.get("timeline"),
        authorized_by=body.get("authorized_by"),
        state=body.get("state"),
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/api/m3/learning/outcome")
def api_m3_learning_outcome(body: dict | None = None):
    from transaction_learning import get_transaction_learning_store

    body = body or {}
    rid = str(body.get("record_id") or "")
    if not rid:
        raise HTTPException(status_code=400, detail="record_id required")
    result = get_transaction_learning_store().record_outcome(
        rid,
        status=str(body.get("status") or "IN_PROGRESS"),
        revenue=body.get("revenue", "UNKNOWN"),
        actual_profit=body.get("actual_profit", "UNKNOWN"),
        timeline=body.get("timeline"),
        problems=body.get("problems"),
        delays=body.get("delays"),
        missing_information=body.get("missing_information"),
        operator_id=body.get("operator_id"),
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.get("/api/m3/learning/feedback")
def api_m3_learning_feedback():
    from learning_feedback import compute_learning_feedback

    return compute_learning_feedback()


@app.get("/api/m3/learning/first-five")
def api_m3_learning_first_five():
    from learning_feedback import first_five_contract_learning_report

    return first_five_contract_learning_report()


@app.get("/api/m3/procurement-profile")
def api_m3_procurement_profile():
    from m3_procurement_profile import assert_m3_isolated_from_legacy, env_separation_notes, load_m3_procurement_profile

    profile = load_m3_procurement_profile()
    return {
        "profile": profile,
        "isolation": assert_m3_isolated_from_legacy(),
        "env_separation": env_separation_notes(),
    }


@app.get("/api/m3/configuration-separation")
def api_m3_configuration_separation():
    """Read-only audit snapshot for operators — legacy vs M3 config."""
    from m3_procurement_profile import (
        LEGACY_SERVICE_NAICS,
        assert_m3_isolated_from_legacy,
        env_separation_notes,
        legacy_naics_codes,
        load_m3_procurement_profile,
    )

    return {
        "kind": "M3ConfigurationSeparation",
        "m3_profile": load_m3_procurement_profile(),
        "legacy_service_naics": sorted(LEGACY_SERVICE_NAICS),
        "legacy_naics_from_catalog": legacy_naics_codes(),
        "isolation": assert_m3_isolated_from_legacy(),
        "env_separation": env_separation_notes(),
        "note": "Legacy NAICS_CODES / gt_app_settings.naics_codes remain for Northern RE facilities sync; M3 does not read them",
    }


@app.get("/api/cost-governor/dashboard")
def api_cost_governor_dashboard():
    from cost_governor import get_cost_governor
    from budget_catchup import market_coverage_state
    from procurement_source_registry import ProcurementSourceRegistry

    gov = get_cost_governor()
    dash = gov.dashboard_payload()
    reg = ProcurementSourceRegistry()
    coverage = market_coverage_state(
        sources=reg.all_sources(),
        budget_paused_ids=set(dash.get("sources_paused_by_budget") or []),
    )
    dash["market_coverage"] = coverage
    dash["backlog_note"] = "Research backlog may remain while market coverage is CURRENT"
    return dash


@app.get("/api/cost-governor/config")
def api_cost_governor_config():
    from cost_governor import get_cost_governor

    gov = get_cost_governor()
    return {"config": gov.config_store.get(), "audit": gov.config_store.audit()[-50:]}


@app.post("/api/cost-governor/config")
def api_cost_governor_update_config(body: dict):
    """Operator-only budget updates — autonomous callers rejected."""
    from cost_governor import get_cost_governor

    gov = get_cost_governor()
    operator_id = str(body.get("operator_id") or "operator")
    updates = body.get("updates") or {}
    reason = body.get("reason")
    try:
        cfg = gov.config_store.update_limits(updates, operator_id=operator_id, reason=reason)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    restored = gov.on_budget_restored() if any(
        k.endswith("_CAP") or k == "ABSOLUTE_AUTONOMOUS_SPEND_CAP" for k in updates
    ) else None
    return {"config": cfg, "restoration": restored}


@app.get("/api/cost-governor/deferred")
def api_cost_governor_deferred():
    from cost_governor import get_cost_governor

    gov = get_cost_governor()
    return {"deferred": gov.deferred_work(), "paused_sources": gov._paused_sources}


# --- Bid requirements + compliance intelligence ---
_BID_COMPLIANCE_CACHE: dict[str, dict] = {}


def _bid_compliance_for(opportunity_id: str, body: dict | None = None) -> dict:
    from bid_compliance_engine import acknowledge_material_change, analyze_bid_compliance

    body = body or {}
    if opportunity_id in _BID_COMPLIANCE_CACHE and not body.get("force"):
        cached = _BID_COMPLIANCE_CACHE[opportunity_id]
        if body.get("acknowledge_change_id"):
            cached = acknowledge_material_change(
                analysis=cached,
                change_id=str(body["acknowledge_change_id"]),
                operator_id=str(body.get("operator_id") or "operator"),
            )
            _BID_COMPLIANCE_CACHE[opportunity_id] = cached
        return cached
    result = analyze_bid_compliance(
        solicitation_id=opportunity_id,
        documents=body.get("documents"),
        document_texts=body.get("document_texts"),
        company_profile=body.get("company_profile"),
        company_facts=body.get("company_facts"),
        product=body.get("product"),
        deadline_viability=body.get("deadline_viability"),
        pursuit_worthy=bool(body.get("pursuit_worthy")),
        economics_potentially_viable=bool(body.get("economics_potentially_viable")),
        assumed_lead_time_days=body.get("assumed_lead_time_days"),
        known_registrations=body.get("known_registrations"),
        unreviewed_material_changes=body.get("unreviewed_material_changes"),
        last_authoritative_check_at=body.get("last_authoritative_check_at"),
        paid_research_questions=body.get("paid_research_questions"),
    )
    _BID_COMPLIANCE_CACHE[opportunity_id] = result
    return result


@app.get("/api/opportunities/{opportunity_id}/bid-requirements")
def api_bid_requirements(opportunity_id: str):
    analysis = _bid_compliance_for(opportunity_id)
    return {
        "solicitation_id": opportunity_id,
        "requirements": analysis.get("requirements"),
        "mandatory_vs_informational": analysis.get("mandatory_vs_informational"),
    }


@app.get("/api/opportunities/{opportunity_id}/compliance-matrix")
def api_compliance_matrix(opportunity_id: str):
    analysis = _bid_compliance_for(opportunity_id)
    return analysis.get("compliance_matrix") or {}


@app.get("/api/opportunities/{opportunity_id}/bid-readiness")
def api_bid_readiness(opportunity_id: str):
    analysis = _bid_compliance_for(opportunity_id)
    return {
        "bid_readiness": analysis.get("bid_readiness"),
        "package_completeness": analysis.get("package_completeness"),
        "commercial_verification": analysis.get("commercial_verification"),
    }


@app.get("/api/opportunities/{opportunity_id}/package-map")
def api_package_map(opportunity_id: str):
    analysis = _bid_compliance_for(opportunity_id)
    return analysis.get("package_map") or {}


@app.get("/api/opportunities/{opportunity_id}/bid-checklist")
def api_bid_checklist(opportunity_id: str):
    analysis = _bid_compliance_for(opportunity_id)
    return analysis.get("checklist") or {}


@app.post("/api/opportunities/{opportunity_id}/analyze-compliance")
def api_analyze_compliance(opportunity_id: str, body: dict | None = None):
    payload = dict(body or {})
    payload["force"] = True
    return _bid_compliance_for(opportunity_id, payload)


@app.post("/api/opportunities/{opportunity_id}/acknowledge-material-change")
def api_acknowledge_material_change(opportunity_id: str, body: dict | None = None):
    payload = dict(body or {})
    if not payload.get("acknowledge_change_id") and payload.get("change_id"):
        payload["acknowledge_change_id"] = payload["change_id"]
    analysis = _bid_compliance_for(opportunity_id, payload)
    return {"ok": True, "acknowledged_changes": analysis.get("acknowledged_changes"), "bid_readiness": analysis.get("bid_readiness")}


# --- Bid assembly + pricing intelligence ---
_BID_PRICING_CACHE: dict[str, dict] = {}


def _bid_pricing_for(opportunity_id: str, body: dict | None = None) -> dict:
    from bid_pricing_engine import analyze_bid_pricing

    body = body or {}
    if opportunity_id in _BID_PRICING_CACHE and not body.get("force") and not body.get("line_items"):
        return _BID_PRICING_CACHE[opportunity_id]
    result = analyze_bid_pricing(
        solicitation_id=opportunity_id,
        line_items=body.get("line_items"),
        historical_observations=body.get("historical_observations"),
        freight_cost=body.get("freight_cost"),
        freight_confidence=body.get("freight_confidence") or "UNKNOWN",
        financing_cost=body.get("financing_cost"),
        financing_confidence=body.get("financing_confidence") or "UNKNOWN",
        transaction_expense=body.get("transaction_expense"),
        risk_allowance=body.get("risk_allowance"),
        risk_protects_against=body.get("risk_protects_against"),
        evaluation_basis=body.get("evaluation_basis"),
        award_mode=body.get("award_mode") or "ALL_OR_NONE",
        government_estimate=body.get("government_estimate"),
        company_facts=body.get("company_facts"),
        required_forms=body.get("required_forms"),
        submission=body.get("submission"),
        delivery=body.get("delivery"),
        compliance_matrix=body.get("compliance_matrix"),
        delivery_by=body.get("delivery_by"),
        authorization_document=body.get("authorization_document"),
        priced_at=body.get("priced_at"),
        profit_config=body.get("profit_config"),
        amendments_accounted=body.get("amendments_accounted"),
        freshness_ok=body.get("freshness_ok", True),
        hard_blocker=bool(body.get("hard_blocker")),
        deadline_actionable=body.get("deadline_actionable", True),
        product_compliance_ok=body.get("product_compliance_ok", True),
        eligibility_ok=body.get("eligibility_ok", True),
        paid_research_questions=body.get("paid_research_questions"),
        prior_audit=(_BID_PRICING_CACHE.get(opportunity_id) or {}).get("pricing_audit"),
    )
    _BID_PRICING_CACHE[opportunity_id] = result
    return result


@app.get("/api/opportunities/{opportunity_id}/pricing")
def api_opp_pricing(opportunity_id: str):
    a = _bid_pricing_for(opportunity_id)
    return {
        "operator_summary": a.get("operator_summary"),
        "finalization_state": a.get("finalization_state"),
        "recommendation": (a.get("pricing_scenarios") or {}).get("recommended"),
        "transaction_economics": a.get("transaction_economics"),
    }


@app.get("/api/opportunities/{opportunity_id}/pricing-scenarios")
def api_opp_pricing_scenarios(opportunity_id: str):
    return _bid_pricing_for(opportunity_id).get("pricing_scenarios") or {}


@app.get("/api/opportunities/{opportunity_id}/transaction-economics")
def api_opp_transaction_economics(opportunity_id: str):
    return _bid_pricing_for(opportunity_id).get("transaction_economics") or {}


@app.get("/api/opportunities/{opportunity_id}/draft-bid")
def api_opp_draft_bid(opportunity_id: str):
    return _bid_pricing_for(opportunity_id).get("draft_bid_package") or {}


@app.get("/api/opportunities/{opportunity_id}/bid-package-manifest")
def api_opp_bid_package_manifest(opportunity_id: str):
    draft = _bid_pricing_for(opportunity_id).get("draft_bid_package") or {}
    return draft.get("package_manifest") or {}


@app.get("/api/opportunities/{opportunity_id}/commercial-verification-targets")
def api_opp_commercial_targets(opportunity_id: str):
    return _bid_pricing_for(opportunity_id).get("commercial_verification_targets") or {}


@app.get("/api/opportunities/{opportunity_id}/pricing-audit")
def api_opp_pricing_audit(opportunity_id: str):
    return {"audit": _bid_pricing_for(opportunity_id).get("pricing_audit") or []}


@app.post("/api/opportunities/{opportunity_id}/analyze-pricing")
def api_analyze_pricing(opportunity_id: str, body: dict | None = None):
    payload = dict(body or {})
    payload["force"] = True
    return _bid_pricing_for(opportunity_id, payload)


@app.post("/api/opportunities/{opportunity_id}/assemble-draft-bid")
def api_assemble_draft_bid(opportunity_id: str, body: dict | None = None):
    payload = dict(body or {})
    payload["force"] = True
    analysis = _bid_pricing_for(opportunity_id, payload)
    return {
        "draft_bid_package": analysis.get("draft_bid_package"),
        "draft_validation": analysis.get("draft_validation"),
        "finalization_state": analysis.get("finalization_state"),
    }


# --- Commercial verification + execution control ---
_COMMERCIAL_VERIFICATION_CACHE: dict[str, dict] = {}


def _commercial_for(opportunity_id: str, body: dict | None = None) -> dict:
    from commercial_verification_engine import build_commercial_verification_bundle

    body = body or {}
    if opportunity_id in _COMMERCIAL_VERIFICATION_CACHE and not body.get("force"):
        return _COMMERCIAL_VERIFICATION_CACHE[opportunity_id]
    result = build_commercial_verification_bundle(
        opportunity_id=opportunity_id,
        product_description=body.get("product_description"),
        quantity=body.get("quantity"),
        acquisition_estimate=body.get("acquisition_estimate"),
        acquisition_confidence=body.get("acquisition_confidence") or "UNKNOWN",
        freight_estimate=body.get("freight_estimate"),
        freight_confidence=body.get("freight_confidence") or "UNKNOWN",
        financing_estimate=body.get("financing_estimate"),
        bid_revenue=body.get("bid_revenue"),
        delivery_by=body.get("delivery_by"),
        quote_valid_through=body.get("quote_valid_through"),
        destination=body.get("destination"),
        authorization_required=bool(body.get("authorization_required")),
        origin_required=bool(body.get("origin_required")),
        government_payment_terms=body.get("government_payment_terms"),
        supplier_payment_timing=body.get("supplier_payment_timing"),
        agency=body.get("agency"),
        supplier=body.get("supplier"),
        operator_fico=body.get("operator_fico"),
        operator_cash_available=body.get("operator_cash_available"),
        pg_acceptable=body.get("pg_acceptable", True),
        economically_attractive=bool(body.get("economically_attractive")),
        stock_verified=bool(body.get("stock_verified")),
        lead_time_verified=bool(body.get("lead_time_verified")),
        unit_target=body.get("unit_target"),
        unit_ceiling=body.get("unit_ceiling"),
        solicitation_open=body.get("solicitation_open", True),
        deadline_viable=body.get("deadline_viable", True),
        package_fresh=body.get("package_fresh", True),
        amendments_acknowledged=bool(body.get("amendments_acknowledged")),
        compliance_passes=bool(body.get("compliance_passes")),
        product_compliant=bool(body.get("product_compliant")),
        submission_method=body.get("submission_method"),
    )
    _COMMERCIAL_VERIFICATION_CACHE[opportunity_id] = result
    return result


@app.get("/api/opportunities/{opportunity_id}/commercial-verification")
def api_commercial_verification(opportunity_id: str):
    b = _commercial_for(opportunity_id)
    return {"dashboard": b.get("dashboard"), "readiness": b.get("commercial_readiness"), "funding_gate": b.get("funding_gate")}


@app.get("/api/opportunities/{opportunity_id}/verification-items")
def api_verification_items(opportunity_id: str):
    return {"items": (_commercial_for(opportunity_id).get("verification_plan") or {}).get("items") or []}


@app.get("/api/opportunities/{opportunity_id}/funding-requirement")
def api_funding_requirement(opportunity_id: str):
    return _commercial_for(opportunity_id).get("funding_requirement") or {}


@app.get("/api/opportunities/{opportunity_id}/financing-compatibility")
def api_financing_compatibility(opportunity_id: str):
    return {"compatibility": _commercial_for(opportunity_id).get("financing_compatibility") or []}


@app.get("/api/opportunities/{opportunity_id}/external-actions")
def api_external_actions(opportunity_id: str):
    return {"actions": _commercial_for(opportunity_id).get("external_actions") or []}


@app.get("/api/opportunities/{opportunity_id}/commercial-readiness")
def api_commercial_readiness(opportunity_id: str):
    return _commercial_for(opportunity_id).get("commercial_readiness") or {}


@app.get("/api/opportunities/{opportunity_id}/execution-gate")
def api_execution_gate(opportunity_id: str):
    return _commercial_for(opportunity_id).get("execution_gate") or {}


@app.post("/api/opportunities/{opportunity_id}/build-verification-plan")
def api_build_verification_plan(opportunity_id: str, body: dict | None = None):
    payload = dict(body or {})
    payload["force"] = True
    return _commercial_for(opportunity_id, payload)


@app.post("/api/opportunities/{opportunity_id}/verification-result")
def api_verification_result(opportunity_id: str, body: dict | None = None):
    from commercial_result_pipeline import ingest_verification_result
    from commercial_verification_engine import process_verification_result_and_recalc

    body = body or {}
    bundle = _commercial_for(opportunity_id)
    result = ingest_verification_result(
        opportunity_id=opportunity_id,
        result_type=str(body.get("result_type") or "supplier_quote"),
        payload=body.get("payload") or {},
        authority=str(body.get("authority") or "UNKNOWN"),
        source=body.get("source"),
        expires_at=body.get("expires_at"),
    )
    updated = process_verification_result_and_recalc(
        bundle,
        result=result,
        bid_revenue=body.get("bid_revenue"),
        freight=body.get("freight"),
        financing=body.get("financing"),
    )
    _COMMERCIAL_VERIFICATION_CACHE[opportunity_id] = updated
    return updated


@app.post("/api/external-actions/{action_id}/authorize")
def api_authorize_external_action(action_id: str, body: dict | None = None):
    from external_action_control import get_external_action_store

    return get_external_action_store().authorize(action_id, operator_id=str((body or {}).get("operator_id") or "operator"))


@app.post("/api/external-actions/{action_id}/reject")
def api_reject_external_action(action_id: str, body: dict | None = None):
    from external_action_control import get_external_action_store

    return get_external_action_store().reject(
        action_id,
        operator_id=str((body or {}).get("operator_id") or "operator"),
        reason=(body or {}).get("reason"),
    )


@app.post("/api/external-actions/{action_id}/revoke")
def api_revoke_external_action(action_id: str, body: dict | None = None):
    from external_action_control import get_external_action_store

    return get_external_action_store().revoke(action_id, operator_id=str((body or {}).get("operator_id") or "operator"))


@app.post("/api/external-actions/{action_id}/dry-run")
def api_dry_run_external_action(action_id: str):
    from external_action_control import execute_external_action

    return execute_external_action(action_id)


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
        "autopilot": _autopilot_summary(),
    }


@app.post("/api/csv-attachment-queue/process")
def process_csv_attachment_queue_endpoint():
    """Process queued CSV attachment downloads using remaining daily SAM budget."""
    from csv_attachment_queue_service import get_attachment_queue_dashboard_stats, process_attachment_queue

    session = SessionLocal()
    try:
        before = get_attachment_queue_dashboard_stats(session)
        result = process_attachment_queue(session, use_reserved_budget=True)
        session.commit()
        after = get_attachment_queue_dashboard_stats(session)
        return {
            "ok": True,
            "before": before,
            "result": result,
            "after": after,
        }
    finally:
        session.close()


@app.get("/api/csv-attachment-queue")
def csv_attachment_queue_status():
    """CSV import attachment queue counts for dashboard."""
    from csv_attachment_queue_service import get_attachment_queue_dashboard_stats

    session = SessionLocal()
    try:
        return get_attachment_queue_dashboard_stats(session)
    finally:
        session.close()


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
        from api_budget import get_usage_snapshot
        from attachment_storage import contract_ids_with_stored_pdfs
        from expired_purge_service import purge_expired_unpursued
        from screening_pipeline import is_dashboard_ready_fast, is_visible_on_dashboard
        from sam_client import min_days_from_env
        from sync import contract_to_card_dict, list_contracts

        purge = purge_expired_unpursued(session)
        if purge.get("contracts_removed") or purge.get("csv_removed"):
            session.commit()

        effective_min_days = min_days if min_days is not None else min_days_from_env()
        stored_pdf_ids = set(contract_ids_with_stored_pdfs(session))

        all_rows = list_contracts(
            session,
            naics_codes=naics_codes,
            min_days_until_due=effective_min_days,
            min_score=min_score,
            agency=agency,
            pursue_only=pursue_only,
            tier=tier,
            status_filter=status,
            set_aside_filter=set_aside,
            require_dashboard_ready=False,
            require_scrape_complete=False,
        )

        from workflow_status import repair_open_opportunity_status

        status_dirty = False
        for row in all_rows:
            if repair_open_opportunity_status(row):
                status_dirty = True
        if status_dirty:
            session.commit()

        visible_rows: list = []
        for row in all_rows:
            if row.id in stored_pdf_ids:
                if getattr(row, "subcontracting_limitation_check", None) != "FOUND":
                    visible_rows.append(row)
                continue
            if is_visible_on_dashboard(row, session, stored_pdf_ids=stored_pdf_ids):
                visible_rows.append(row)

        row_flags: list[tuple] = []
        for row in visible_rows:
            ready = is_dashboard_ready_fast(row, session, stored_pdf_ids=stored_pdf_ids)
            row_flags.append((row, ready))

        processing_count = sum(1 for _, ready in row_flags if not ready)

        from gs_watchlist_service import (
            govspend_watchlist_meta,
            is_govspend_watchlist_hit,
            is_possible_watchlist_match,
            load_watching_targets,
            match_contract_to_targets,
        )

        watchlist_targets = load_watching_targets()

        def _watchlist_sort_key(item: tuple) -> tuple:
            from datetime import date

            row, ready = item
            meta = govspend_watchlist_meta(row)
            if meta or is_govspend_watchlist_hit(row):
                priority = (meta or {}).get("priority") or ""
                rank = 0 if str(priority).lower() == "high" else 1
                conf_rank = 0 if (meta or {}).get("match_confidence") == "High" else 1
                return (
                    0,
                    conf_rank,
                    rank,
                    0 if ready else 1,
                    row.due_date is None,
                    (row.due_date - today_local()).days if row.due_date else 9999,
                )
            if is_possible_watchlist_match(row):
                return (1, 0, 0 if ready else 1, row.due_date is None)
            matched, priority, _ = match_contract_to_targets(row, watchlist_targets)
            if matched:
                rank = 0 if str(priority or "").lower() == "high" else 1
                return (1, rank, 0 if ready else 1, row.due_date is None)
            return (
                2,
                0 if ready else 1,
                -int(
                    (row.analysis or {}).get("score")
                    or (row.analysis or {}).get("text_score")
                    or 0
                ),
                row.due_date is None,
            )

        rows = sorted(row_flags, key=_watchlist_sort_key)

        hidden_by_min_days = 0
        ready_eligible = len(visible_rows)
        if effective_min_days > 0:
            at_zero = list_contracts(
                session,
                naics_codes=naics_codes,
                min_days_until_due=0,
                min_score=min_score,
                agency=agency,
                pursue_only=pursue_only,
                tier=tier,
                status_filter=status,
                set_aside_filter=set_aside,
                require_dashboard_ready=False,
                require_scrape_complete=False,
            )
            visible_at_zero = 0
            for row in at_zero:
                if row.id in stored_pdf_ids:
                    if getattr(row, "subcontracting_limitation_check", None) != "FOUND":
                        visible_at_zero += 1
                    continue
                if is_visible_on_dashboard(row, session, stored_pdf_ids=stored_pdf_ids):
                    visible_at_zero += 1
            ready_eligible = visible_at_zero
            hidden_by_min_days = max(0, visible_at_zero - len(visible_rows))

        from document_intel import attachment_fetch_alert_for_card, piee_intel_for_card

        piee_action_count = sum(
            1
            for row, _ in rows
            if piee_intel_for_card(row, session).get("action_required")
        )
        manual_fetch_count = sum(
            1
            for row, _ in rows
            if attachment_fetch_alert_for_card(row, session).get("blocked")
        )
        watchlist_match_count = sum(
            1
            for row, _ in rows
            if is_govspend_watchlist_hit(row)
            or match_contract_to_targets(row, watchlist_targets)[0]
        )
        watchlist_hit_count = sum(1 for row, _ in rows if is_govspend_watchlist_hit(row))
        possible_match_count = sum(1 for row, _ in rows if is_possible_watchlist_match(row))

        return {
            "count": len(rows),
            "processing_count": processing_count,
            "contracts": [
                contract_to_card_dict(
                    row,
                    session,
                    stored_pdf_ids=stored_pdf_ids,
                    watchlist_targets=watchlist_targets,
                )
                for row, _ in rows
            ],
            "api_budget": get_usage_snapshot(),
            "filter_stats": {
                "ready_eligible": ready_eligible,
                "hidden_by_min_days": hidden_by_min_days,
                "total_matching_naics": len(all_rows),
                "min_days_applied": effective_min_days,
                "piee_action_count": piee_action_count,
                "manual_fetch_count": manual_fetch_count,
                "watchlist_match_count": watchlist_match_count,
                "watchlist_hit_count": watchlist_hit_count,
                "possible_match_count": possible_match_count,
                "watchlist_target_count": len(watchlist_targets),
                "attachment_queue": _attachment_queue_stats(session),
            },
            "attachment_queue": _attachment_queue_stats(session),
            "autopilot": _autopilot_summary(),
        }
    finally:
        session.close()


def _attachment_queue_stats(session) -> dict[str, Any]:
    try:
        from csv_attachment_queue_service import get_attachment_queue_dashboard_stats

        return get_attachment_queue_dashboard_stats(session)
    except Exception:
        return {
            "queued": 0,
            "downloading": 0,
            "complete": 0,
            "failed": 0,
            "waiting_for_budget": 0,
        }


@app.get("/api/contracts/watchlist-hits")
def get_watchlist_hits():
    """Contracts matched from GovSpend gs_watchlist — sorted by bid deadline."""
    from attachment_storage import contract_ids_with_stored_pdfs
    from sync import contract_to_card_dict
    from watchlist_sync import list_watchlist_hit_contracts

    session = SessionLocal()
    try:
        from gs_watchlist_service import load_watching_targets

        stored_pdf_ids = set(contract_ids_with_stored_pdfs(session))
        targets = load_watching_targets()
        hits = list_watchlist_hit_contracts(session)
        return {
            "count": len(hits),
            "contracts": [
                contract_to_card_dict(
                    row,
                    session,
                    stored_pdf_ids=stored_pdf_ids,
                    watchlist_targets=targets,
                )
                for row in hits
            ],
        }
    finally:
        session.close()


@app.get("/api/contracts/watchlist-possible")
def get_watchlist_possible_matches():
    """Possible fingerprint matches awaiting manual review."""
    from attachment_storage import contract_ids_with_stored_pdfs
    from sync import contract_to_card_dict
    from watchlist_sync import list_possible_watchlist_matches

    session = SessionLocal()
    try:
        from gs_watchlist_service import load_watching_targets

        stored_pdf_ids = set(contract_ids_with_stored_pdfs(session))
        targets = load_watching_targets()
        matches = list_possible_watchlist_matches(session)
        return {
            "count": len(matches),
            "contracts": [
                contract_to_card_dict(
                    row,
                    session,
                    stored_pdf_ids=stored_pdf_ids,
                    watchlist_targets=targets,
                )
                for row in matches
            ],
        }
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/watchlist-match/confirm")
def confirm_watchlist_match_endpoint(notice_id: str):
    from watchlist_sync import confirm_watchlist_match

    session = SessionLocal()
    try:
        result = confirm_watchlist_match(session, notice_id)
        if not result.get("ok"):
            code = 404 if result.get("error") == "not_found" else 400
            raise HTTPException(status_code=code, detail=result.get("error"))
        return result
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/watchlist-match/reject")
def reject_watchlist_match_endpoint(notice_id: str):
    from watchlist_sync import reject_watchlist_match

    session = SessionLocal()
    try:
        result = reject_watchlist_match(session, notice_id)
        if not result.get("ok"):
            code = 404 if result.get("error") == "not_found" else 400
            raise HTTPException(status_code=code, detail=result.get("error"))
        return result
    finally:
        session.close()


@app.get("/api/contracts/{notice_id}")
def get_contract(notice_id: str):
    from sqlalchemy import func
    from sqlalchemy.orm import defer

    session = SessionLocal()
    try:
        from models import Contract
        from workflow_status import repair_open_opportunity_status

        row = (
            session.query(Contract)
            .options(defer(Contract.attachment_text))
            .filter_by(notice_id=notice_id)
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        if repair_open_opportunity_status(row):
            session.commit()
        from usaspending_client import backfill_predecessor_award_details

        intel = row.pricing_intel if isinstance(row.pricing_intel, dict) else None
        if intel and isinstance(intel.get("predecessor_award"), dict):
            pred = backfill_predecessor_award_details(intel["predecessor_award"])
            if pred and (
                pred.get("number_of_offers_received") != intel["predecessor_award"].get("number_of_offers_received")
                or pred.get("modification_history") != intel["predecessor_award"].get("modification_history")
            ):
                intel = dict(intel)
                intel["predecessor_award"] = pred
                if pred.get("number_of_offers_received") is not None:
                    intel["number_of_offers_received"] = pred["number_of_offers_received"]
                row.pricing_intel = intel
                session.commit()
        text_chars = (
            session.query(func.coalesce(func.length(Contract.attachment_text), 0))
            .filter(Contract.id == row.id)
            .scalar()
        )
        data = contract_to_dict(row, session, attachment_text_chars=int(text_chars or 0))
        data["sam_raw"] = row.sam_raw if isinstance(row.sam_raw, dict) else {}
        return data
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/extract-solicitation")
def extract_contract_solicitation(notice_id: str, force: bool = Query(False)):
    """Pull CO, dates, and PWS scope from bid PDFs via AI."""
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
        data = contract_to_dict(row, session)
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


@app.post("/api/contracts/{notice_id}/lookup-prior-contract")
def lookup_prior_contract(notice_id: str, body: PriorContractLookup):
    """Look up prior contract pricing on USAspending by PIID — no SAM.gov API calls."""
    session = SessionLocal()
    try:
        from models import Contract

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        intel = lookup_prior_contract_by_number(row, body.contract_number)
        session.commit()
        payload = get_full_pricing_intel(row, session, force_refresh=False)
        payload["pricing_intel"] = intel
        payload["contract"] = contract_to_dict(row, session)
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=502, detail=f"Prior contract lookup failed: {exc}") from exc
    finally:
        session.close()


@app.post("/api/contracts/{notice_id}/contract-advice")
def generate_contract_advice_endpoint(notice_id: str, refresh: bool = Query(False)):
    """Generate pursue/avoid coaching from stored summary + pricing — no SAM.gov calls."""
    session = SessionLocal()
    try:
        from api_budget import ScreenBudgetExceeded
        from contract_advice import ensure_contract_advice, get_contract_advice
        from models import Contract

        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")

        existing = get_contract_advice(row)
        if existing and not refresh:
            return {"contract_advice": existing, "contract": contract_to_dict(row, session)}

        advice = ensure_contract_advice(session, row, force=refresh)
        return {"contract_advice": advice, "contract": contract_to_dict(row, session)}
    except ScreenBudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=502, detail=f"Contract advice failed: {exc}") from exc
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
        return contract_to_dict(row, session)
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


@app.get("/api/export/ai")
@app.get("/api/export/claude")
def export_ai_portfolio():
    """Download a complete JSON snapshot of GovTracker for offline AI / archive use."""
    session = SessionLocal()
    try:
        from ai_export import export_ai_json_bytes

        data, filename = export_ai_json_bytes(session, include_attachment_text=True)
        return Response(
            content=data,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}") from exc
    finally:
        session.close()


@app.post("/api/pricing/refresh")
def run_pricing_refresh():
    """USAspending prior-contract lookup for cards missing dollar amounts (no SAM.gov)."""
    try:
        from pricing_backfill_service import start_background_pricing_backfill

        return start_background_pricing_backfill()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Pricing refresh failed: {exc}") from exc


@app.get("/api/pricing/refresh/status")
def pricing_refresh_status():
    from pricing_backfill_service import get_pricing_backfill_status

    return get_pricing_backfill_status()


@app.post("/api/repair/stored")
def run_stored_pdf_repair():
    """Repair only contracts that already have PDF bytes in PostgreSQL (no SAM.gov)."""
    try:
        from workflow_backfill_service import start_stored_pdf_repair_background

        return start_stored_pdf_repair_background()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Stored-PDF repair failed: {exc}") from exc


@app.get("/api/repair/status")
def stored_pdf_repair_status():
    from workflow_backfill_service import get_repair_status

    return get_repair_status()


@app.get("/api/sync/attachments/status")
def attachment_sync_status_endpoint():
    from sync import attachment_sync_status

    return attachment_sync_status()


@app.post("/api/sync/attachments")
def run_attachment_sync():
    try:
        from sync import sync_attachments_only

        return sync_attachments_only()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Attachment sync failed: {exc}") from exc


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
def run_screen_one(
    notice_id: str,
    force: bool = Query(False),
    auto: bool = Query(False, description="True when triggered by opening contract detail"),
):
    try:
        from api_budget import ScreenBudgetExceeded, auto_screen_on_contract_detail, can_screen

        if auto and not force and not auto_screen_on_contract_detail():
            return {
                "notice_id": notice_id,
                "skipped": True,
                "reason": "auto_screen_disabled",
                "message": "Automatic screening on contract detail is off. Use Sync/Screen or set AUTO_SCREEN_ON_CONTRACT_DETAIL=true.",
            }
        if not can_screen() and not force:
            raise ScreenBudgetExceeded()
        return screen_one(notice_id, force=force)
    except ScreenBudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Screening failed: {exc}") from exc


@app.post("/api/contracts/{notice_id}/full-analysis")
def run_force_full_analysis(notice_id: str):
    """Manual override — PIEE/PDF download + full AI analysis regardless of text score."""
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
        from models import Contract
        from sub_finder import list_contract_subs, maybe_start_background_sub_search

        contract = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not contract:
            raise ValueError("Contract not found")
        maybe_start_background_sub_search(session, contract)
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


@app.post("/api/contracts/{notice_id}/attachments")
async def upload_contract_attachments(
    notice_id: str,
    files: list[UploadFile] = File(...),
):
    """Upload solicitation PDFs for a contract — stored in DB, no SAM.gov API calls."""
    from attachment_storage import MANUAL_ATTACHMENT_MAX_BYTES, upload_manual_contract_attachments
    from models import Contract

    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one PDF")

    session = SessionLocal()
    try:
        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")

        batch: list[tuple[str, bytes]] = []
        for upload in files:
            filename = (upload.filename or "document.pdf").strip()
            if not filename.lower().endswith(".pdf"):
                raise HTTPException(status_code=400, detail=f"PDF only: {filename}")
            content = await upload.read()
            if not content:
                raise HTTPException(status_code=400, detail=f"Empty file: {filename}")
            if len(content) > MANUAL_ATTACHMENT_MAX_BYTES:
                max_mb = MANUAL_ATTACHMENT_MAX_BYTES // (1024 * 1024)
                raise HTTPException(status_code=400, detail=f"{filename} too large (max {max_mb}MB)")
            if not content.startswith(b"%PDF"):
                raise HTTPException(status_code=400, detail=f"Not a valid PDF: {filename}")
            batch.append((filename, content))

        result = upload_manual_contract_attachments(session, row, batch)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "Upload failed"))
        session.commit()
        return {
            **result,
            "notice_id": notice_id,
            "attachment_files": contract_to_dict(row, session).get("attachment_files"),
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        logging.getLogger("govtracker.attachments").exception("Manual attachment upload failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
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
        return contract_to_dict(row, session) if row else {"notice_id": notice_id, "amendment_alert_active": False}
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


@app.get("/api/csv-opportunities")
def list_csv_opportunities(
    state: str | None = Query(None, description="Filter by place-of-performance state"),
    days: str | None = Query(
        None,
        description="Days until due bucket: under_7, 7_14, 14_30, 30_plus, or all",
    ),
    naics: str | None = Query(None, description="Filter by NAICS code"),
    q: str | None = Query(None, description="Keyword search in title"),
):
    """All filtered CSV import rows for the dashboard CSV Opportunities section."""
    session = SessionLocal()
    try:
        from csv_opportunity_service import list_csv_opportunity_cards

        result = list_csv_opportunity_cards(
            session,
            state=state,
            days_bucket=days,
            naics_code=naics,
            keyword=q,
        )
        if (
            result.get("records_removed_expired")
            or result.get("records_removed_duplicate")
            or result.get("records_removed_contracts")
        ):
            session.commit()
        return result
    finally:
        session.close()


@app.post("/api/csv-opportunities/refresh-pricing")
def refresh_csv_opportunities_pricing(
    state: str | None = Query(None),
    days: str | None = Query(None),
    naics: str | None = Query(None),
    q: str | None = Query(None),
    force: bool = Query(False, description="Re-price rows that already have cached pricing"),
):
    """Background USAspending pricing for filtered CSV opportunities."""
    from csv_pricing_job import start_csv_pricing_job

    started = start_csv_pricing_job(
        state=state,
        days_bucket=days,
        naics_code=naics,
        keyword=q,
        force=force,
    )
    if not started.get("ok"):
        raise HTTPException(status_code=409, detail=started.get("error", "Pricing refresh failed"))
    return JSONResponse(status_code=202, content=started)


@app.get("/api/csv-opportunities/refresh-pricing/status")
def csv_pricing_refresh_status():
    from csv_pricing_job import csv_pricing_job_status

    return csv_pricing_job_status()


@app.post("/api/csv-opportunities/{notice_id}/pursue")
def pursue_csv_opportunity_endpoint(notice_id: str):
    """Mark a CSV opportunity as Pursuing and start the full pipeline."""
    session = SessionLocal()
    try:
        from csv_opportunity_service import pursue_csv_opportunity

        result = pursue_csv_opportunity(session, notice_id)
        if not result.get("ok"):
            code = 404 if result.get("error") == "not_found" else 400
            raise HTTPException(status_code=code, detail=result.get("error"))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/upload/sam-csv/status")
def csv_upload_status():
    """Poll background CSV import progress."""
    from csv_upload_job import csv_upload_status as job_status

    return job_status()


@app.post("/api/upload/sam-csv/reset")
def reset_sam_csv_upload(request: Request):
    """Clear a stuck or failed background CSV import."""
    from csv_upload_job import reset_csv_upload_job

    token = request.cookies.get(COOKIE_NAME)
    if not verify_auth_token(token):
        raise HTTPException(status_code=403, detail="Login required")
    return reset_csv_upload_job()


@app.post("/api/upload/sam-csv")
async def upload_sam_csv(
    request: Request,
    file: UploadFile = File(...),
    password: str = Form(""),
):
    """Import SAM full CSV into gt_csv_opportunities (runs in background for large files)."""
    from csv_import_service import csv_upload_max_bytes, verify_csv_upload_password
    from csv_upload_job import start_csv_upload_job

    token = request.cookies.get(COOKIE_NAME)
    if not verify_auth_token(token) and not verify_csv_upload_password(password):
        raise HTTPException(status_code=403, detail="Invalid upload password")

    if not file.filename or not str(file.filename).lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload a .csv file")

    content = await file.read()
    if not content.strip():
        raise HTTPException(status_code=400, detail="CSV file is empty")
    max_bytes = csv_upload_max_bytes()
    if len(content) > max_bytes:
        max_mb = max_bytes // (1024 * 1024)
        raise HTTPException(status_code=400, detail=f"CSV file too large (max {max_mb}MB)")

    started = start_csv_upload_job(content, process_attachments=False)
    if not started.get("ok"):
        raise HTTPException(status_code=409, detail=started.get("error", "Import already running"))
    return JSONResponse(status_code=202, content=started)


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

"""GovTracker web API and dashboard."""

from __future__ import annotations

# Cap BLAS/OpenMP threads before any numpy/scipy-backed import chain.
import bidnet_engine.thread_limits  # noqa: F401

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
APP_BUILD_VERSION = "20261007-m3-live-channel-fit-canary-v1"

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
        # Baseline auto-resume disabled — money-path recovery supersedes stalled full baseline.
        try:
            from bidnet_auth import startup_health_report

            startup_health_report()
        except Exception:
            log.exception("BidNet auth startup health report failed (non-fatal)")
        try:
            from opengov_auth import startup_health_report as opengov_startup_health_report

            opengov_startup_health_report()
        except Exception:
            log.exception("OpenGov auth startup health report failed (non-fatal)")
        try:
            from euna_auth import startup_health_report as euna_startup_health_report

            euna_startup_health_report()
        except Exception:
            log.exception("Euna auth startup health report failed (non-fatal)")
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
    try:
        from m3_data_root import log_data_root_startup

        log_data_root_startup()
    except Exception as exc:
        print(f"govtracker: data root startup log failed: {exc}", flush=True)
    threading.Thread(target=_run_background_startup, name="govtracker-startup", daemon=True).start()
    yield
    stop_scheduler()


app = FastAPI(title="GovTracker", version="0.1.0", lifespan=lifespan)

from micro_purchase_lab_routes import register_micro_purchase_lab_routes

register_micro_purchase_lab_routes(app)

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


@app.get("/api/m3/phase-l/latest")
def api_m3_phase_l_latest():
    """Phase L hunt summary + owner chips for top accessible / profit-queue opportunities."""
    from pathlib import Path

    from phase_l.owner_view import build_owner_view

    root = Path(__file__).resolve().parent / "artifacts" / "phase_l"
    hunt_path = root / "hunt_latest.json"
    enrich_path = root / "enrichment_latest.json"
    queue_path = root / "owner_profit_queue.json"
    if not hunt_path.exists() and not enrich_path.exists():
        return {"kind": "PhaseLLatest", "ok": False, "error": "no_hunt_artifact"}
    import json

    data = json.loads(hunt_path.read_text(encoding="utf-8")) if hunt_path.exists() else {}
    enrich = json.loads(enrich_path.read_text(encoding="utf-8")) if enrich_path.exists() else {}
    queue = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else {}

    # Prefer ≥$10K owner profit queue when L.2 enrichment produced any
    source_rows = queue.get("rows") or enrich.get("top20") or data.get("top20") or []
    top = []
    for row in source_rows[:20]:
        top.append(
            {
                **build_owner_view(row),
                "title": row.get("title"),
                "solicitation_id": row.get("solicitation_id"),
                "expected_net_profit": row.get("expected_net_profit"),
                "profit_tier": row.get("profit_tier"),
            }
        )
    return {
        "kind": "PhaseLLatest",
        "ok": True,
        "generated_at": enrich.get("generated_at") or data.get("generated_at"),
        "phase": enrich.get("phase") or data.get("phase") or "L",
        "source_mix": data.get("source_mix"),
        "counts": enrich.get("counts") or data.get("counts"),
        "funnel_total": (enrich.get("funnel") or data.get("funnel") or {}).get("TOTAL"),
        "enrichment_funnel": (enrich.get("funnel") or {}).get("TOTAL"),
        "primary_blockers": data.get("primary_blockers"),
        "failure_reasons": enrich.get("failure_reasons"),
        "owner_queue_count": (queue.get("count") if queue else None)
        or (enrich.get("counts") or {}).get("owner_queue"),
        "top20_owner_views": top,
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


@app.get("/api/m3/discovery/coverage")
def api_m3_discovery_coverage():
    """Discovery Coverage dashboard — RAW LIVE vs PRODUCT CANDIDATES (no profit filter)."""
    from discovery_expansion import discovery_coverage_dashboard

    return discovery_coverage_dashboard()


@app.post("/api/m3/discovery/expansion-harvest")
def api_m3_discovery_expansion_harvest(body: dict | None = None):
    """Free national expansion harvest — BidNet + platform families → L23 merge. Does not burn SAM."""
    from discovery_expansion import run_expansion_harvest

    payload = body or {}
    return run_expansion_harvest(
        include_bidnet=bool(payload.get("include_bidnet", True)),
        include_structured=bool(payload.get("include_structured", True)),
        include_platform_catalog=bool(payload.get("include_platform_catalog", True)),
        max_pages=int(payload.get("max_pages") or 80),
        max_catalog_entities_per_family=int(payload.get("max_catalog_entities_per_family") or 40),
        persist=True,
    )


@app.get("/api/m3/universe-pass/funnel")
def api_m3_universe_pass_funnel():
    """Classification → freshness → profit-evidence funnel for the live universe."""
    from universe_pass import universe_funnel_dashboard

    return universe_funnel_dashboard()


@app.post("/api/m3/universe-pass/run")
def api_m3_universe_pass_run(body: dict | None = None):
    """Classify full live universe, BidNet freshness cleanup, profit-first product routing."""
    from universe_pass import run_universe_pass

    payload = body or {}
    return run_universe_pass(
        classify=bool(payload.get("classify", True)),
        freshness=bool(payload.get("freshness", True)),
        profit_route=bool(payload.get("profit_route", True)),
        limit=payload.get("limit"),
        profit_limit=payload.get("profit_limit"),
        resume=bool(payload.get("resume", True)),
        persist=bool(payload.get("persist", True)),
        force_reclassify=bool(payload.get("force_reclassify", False)),
    )


@app.post("/api/m3/full-production-e2e/run")
def api_m3_full_production_e2e_run(body: dict | None = None):
    """Async full free-source discovery → universe → recovery → profit report."""
    from m3_auth_jobs import start_full_production_e2e_job

    payload = body or {}
    pl = payload.get("profit_limit")
    return start_full_production_e2e_job(
        skip_bidnet=bool(payload.get("skip_bidnet", False)),
        skip_opengov=bool(payload.get("skip_opengov", False)),
        skip_expansion=bool(payload.get("skip_expansion", False)),
        bidnet_max_results=int(payload.get("bidnet_max_results") or 25000),
        opengov_max_pages=int(payload.get("opengov_max_pages") or 40),
        recovery_bidnet_limit=int(payload.get("recovery_bidnet_limit") or 400),
        recovery_opengov_limit=int(payload.get("recovery_opengov_limit") or 200),
        profit_limit=None if pl in (None, "", "all") else int(pl),
    )


@app.post("/api/m3/full-funnel-sweep/run")
def api_m3_full_funnel_sweep_run(body: dict | None = None):
    """Async full-universe sweep through END_OF_FUNNEL_READY."""
    from m3_auth_jobs import start_full_funnel_sweep_job

    payload = body or {}
    og_lim = payload.get("opengov_recovery_limit")
    return start_full_funnel_sweep_job(
        skip_bidnet=bool(payload.get("skip_bidnet", False)),
        skip_opengov=bool(payload.get("skip_opengov", False)),
        skip_expansion=bool(payload.get("skip_expansion", False)),
        skip_discovery=bool(payload.get("skip_discovery", False)),
        bidnet_max_results=int(payload.get("bidnet_max_results") or 25000),
        opengov_max_pages=int(payload.get("opengov_max_pages") or 40),
                    free_package_batch_size=int(payload.get("free_package_batch_size") or 5000),
                    free_package_max_batches=int(payload.get("free_package_max_batches") or 40),
        opengov_recovery_limit=None if og_lim in (None, "", "all") else int(og_lim),
        resume=bool(payload.get("resume", True)),
    )


@app.get("/api/m3/full-funnel-sweep/last-report")
def api_m3_full_funnel_sweep_last_report():
    """Last persisted full-funnel sweep completion report."""
    import json

    from m3_data_root import data_path

    path = data_path("m3_full_funnel_sweep_last_report.json")
    if not path.exists():
        return {"kind": None, "status": "NO_REPORT"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"kind": None, "status": "READ_ERROR", "error": type(exc).__name__}


@app.get("/api/m3/basket-full-funnel/last-report")
def api_m3_basket_full_funnel_last_report():
    """Last basket completion + canonical funnel reconciliation report."""
    import json

    from m3_data_root import data_path

    # Prefer strict-economics build report when present
    strict = data_path("m3_line_basket_completion_strict_economics_v1_last_report.json")
    path = strict if strict.exists() else data_path("m3_basket_full_funnel_reconcile_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "READ_ERROR", "error": type(exc).__name__}


@app.get("/api/m3/line-basket-strict/last-report")
def api_m3_line_basket_strict_last_report():
    """Line-level basket completion + strict economics gate report."""
    import json

    from m3_data_root import data_path

    # Prefer material-line recovery report when present
    recovery = data_path("m3_material_line_identity_price_recovery_v1_last_report.json")
    path = recovery if recovery.exists() else data_path("m3_line_basket_completion_strict_economics_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "READ_ERROR", "error": type(exc).__name__}


@app.get("/api/m3/material-line-recovery/last-report")
def api_m3_material_line_recovery_last_report():
    """Material line identity + production price recovery report."""
    import json

    from m3_data_root import data_path

    deep = data_path("m3_deep_completion_3to5_v1_last_report.json")
    path = deep if deep.exists() else data_path("m3_material_line_identity_price_recovery_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "READ_ERROR", "error": type(exc).__name__}


@app.get("/api/m3/deep-completion/last-report")
def api_m3_deep_completion_last_report():
    """Deep completion 3–5 quote conversion report."""
    import json

    from m3_data_root import data_path

    path = data_path("m3_deep_completion_3to5_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "READ_ERROR", "error": type(exc).__name__}


@app.get("/api/m3/deep-completion/opportunity/{opportunity_id}")
def api_m3_deep_completion_opportunity(opportunity_id: str):
    """Owner UI: deep completion candidate — no profit until ECONOMICS_READY."""
    import json

    from m3_data_root import data_path

    path = data_path("m3_deep_completion_3to5_v1_checkpoint.json")
    if not path.exists():
        return {"status": "NO_CHECKPOINT", "opportunity_id": opportunity_id}
    ck = json.loads(path.read_text(encoding="utf-8"))
    row = (ck.get("opportunities") or {}).get(opportunity_id)
    if not row:
        return {"status": "NOT_IN_CORPUS", "opportunity_id": opportunity_id}
    owner = row.get("owner_view") or {}
    return {
        "opportunity_id": opportunity_id,
        "deep_completion": True,
        "owner_view": {
            "material_lines": owner.get("material_lines"),
            "public_priced": owner.get("public_priced"),
            "quote_required": owner.get("quote_required"),
            "quote_packets": owner.get("quote_packets"),
            "suppliers": owner.get("suppliers"),
            "revenue_evidence": owner.get("revenue_evidence"),
            "target_acquisition_ceiling": owner.get("target_acquisition_ceiling"),
            "financing_state": owner.get("financing_state"),
            "execution_risk": owner.get("execution_risk"),
            "deadline": owner.get("deadline"),
            "next_action": owner.get("next_action"),
            "expected_profit": None,
            "headline": "PROFIT NOT YET PROVEN",
        },
        "revenue": row.get("revenue"),
        "execution": row.get("execution"),
        "quote_packets": {
            k: (row.get("quote_packets") or {}).get(k)
            for k in ("packet_count", "coverage", "suppliers", "quote_ready_lines", "consolidation_ratio")
        },
        "financing": row.get("financing"),
        "target_economics": row.get("target_economics"),
        "coverage": row.get("coverage"),
    }


@app.get("/api/m3/deep-completion/outreach-queue")
def api_m3_deep_completion_outreach_queue():
    """Owner quote outreach queue — do not auto-send."""
    import json

    from m3_data_root import data_path

    path = data_path("m3_final_pre_scale_outreach_queue_v1.json")
    if not path.exists():
        path = data_path("m3_deep_completion_outreach_queue_v1.json")
    if not path.exists():
        return {"status": "NO_QUEUE", "build_version": APP_BUILD_VERSION, "queue": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data["build_version"] = APP_BUILD_VERSION
            data["do_not_send_automatically"] = True
            return data
        return {"queue": data, "build_version": APP_BUILD_VERSION, "do_not_send_automatically": True}
    except Exception as exc:
        return {"status": "READ_ERROR", "error": type(exc).__name__}


@app.get("/api/m3/final-pre-scale/last-report")
def api_m3_final_pre_scale_last_report():
    """Final pre-scale proof — revenue hardening + owner-ready quote gate."""
    import json

    from m3_data_root import data_path

    path = data_path("m3_final_pre_scale_proof_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["build_version"] = APP_BUILD_VERSION
        return data
    except Exception as exc:
        return {"status": "READ_ERROR", "error": type(exc).__name__}


@app.get("/api/m3/final-pre-scale/gap-register")
def api_m3_final_pre_scale_gap_register():
    """PRE_SCALE_GAP_REGISTER_V1."""
    import json

    from m3_data_root import data_path

    path = data_path("PRE_SCALE_GAP_REGISTER_V1.json")
    if not path.exists():
        return {"status": "NO_REGISTER", "build_version": APP_BUILD_VERSION}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["build_version"] = APP_BUILD_VERSION
        return data
    except Exception as exc:
        return {"status": "READ_ERROR", "error": type(exc).__name__}


@app.get("/api/m3/p0-hardening/last-report")
def api_m3_p0_hardening_last_report():
    import json

    from m3_data_root import data_path

    path = data_path("m3_p0_prescale_hardening_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["build_version"] = APP_BUILD_VERSION
        return data
    except Exception as exc:
        return {"status": "READ_ERROR", "error": type(exc).__name__}


@app.get("/api/m3/owner-channel/packets")
def api_m3_owner_channel_packets():
    """Owner channel test packets — do not auto-send."""
    import json

    from m3_data_root import data_path

    # Prefer frozen corpus
    path = data_path("OWNER_CHANNEL_TEST_CORPUS_V1.json")
    if not path.exists():
        path = data_path("m3_owner_channel_test_packets_v1.json")
    if not path.exists():
        return {"status": "NO_PACKETS", "build_version": APP_BUILD_VERSION, "packets": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["build_version"] = APP_BUILD_VERSION
        data["do_not_send_automatically"] = True
        return data
    except Exception as exc:
        return {"status": "READ_ERROR", "error": type(exc).__name__}


@app.get("/api/m3/owner-channel/last-report")
def api_m3_owner_channel_last_report():
    import json

    from m3_data_root import data_path

    path = data_path("m3_owner_channel_tests_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["build_version"] = APP_BUILD_VERSION
        return data
    except Exception as exc:
        return {"status": "READ_ERROR", "error": type(exc).__name__}


@app.get("/api/m3/owner-channel/cards")
def api_m3_owner_channel_cards():
    """Owner Channel Tests action cards — four suppliers."""
    import json

    from m3_data_root import data_path

    report = data_path("m3_owner_channel_tests_v1_last_report.json")
    outreach = data_path("m3_owner_channel_outreach_texts_v1.json")
    sent = data_path("m3_owner_channel_sent_state_v1.json")
    resp = data_path("m3_owner_channel_responses_v1.json")
    obs_ui = data_path("m3_quote_observability_ui_v1.json")
    if not report.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION, "cards": []}
    r = json.loads(report.read_text(encoding="utf-8"))
    o = json.loads(outreach.read_text(encoding="utf-8")) if outreach.exists() else {"requests": []}
    s = json.loads(sent.read_text(encoding="utf-8")) if sent.exists() else {"by_packet": {}}
    responses = json.loads(resp.read_text(encoding="utf-8")) if resp.exists() else {"by_packet": {}}
    obs_by = (
        json.loads(obs_ui.read_text(encoding="utf-8")).get("by_packet") or {}
        if obs_ui.exists()
        else {}
    )
    by_supplier = {(x.get("Supplier") or ""): x for x in o.get("requests") or []}
    cards = []
    for p in (r.get("OWNER_CHANNEL_PACKETS") or {}).get("packets") or []:
        name = p.get("Supplier") or ""
        req = by_supplier.get(name) or {}
        pid = p.get("packet_id")
        sent_row = (s.get("by_packet") or {}).get(pid)
        resp_row = (responses.get("by_packet") or {}).get(pid)
        obs = obs_by.get(pid) or {}
        cards.append(
            {
                **p,
                "subject": req.get("Subject"),
                "request_body": req.get("Request_body") or req.get("Request_body_preview"),
                "exports": req.get("Packet_attachment_export") or req.get("exports"),
                "sent_state": sent_row,
                "response_state": resp_row,
                "observability": {
                    "packet_status": obs.get("packet_status")
                    or (sent_row or {}).get("channel_test_state")
                    or p.get("Status"),
                    "sent_date": obs.get("sent_date") or (sent_row or {}).get("sent_timestamp"),
                    "supplier_response": obs.get("supplier_response")
                    or (resp_row or {}).get("outcome"),
                    "quote_status": obs.get("quote_status"),
                    "line_match_count": obs.get("line_match_count"),
                    "rejected_line_count": obs.get("rejected_line_count"),
                    "basket_before": obs.get("basket_before"),
                    "basket_after": obs.get("basket_after"),
                    "economics_before": obs.get("economics_before"),
                    "economics_after": obs.get("economics_after"),
                    "current_next_action": obs.get("current_next_action")
                    or "Copy request → send externally → Mark Sent",
                    "plain_english": obs.get("plain_english"),
                    "can_revert": obs.get("can_revert"),
                    "active_quote_id": obs.get("active_quote_id"),
                    "event_count": obs.get("event_count") or 0,
                },
                "do_not_send_automatically": True,
            }
        )
    return {"build_version": APP_BUILD_VERSION, "cards": cards, "do_not_send_automatically": True}


@app.get("/api/m3/quote-observability/{packet_id}")
def api_m3_quote_observability(packet_id: str):
    """Owner audit trail for a channel packet quote loop."""
    import json

    from m3_data_root import data_path

    ledger = data_path("QUOTE_EVENT_LEDGER.json")
    snaps = data_path("m3_quote_before_after_snapshots_v1.json")
    ui = data_path("m3_quote_observability_ui_v1.json")
    events = []
    snapshots = []
    card = {}
    if ledger.exists():
        events = [
            e
            for e in (json.loads(ledger.read_text(encoding="utf-8")).get("events") or [])
            if e.get("packet") == packet_id
        ]
    if snaps.exists():
        snapshots = [
            s
            for s in (json.loads(snaps.read_text(encoding="utf-8")).get("snapshots") or [])
            if s.get("packet") == packet_id
        ]
    if ui.exists():
        card = (json.loads(ui.read_text(encoding="utf-8")).get("by_packet") or {}).get(packet_id) or {}
    return {
        "build_version": APP_BUILD_VERSION,
        "packet_id": packet_id,
        "card": card,
        "events": events,
        "snapshots": snapshots,
    }


@app.post("/api/m3/quote-observability/deactivate")
def api_m3_quote_deactivate(body: dict | None = None):
    from p1_prescale_hardening.quote_observability import deactivate_quote

    payload = body or {}
    return {
        "build_version": APP_BUILD_VERSION,
        **deactivate_quote(str(payload.get("quote_id") or ""), reason=str(payload.get("reason") or "owner_revert")),
    }


@app.get("/api/m3/p1-prescale/report")
def api_m3_p1_prescale_report():
    import json

    from m3_data_root import data_path

    path = data_path("m3_p1_prescale_hardening_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/large-production-test/report")
def api_m3_large_production_test_report():
    import json

    from m3_data_root import data_path

    path = data_path("m3_large_production_test_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/large-production-test/progress")
def api_m3_large_production_test_progress():
    import json

    from m3_data_root import data_path

    path = data_path("m3_large_production_test_v1_progress.json")
    if not path.exists():
        return {"status": "NO_PROGRESS", "build_version": APP_BUILD_VERSION, "progress_pct": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/large-production-test/ui")
def api_m3_large_production_test_ui():
    """Canonical /ops sync for large-test top deals, quote reserve, blocked."""
    import json

    from m3_data_root import data_path

    path = data_path("m3_large_production_test_v1_ui.json")
    if not path.exists():
        return {"status": "NO_UI", "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    data["REAL_SUPPLIER_LOOP_PROVEN"] = "NO"
    return data


@app.get("/api/m3/package-recovery/report")
def api_m3_package_recovery_report():
    import json

    from m3_data_root import data_path

    path = data_path("m3_package_recovery_sam_budget_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/package-recovery/progress")
def api_m3_package_recovery_progress():
    import json

    from m3_data_root import data_path

    path = data_path("m3_package_recovery_sam_budget_v1_progress.json")
    if not path.exists():
        return {"status": "NO_PROGRESS", "build_version": APP_BUILD_VERSION, "progress_pct": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/package-recovery/ui")
def api_m3_package_recovery_ui():
    """Explicit package states + SAM credit meter for /ops."""
    import json

    from m3_data_root import data_path

    path = data_path("m3_package_recovery_sam_budget_v1_ui.json")
    if not path.exists():
        return {"status": "NO_UI", "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    # Live SAM meter overlay
    try:
        from package_recovery_sam_budget.sam_manager import SamDailyCreditManager

        data["sam_meter"] = SamDailyCreditManager().snapshot()
    except Exception as exc:
        data["sam_meter_error"] = type(exc).__name__
    return data


@app.post("/api/m3/package-recovery/promote-sam")
def api_m3_package_recovery_promote_sam(body: dict | None = None):
    """Owner promotes one SAM opportunity — still respects hard 10/day unless override=true."""
    import json

    from m3_data_root import data_path
    from package_recovery_sam_budget.models import BUILD, SAM_QUEUE
    from package_recovery_sam_budget.sam_manager import SamDailyCreditManager

    payload = body or {}
    oid = str(payload.get("opportunity_id") or "").strip()
    override = bool(payload.get("owner_override"))
    if not oid:
        return {"ok": False, "error": "opportunity_id_required", "build_version": APP_BUILD_VERSION}
    manager = SamDailyCreditManager()
    snap = manager.snapshot()
    if not override and not manager.can_spend_automated(1):
        return {
            "ok": False,
            "error": "SAM_BUDGET_EXHAUSTED",
            "sam_meter": snap,
            "hint": "Set owner_override=true to spend reserved credit",
            "build_version": APP_BUILD_VERSION,
        }
    qpath = data_path(SAM_QUEUE)
    queue = {}
    if qpath.exists():
        queue = json.loads(qpath.read_text(encoding="utf-8"))
    promoted = list(queue.get("owner_promoted") or [])
    if oid not in promoted:
        promoted.append(oid)
    queue["owner_promoted"] = promoted
    queue["updated_at"] = __import__("application_clock").now_utc().isoformat()
    queue["build"] = BUILD
    qpath.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "promoted": oid,
        "use_reserve": override,
        "sam_meter": manager.snapshot(),
        "build_version": APP_BUILD_VERSION,
        "auto_spent": False,
    }


@app.get("/api/m3/bidnet-full-production/report")
def api_m3_bidnet_full_production_report():
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_full_production_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/bidnet-full-production/progress")
def api_m3_bidnet_full_production_progress():
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_full_production_v1_progress.json")
    if not path.exists():
        return {"status": "NO_PROGRESS", "build_version": APP_BUILD_VERSION, "progress_pct": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/bidnet-full-production/ui")
def api_m3_bidnet_full_production_ui():
    """BidNet source health + 192 acceptance for /ops."""
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_full_production_v1_ui.json")
    if not path.exists():
        return {"status": "NO_UI", "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    try:
        from bidnet_auth import owner_connection_status

        data["bidnet_auth"] = owner_connection_status()
    except Exception as exc:
        data["bidnet_auth_error"] = type(exc).__name__
    return data


@app.get("/api/m3/bidnet-full-production/architecture")
def api_m3_bidnet_full_production_architecture():
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_full_production_architecture_audit.json")
    if not path.exists():
        from bidnet_full_production.architecture_audit import build_architecture_audit

        return build_architecture_audit()
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/m3/bidnet-full-production/run")
def api_m3_bidnet_full_production_run(body: dict | None = None):
    """Kick authenticated BidNet full-production acceptance (background job)."""
    from m3_auth_jobs import start_bidnet_full_production_job

    payload = body or {}
    return start_bidnet_full_production_job(
        fresh=bool(payload.get("fresh", True)),
        full_discovery=bool(payload.get("full_discovery", False)),
    )


@app.post("/api/m3/bidnet-full-production/seed-corpus")
def api_m3_bidnet_full_production_seed_corpus(body: dict | None = None):
    """Seed LARGE_TEST_CORPUS_V1 onto M3_DATA_ROOT (immutable frozen 500)."""
    import json

    from m3_data_root import data_path

    payload = body or {}
    corpus = payload.get("corpus") if isinstance(payload.get("corpus"), dict) else payload
    ids = corpus.get("opportunity_ids") or []
    items = corpus.get("items") or []
    if not ids or not items or len(ids) != len(items):
        return {"ok": False, "error": "invalid_corpus_shape", "build_version": APP_BUILD_VERSION}
    bidnet = sum(1 for i in items if i.get("source_bucket") == "BidNet")
    if bidnet != 192:
        return {
            "ok": False,
            "error": "bidnet_count_must_be_192",
            "bidnet": bidnet,
            "build_version": APP_BUILD_VERSION,
        }
    path = data_path("LARGE_TEST_CORPUS_V1.json")
    path.write_text(json.dumps(corpus, indent=2, default=str), encoding="utf-8")
    return {
        "ok": True,
        "path": str(path),
        "count": len(ids),
        "bidnet": bidnet,
        "build_version": APP_BUILD_VERSION,
    }


@app.get("/api/m3/bidnet-full-production/env-check")
def api_m3_bidnet_full_production_env_check():
    """Confirm BidNet auth env + storage state + corpus (no secrets)."""
    from bidnet_auth.config import load_bidnet_auth_config
    from bidnet_auth.session_store import storage_state_exists, storage_state_path
    from m3_data_root import data_path, get_data_root
    import json

    cfg = load_bidnet_auth_config()
    corpus_path = data_path("LARGE_TEST_CORPUS_V1.json")
    bidnet = 0
    count = 0
    if corpus_path.exists():
        try:
            c = json.loads(corpus_path.read_text(encoding="utf-8"))
            count = len(c.get("opportunity_ids") or [])
            bidnet = sum(1 for i in (c.get("items") or []) if i.get("source_bucket") == "BidNet")
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "build_version": APP_BUILD_VERSION}
    from bidnet_engine.thread_limits import verify_thread_limits

    threads = verify_thread_limits()
    return {
        "ok": True,
        "build_version": APP_BUILD_VERSION,
        "gap_walker": 3,
        "downstream_walker": 4,
        "engine_walker": 2,
        "recovery_walker": 1,
        "production_walker": 2,
        "money_path_walker": 1,
        "channel_fit_canary_walker": 1,
        "data_root": str(get_data_root()),
        "auth_enabled": cfg.auth_enabled,
        "credentials_configured": cfg.credentials_present,
        "storage_state_present": storage_state_exists(),
        "storage_state_path": str(storage_state_path()),
        "corpus_present": corpus_path.exists(),
        "corpus_count": count,
        "corpus_bidnet": bidnet,
        "thread_limits": threads,
        "bidnet_logical_workers": __import__("os").environ.get("BIDNET_LOGICAL_WORKERS"),
        "bidnet_browser_workers": __import__("os").environ.get("BIDNET_BROWSER_WORKERS"),
    }


@app.get("/api/m3/bidnet-full-production/job/{job_id}")
def api_m3_bidnet_full_production_job(job_id: str):
    from m3_auth_jobs import get_job

    job = get_job(job_id)
    if not job:
        return {"status": "NO_JOB", "job_id": job_id, "build_version": APP_BUILD_VERSION}
    job = dict(job)
    job["build_version"] = APP_BUILD_VERSION
    return job


@app.post("/api/m3/owner-channel/mark-sent")
def api_m3_owner_channel_mark_sent(body: dict | None = None):
    """Owner manually marks quote request sent externally — never auto-sends."""
    from owner_channel_tests.lifecycle import mark_quote_request_sent

    payload = body or {}
    row = mark_quote_request_sent(
        packet_id=str(payload.get("packet_id") or ""),
        supplier=str(payload.get("supplier") or ""),
        method=str(payload.get("method") or "MANUAL_EXTERNAL"),
        contact_used=payload.get("contact_used"),
        requested_response_date=payload.get("requested_response_date"),
        owner_notes=payload.get("owner_notes"),
    )
    return {"ok": True, "build_version": APP_BUILD_VERSION, "sent": row, "auto_sent": False}


@app.post("/api/m3/owner-channel/record-response")
def api_m3_owner_channel_record_response(body: dict | None = None):
    from owner_channel_tests.lifecycle import record_response

    payload = body or {}
    row = record_response(
        packet_id=str(payload.get("packet_id") or ""),
        outcome=str(payload.get("outcome") or "OTHER"),
        owner_notes=payload.get("owner_notes"),
    )
    return {"ok": True, "build_version": APP_BUILD_VERSION, "response": row}


@app.post("/api/m3/owner-channel/ingest-quote")
def api_m3_owner_channel_ingest_quote(body: dict | None = None):
    """Ingest REAL_SUPPLIER_QUOTE only — fixtures blocked."""
    import json

    from m3_data_root import data_path
    from owner_channel_tests.lifecycle import ingest_real_quote

    payload = body or {}
    quote = payload.get("quote") or payload
    packet_id = payload.get("packet_id") or quote.get("packet_id")
    corpus = data_path("OWNER_CHANNEL_TEST_CORPUS_V1.json")
    packet = None
    if corpus.exists():
        packets = json.loads(corpus.read_text(encoding="utf-8")).get("packets") or []
        packet = next((p for p in packets if p.get("packet_id") == packet_id), None)
    if not packet:
        return {"ok": False, "error": "PACKET_NOT_FOUND", "build_version": APP_BUILD_VERSION}
    result = ingest_real_quote(packet=packet, quote=quote)
    return {"ok": bool(result.get("accepted")), "build_version": APP_BUILD_VERSION, "result": result}


@app.post("/api/m3/quotes/process-real")
def api_m3_process_real_supplier_quote(body: dict | None = None):
    """PROCESS_REAL_SUPPLIER_QUOTE — fixtures blocked; no auto outreach."""
    from copy import deepcopy

    from p0_prescale_hardening.quote_pipeline import init_basket_from_packet, process_real_supplier_quote
    from m3_data_root import data_path
    import json

    payload = body or {}
    quote = payload.get("quote") or payload
    packet = payload.get("packet")
    if not packet:
        ch = data_path("m3_owner_channel_test_packets_v1.json")
        if ch.exists():
            packets = (json.loads(ch.read_text(encoding="utf-8")).get("channel_packets") or [])
            pid = payload.get("packet_id")
            packet = next((p for p in packets if p.get("packet_id") == pid), packets[0] if packets else None)
    if not packet:
        return {"ok": False, "error": "NO_PACKET", "build_version": APP_BUILD_VERSION}
    basket = init_basket_from_packet(packet)
    audit = process_real_supplier_quote(quote, basket_state=deepcopy(basket), opportunity_state={})
    return {"ok": True, "build_version": APP_BUILD_VERSION, "audit": audit, "do_not_send_automatically": True}


@app.get("/api/m3/material-line-recovery/opportunity/{opportunity_id}")
def api_m3_material_line_recovery_opportunity(opportunity_id: str):
    """Owner UI: material identity/price recovery — no profit until ECONOMICS_READY."""
    import json

    from m3_data_root import data_path

    path = data_path("m3_material_line_identity_price_recovery_v1_checkpoint.json")
    if not path.exists():
        return {"status": "NO_CHECKPOINT", "opportunity_id": opportunity_id}
    ck = json.loads(path.read_text(encoding="utf-8"))
    row = (ck.get("opportunities") or {}).get(opportunity_id)
    if not row:
        return {"status": "NOT_IN_CORPUS", "opportunity_id": opportunity_id}
    owner = row.get("owner_view") or {}
    return {
        "opportunity_id": opportunity_id,
        "owner_view": {
            "material_lines": owner.get("material_lines"),
            "identity_ready": owner.get("identity_ready"),
            "production_priced": owner.get("production_priced"),
            "quote_required": owner.get("quote_required"),
            "ambiguous": owner.get("ambiguous"),
            "material_coverage": owner.get("material_coverage"),
            "revenue_confidence": owner.get("revenue_confidence"),
            "next_blocker": owner.get("next_blocker"),
            "expected_profit": None,
            "headline": "PROFIT NOT YET PROVEN",
        },
        "coverage": row.get("coverage"),
        "source_stats": row.get("source_stats"),
    }


@app.get("/api/m3/basket-full-funnel/opportunity/{opportunity_id}")
def api_m3_basket_full_funnel_opportunity(opportunity_id: str):
    """Owner-facing canonical stage, next action, and what-can-hurt-us for one opportunity."""
    import json

    from m3_data_root import data_path

    strict_ck = data_path("m3_line_basket_completion_strict_economics_v1_checkpoint.json")
    path = strict_ck if strict_ck.exists() else data_path("m3_basket_full_funnel_reconcile_v1_checkpoint.json")
    if not path.exists():
        return {"status": "NO_CHECKPOINT", "opportunity_id": opportunity_id}
    ck = json.loads(path.read_text(encoding="utf-8"))
    row = (ck.get("opportunities") or {}).get(opportunity_id)
    if not row:
        return {"status": "NOT_IN_CORPUS", "opportunity_id": opportunity_id}
    owner = row.get("owner_view") or {}
    econ = row.get("economics") or {}
    # Never surface positive profit as actionable when economics not ready
    if econ.get("economics_status") == "ECONOMICS_NOT_READY" or econ.get("profit_confidence") == "PROFIT_UNPROVEN":
        owner = {
            **owner,
            "expected_profit": None,
            "headline": "PROFIT NOT YET PROVEN",
        }
    return {
        "opportunity_id": opportunity_id,
        "canonical_stage": row.get("canonical_stage"),
        "next_action": row.get("next_action") or owner.get("next_action"),
        "what_can_hurt_us": row.get("what_can_hurt_us"),
        "owner_view": {
            **owner,
            "basket_coverage": owner.get("basket_coverage") or (row.get("coverage") or {}).get("LINE_COUNT_COVERAGE"),
            "material_coverage": owner.get("material_coverage") or (row.get("coverage") or {}).get("MATERIAL_VALUE_COVERAGE"),
            "lines_priced": owner.get("lines_priced") or (row.get("coverage") or {}).get("priced_executable"),
            "material_lines_unresolved": owner.get("material_lines_unresolved"),
            "expected_profit": owner.get("expected_profit"),
            "profit_confidence": owner.get("profit_confidence") or econ.get("profit_confidence"),
            "unresolved_cost_exposure": owner.get("unresolved_cost_exposure") or econ.get("unresolved_cost_exposure"),
            "next_action": owner.get("next_action"),
        },
        "basket": row.get("basket"),
        "economics": econ,
        "coverage": row.get("coverage"),
        "lines_summary": {
            k: (row.get("lines") or {}).get(k)
            for k in (
                "TOTAL_LINES",
                "PRICED_LINES",
                "line_coverage",
                "QUOTE_REQUIRED_LINES",
            )
        },
    }


@app.get("/api/m3/full-production-e2e/last-report")
def api_m3_full_production_e2e_last_report():
    """Last persisted full-production E2E completion report."""
    import json

    from m3_data_root import data_path

    path = data_path("m3_full_production_e2e_last_report.json")
    if not path.exists():
        return {"error": "no_report"}
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/m3/bidnet-recovery/funnel")
def api_m3_bidnet_recovery_funnel():
    from bidnet_recovery import bidnet_recovery_funnel

    return bidnet_recovery_funnel()


@app.get("/api/m3/bidnet-auth/status")
def api_m3_bidnet_auth_status():
    from bidnet_auth import owner_connection_status

    return owner_connection_status()


@app.post("/api/m3/bidnet-auth/test-connection")
def api_m3_bidnet_auth_test_connection():
    """Attempt BidNet session reuse or automatic login. Never returns credentials."""
    from bidnet_auth import test_connection

    return test_connection()


@app.post("/api/m3/bidnet-auth/debug-fetch")
def api_m3_bidnet_auth_debug_fetch(body: dict | None = None):
    """Authenticated fetch diagnostics for one BidNet URL — no HTML body, no secrets."""
    import re

    from bidnet_auth import BidNetAuthenticatedClient

    payload = body or {}
    url = str(payload.get("url") or "").strip()
    if not url.startswith("http") or "bidnet" not in url.lower():
        return {"ok": False, "error": "bidnet_url_required"}
    with BidNetAuthenticatedClient() as client:
        auth = client.ensure_authenticated()
        if not auth.authenticated:
            return {"ok": False, "auth": auth.to_dict()}
        html = client.fetch_html(url)
        low = (html or "").lower()
        title_m = re.search(r"<title>([^<]{0,160})</title>", html or "", re.I)
        page_opts = re.findall(
            r'data-page-number="(\d+)"[^>]*data-href="([^"]*)"', html or "", re.I
        )[:8]
        if not page_opts:
            page_opts = re.findall(
                r'data-href="([^"]*)"[^>]*data-page-number="(\d+)"', html or "", re.I
            )[:8]
            page_opts = [(b, a) for a, b in page_opts]
        return {
            "ok": True,
            "auth": auth.to_dict(),
            "final_url": str(getattr(client._page, "url", "") or "")[:220],
            "html_len": len(html or ""),
            "title_snippet": (title_m.group(1) if title_m else "")[:120],
            "has_mets_field": "mets-field" in low,
            "has_member_only": "member-only" in low or "registered members only" in low,
            "has_closing_date": "closing date" in low,
            "has_issuing_org": "issuing organization" in low,
            "has_solicitation_number": "solicitation number" in low,
            "has_login_form": 'type="password"' in low or "authentication/login" in low,
            "has_pdf_link": bool(re.search(r"\.pdf", low)),
            "locked_field_hits": len(re.findall(r"member-only-info|registered members only", low)),
            "has_mets_table_row": "mets-table-row" in low,
            "has_mets_pagination": "mets-pagination" in low,
            "page_number_select": "pagenumberselect" in low or 'id="pagenumber' in low,
            "reported_results": (
                re.search(r"([\d,]+)\s+results", html or "", re.I).group(1)
                if re.search(r"([\d,]+)\s+results", html or "", re.I)
                else None
            ),
            "pagination_options_sample": [{"page": p, "href": h[:160]} for p, h in page_opts],
        }


@app.post("/api/m3/bidnet-discovery/harvest")
def api_m3_bidnet_discovery_harvest(body: dict | None = None):
    """Harvest BidNet search results.

    Default async=true so long Playwright work does not trip proxy timeouts.
    Pass async=false only for tiny smoke tests.
    max_results>=7000 or mode=partitioned → state-partitioned full universe.
    """
    payload = body or {}
    async_mode = bool(payload.get("async", True))
    max_results = int(payload.get("max_results") or 20)
    max_pages = int(payload.get("max_pages") or 4)
    open_details = bool(payload.get("open_details", True))
    detail_limit = payload.get("detail_limit")
    persist = bool(payload.get("persist", True))
    mode = payload.get("mode")
    if async_mode:
        from m3_auth_jobs import start_bidnet_harvest_job

        return start_bidnet_harvest_job(
            max_results=max_results,
            max_pages=max_pages,
            open_details=open_details,
            detail_limit=int(detail_limit) if detail_limit is not None else None,
            persist=persist,
            mode=str(mode) if mode else None,
        )
    if (mode or "").lower() in {"partitioned", "full", "state"} or max_results >= 7000:
        from bidnet_discovery import run_bidnet_partitioned_harvest

        return run_bidnet_partitioned_harvest(
            max_results=max_results,
            max_pages_per_partition=min(max(max_pages, 50), 400),
            persist=persist,
            use_auth_seed=bool(payload.get("use_auth", True)),
        )
    from bidnet_discovery import run_bidnet_authenticated_harvest

    return run_bidnet_authenticated_harvest(
        max_results=max_results,
        max_pages=max_pages,
        open_details=open_details,
        detail_limit=detail_limit,
        persist=persist,
        use_auth=bool(payload.get("use_auth", True)),
    )


@app.post("/api/m3/bidnet-gap-closure/run")
def api_m3_bidnet_gap_closure_run():
    """Classify and retry the 2,403-id harvest gap. Does not retune coverage thresholds."""
    from m3_auth_jobs import start_bidnet_gap_closure_job

    return start_bidnet_gap_closure_job()


@app.get("/api/m3/bidnet-gap-closure/report")
def api_m3_bidnet_gap_closure_report(format: str = "json"):
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_gap_closure_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    report = json.loads(path.read_text(encoding="utf-8"))
    if format == "text":
        from bidnet_gap_closure.report import format_gap_report

        return Response(content=format_gap_report(report), media_type="text/plain")
    return report


@app.post("/api/m3/bidnet-downstream/run")
def api_m3_bidnet_downstream_run():
    """Classify the frozen 21,976 valid-open BidNet corpus. Does not rerun discovery."""
    from m3_auth_jobs import start_bidnet_downstream_job

    return start_bidnet_downstream_job()


@app.post("/api/m3/bidnet-downstream/deep")
def api_m3_bidnet_downstream_deep(body: dict | None = None):
    """Authenticated detail/package for PRODUCT + MIXED. Does not rerun discovery or call SAM."""
    from m3_auth_jobs import start_bidnet_downstream_deep_job

    payload = body or {}
    max_items = payload.get("max_items")
    return start_bidnet_downstream_deep_job(
        time_budget_s=int(payload.get("time_budget_s") or 5400),
        max_items=int(max_items) if max_items is not None else None,
    )


@app.post("/api/m3/bidnet-engine/run")
def api_m3_bidnet_engine_run(body: dict | None = None):
    """Incremental/parallel BidNet engine validation + accelerated baseline batch."""
    from m3_auth_jobs import start_bidnet_engine_job

    payload = body or {}
    return start_bidnet_engine_job(
        scale_sample_size=int(payload.get("scale_sample_size") or 8),
        baseline_batch=int(payload.get("baseline_batch") or 60),
        stress_n=int(payload.get("stress_n") or 5000),
        time_budget_s=int(payload.get("time_budget_s") or 2400),
    )


@app.post("/api/m3/bidnet-engine/recovery/run")
def api_m3_bidnet_engine_recovery_run(body: dict | None = None):
    """Recover hung engine job, isolate resources, stability-retest, resume baseline."""
    from m3_auth_jobs import start_bidnet_engine_recovery_job

    payload = body or {}
    return start_bidnet_engine_recovery_job(
        stability_sample=int(payload.get("stability_sample") or 6),
        resume_batch=int(payload.get("resume_batch") or 40),
    )


@app.post("/api/m3/bidnet-production/run")
def api_m3_bidnet_production_run(body: dict | None = None):
    """Canary-gated baseline completion → permanent incremental production."""
    from m3_auth_jobs import start_bidnet_baseline_production_job

    payload = body or {}
    return start_bidnet_baseline_production_job(
        canary_s=int(payload.get("canary_s") or 3600),
        baseline_budget_s=int(payload.get("baseline_budget_s") or 30 * 3600),
        skip_canary=bool(payload.get("skip_canary") or False),
    )


@app.post("/api/m3/bidnet-money/run")
def api_m3_bidnet_money_run(body: dict | None = None):
    """Terminate stalled baseline canary; run real downstream money sprint."""
    from m3_auth_jobs import start_bidnet_money_path_job

    payload = body or {}
    return start_bidnet_money_path_job(
        canary_n=int(payload.get("canary_n") or 20),
        sprint_n=int(payload.get("sprint_n") or 100),
        max_n=int(payload.get("max_n") or 250),
    )


@app.get("/api/m3/bidnet-money/precheck")
def api_m3_bidnet_money_precheck():
    from bidnet_engine.money_path import inspect_durable_checkpoint

    out = inspect_durable_checkpoint()
    out["build_version"] = APP_BUILD_VERSION
    out["stalled_job"] = "BNP-97657480a0d0"
    return out


@app.post("/api/m3/bidnet-money/terminate-stalled")
def api_m3_bidnet_money_terminate_stalled(body: dict | None = None):
    from bidnet_engine.money_path import terminate_stalled_job

    payload = body or {}
    return terminate_stalled_job(str(payload.get("job_id") or "BNP-97657480a0d0"))


@app.get("/api/m3/bidnet-money/status")
def api_m3_bidnet_money_status():
    import json

    from m3_data_root import data_path

    path = data_path("m3_money_path_v1_status.json")
    if not path.exists():
        return {"status": "NO_STATUS", "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/bidnet-money/report")
def api_m3_bidnet_money_report(format: str = "json"):
    import json

    from m3_data_root import data_path

    path = data_path("m3_money_path_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    report = json.loads(path.read_text(encoding="utf-8"))
    if format == "text":
        from bidnet_engine.money_path import format_money_report

        return Response(content=format_money_report(report), media_type="text/plain")
    return report


@app.get("/api/m3/bidnet-money/today")
def api_m3_bidnet_money_today():
    import json

    from m3_data_root import data_path

    path = data_path("m3_money_path_v1_last_report.json")
    # Prefer LIVE canary scores; never surface fixture/seed rows as CALL TODAY
    live_path = data_path("m3_channel_fit_live_scores_v1.json")
    seed_path = data_path("m3_channel_fit_scores_v1.json")
    channel_path = live_path if live_path.exists() else None
    channel_queues = {
        "CALL_TODAY": [],
        "QUOTE_IF_CAPACITY": [],
        "WATCH": [],
        "PASS": [],
        "INSUFFICIENT_EVIDENCE": [],
    }
    channel_summary = {"scored": 0, "build": None, "live_only": True}
    if channel_path and channel_path.exists():
        try:
            from bidnet_engine.channel_fit_canary import is_fixture_row
            from channel_fit.engine import queue_buckets

            cdoc = json.loads(channel_path.read_text(encoding="utf-8"))
            rows = [
                r
                for r in (cdoc.get("rows") or [])
                if isinstance(r, dict) and not is_fixture_row(r) and r.get("live_bidnet") is not False
            ]
            # Require live_bidnet True when source is live canary
            if cdoc.get("source") == "live_bidnet_canary":
                rows = [r for r in rows if r.get("live_bidnet") is True]
            channel_queues = queue_buckets(rows)
            channel_summary = {
                "scored": len(rows),
                "build": cdoc.get("build"),
                "live_only": True,
                "fixture_filtered": True,
                "CALL_TODAY": len(channel_queues.get("CALL_TODAY") or []),
                "QUOTE_IF_CAPACITY": len(channel_queues.get("QUOTE_IF_CAPACITY") or []),
                "WATCH": len(channel_queues.get("WATCH") or []),
                "PASS": len(channel_queues.get("PASS") or []),
                "INSUFFICIENT_EVIDENCE": len(channel_queues.get("INSUFFICIENT_EVIDENCE") or []),
            }
        except Exception as exc:
            channel_summary["error"] = str(exc)
    elif seed_path.exists():
        # Seed/fixture file exists but must not populate production CALL TODAY
        channel_summary["seed_present_but_hidden"] = True
        channel_summary["build"] = "fixtures_suppressed"

    if not path.exists():
        return {
            "NEW_ACTIONABLE_DEALS_TODAY": 0,
            "TARGET": 10,
            "READY_FOR_QUOTE": 0,
            "READY_FOR_BID": 0,
            "channel_queues": channel_queues,
            "channel_summary": channel_summary,
            "build_version": APP_BUILD_VERSION,
        }
    report = json.loads(path.read_text(encoding="utf-8"))
    kpi = report.get("daily_kpi") or {}
    return {
        **kpi,
        "actionable_now": report.get("actionable_now") or [],
        "ready_for_quote": report.get("ready_for_quote") or [],
        "promising_blocked": report.get("promising_blocked") or [],
        "channel_queues": channel_queues,
        "channel_summary": channel_summary,
        "build_version": APP_BUILD_VERSION,
    }


@app.post("/api/m3/channel-fit/canary/run")
def api_m3_channel_fit_canary_run(body: dict | None = None):
    """Start real live BidNet channel-fit canary (20; expand to 100 only if CALL_TODAY≥1)."""
    from m3_auth_jobs import start_channel_fit_canary_job

    payload = body or {}
    return start_channel_fit_canary_job(
        canary_n=int(payload.get("canary_n") or 20),
        expand_n=int(payload.get("expand_n") or 100),
        price_budget=int(payload.get("price_budget") or 25),
    )


@app.get("/api/m3/channel-fit/canary/status")
def api_m3_channel_fit_canary_status():
    import json

    from m3_data_root import data_path

    path = data_path("m3_channel_fit_canary_v1_status.json")
    if not path.exists():
        return {"status": "NO_STATUS", "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/channel-fit/canary/report")
def api_m3_channel_fit_canary_report(format: str = "json"):
    import json

    from m3_data_root import data_path

    path = data_path("m3_channel_fit_canary_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    report = json.loads(path.read_text(encoding="utf-8"))
    if format == "text":
        from bidnet_engine.channel_fit_canary import format_canary_report

        return Response(content=format_canary_report(report), media_type="text/plain")
    return report


@app.post("/api/m3/channel-fit/terminate-money")
def api_m3_channel_fit_terminate_money(body: dict | None = None):
    from bidnet_engine.channel_fit_canary import terminate_money_job

    payload = body or {}
    return terminate_money_job(str(payload.get("job_id") or "MNY-0525b77c8bf6"))


@app.post("/api/m3/channel-fit/rescore")
def api_m3_channel_fit_rescore(body: dict | None = None):
    """Re-score money-sprint / provided rows with channel-fit + MSRP screen. Does not restart sprint."""
    import json

    from channel_fit.engine import BUILD, persist_scores, queue_buckets, score_money_sprint_rows
    from m3_data_root import data_path

    payload = body or {}
    rows = payload.get("rows")
    if not isinstance(rows, list):
        money_rows = data_path("m3_money_path_v1_rows.json")
        if money_rows.exists():
            doc = json.loads(money_rows.read_text(encoding="utf-8"))
            rows = doc.get("rows") or doc.get("items") or (doc if isinstance(doc, list) else [])
        else:
            rows = []
    scored = score_money_sprint_rows(rows)
    path = persist_scores(scored)
    buckets = queue_buckets(scored)
    return {
        "build": BUILD,
        "build_version": APP_BUILD_VERSION,
        "scored": len(scored),
        "path": str(path),
        "counts": {k: len(v) for k, v in buckets.items()},
        "CALL_TODAY": buckets.get("CALL_TODAY") or [],
        "money_sprint_untouched": True,
    }


@app.get("/api/m3/channel-fit/queue")
def api_m3_channel_fit_queue():
    import json

    from bidnet_engine.channel_fit_canary import is_fixture_row
    from channel_fit.engine import BUILD, queue_buckets
    from m3_data_root import data_path

    live = data_path("m3_channel_fit_live_scores_v1.json")
    path = live if live.exists() else data_path("m3_channel_fit_scores_v1.json")
    if not path.exists() or path.name == "m3_channel_fit_scores_v1.json":
        # Seed/fixture scores never become production queue
        if not live.exists():
            return {
                "build": BUILD,
                "build_version": APP_BUILD_VERSION,
                "scored": 0,
                "live_only": True,
                "queues": {
                    "CALL_TODAY": [],
                    "QUOTE_IF_CAPACITY": [],
                    "WATCH": [],
                    "PASS": [],
                    "INSUFFICIENT_EVIDENCE": [],
                },
            }
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = [
        r
        for r in (doc.get("rows") or [])
        if isinstance(r, dict) and not is_fixture_row(r) and r.get("live_bidnet") is True
    ]
    buckets = queue_buckets(rows)
    return {
        "build": doc.get("build") or BUILD,
        "build_version": APP_BUILD_VERSION,
        "scored": len(rows),
        "live_only": True,
        "updated_at": doc.get("updated_at"),
        "counts": {k: len(v) for k, v in buckets.items()},
        "queues": buckets,
    }


@app.get("/api/m3/supplier-terms")
def api_m3_supplier_terms():
    import json

    from m3_data_root import data_path

    path = data_path("m3_supplier_terms_v1.json")
    if not path.exists():
        return {"items": [], "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/bidnet-production/status")
def api_m3_bidnet_production_status():
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_production_v1_status.json")
    if not path.exists():
        return {"status": "NO_STATUS", "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/bidnet-production/progress")
def api_m3_bidnet_production_progress():
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_production_v1_progress.json")
    if not path.exists():
        return {"status": "NO_PROGRESS", "progress_pct": 0, "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/bidnet-production/report")
def api_m3_bidnet_production_report(format: str = "json"):
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_production_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    report = json.loads(path.read_text(encoding="utf-8"))
    if format == "text":
        from bidnet_engine.production import format_production_report

        return Response(content=format_production_report(report), media_type="text/plain")
    return report


@app.get("/api/m3/bidnet-engine/report")
def api_m3_bidnet_engine_report(format: str = "json"):
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_engine_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    report = json.loads(path.read_text(encoding="utf-8"))
    if format == "text":
        from bidnet_engine.run import format_engine_report

        return Response(content=format_engine_report(report), media_type="text/plain")
    return report


@app.get("/api/m3/bidnet-engine/recovery/report")
def api_m3_bidnet_engine_recovery_report(format: str = "json"):
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_engine_recovery_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    report = json.loads(path.read_text(encoding="utf-8"))
    if format == "text":
        from bidnet_engine.recovery import format_recovery_report

        return Response(content=format_recovery_report(report), media_type="text/plain")
    return report


@app.get("/api/m3/bidnet-engine/progress")
def api_m3_bidnet_engine_progress():
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_engine_v1_progress.json")
    if not path.exists():
        return {"status": "NO_PROGRESS", "progress_pct": 0, "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/bidnet-engine/recovery/progress")
def api_m3_bidnet_engine_recovery_progress():
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_engine_recovery_v1_progress.json")
    if not path.exists():
        return {"status": "NO_PROGRESS", "progress_pct": 0, "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/bidnet-downstream/report")
def api_m3_bidnet_downstream_report(format: str = "json"):
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_downstream_v1_last_report.json")
    if not path.exists():
        return {"status": "NO_REPORT", "build_version": APP_BUILD_VERSION}
    report = json.loads(path.read_text(encoding="utf-8"))
    if format == "text":
        from bidnet_downstream.census import format_downstream_report

        return Response(content=format_downstream_report(report), media_type="text/plain")
    return report


@app.get("/api/m3/bidnet-downstream/progress")
def api_m3_bidnet_downstream_progress():
    import json

    from m3_data_root import data_path

    path = data_path("m3_bidnet_downstream_v1_progress.json")
    if not path.exists():
        return {"status": "NO_PROGRESS", "progress_pct": 0, "build_version": APP_BUILD_VERSION}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["build_version"] = APP_BUILD_VERSION
    return data


@app.get("/api/m3/auth-jobs/latest")
def api_m3_auth_job_latest():
    from m3_auth_jobs import latest_job

    job = latest_job()
    if not job:
        return {"ok": False, "error": "no_jobs"}
    return {"ok": True, **job}


@app.get("/api/m3/auth-jobs/{job_id}")
def api_m3_auth_job_status(job_id: str):
    from m3_auth_jobs import get_job

    job = get_job(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found", "job_id": job_id}
    return {"ok": True, **job}


@app.get("/api/m3/opengov-auth/status")
def api_m3_opengov_auth_status():
    from opengov_auth import owner_connection_status

    return owner_connection_status()


@app.post("/api/m3/opengov-auth/test-connection")
def api_m3_opengov_auth_test_connection():
    from opengov_auth import test_connection

    return test_connection()


@app.post("/api/m3/opengov-discovery/run")
@app.post("/api/m3/opengov-discovery/harvest")
def api_m3_opengov_discovery_run(body: dict | None = None):
    """OpenGov cascade discovery. Default async=true to avoid proxy timeouts."""
    payload = body or {}
    async_mode = bool(payload.get("async", True))
    max_entities = payload.get("max_entities")
    max_pages = int(payload.get("max_pages") or 8)
    persist = bool(payload.get("persist", True))
    mode = str(payload.get("mode") or "cascade")
    allow_browser = bool(payload.get("allow_browser", False))
    # None / "all" → full known entity set
    if max_entities in (None, "", "all", "ALL"):
        me: int | None = None if mode == "cascade" else 10
    else:
        me = int(max_entities)
    if async_mode:
        from m3_auth_jobs import start_opengov_discovery_job

        return start_opengov_discovery_job(
            max_entities=me if me is not None else None,
            max_pages=max_pages,
            persist=persist,
            mode=mode,
            allow_browser=allow_browser,
        )
    from opengov_discovery.cascade import run_opengov_cascade_discovery

    return run_opengov_cascade_discovery(
        max_entities=me,
        max_pages=max_pages,
        persist=persist,
        use_auth=bool(payload.get("use_auth", True)),
        allow_browser=allow_browser,
    )


@app.post("/api/m3/opengov-discovery/route-map")
def api_m3_opengov_route_map(body: dict | None = None):
    """Async route-mapping pass across known OpenGov entities."""
    payload = body or {}
    max_entities = payload.get("max_entities")
    me = None if max_entities in (None, "", "all", "ALL") else int(max_entities)
    from m3_auth_jobs import start_opengov_discovery_job

    return start_opengov_discovery_job(
        max_entities=me,
        max_pages=int(payload.get("max_pages") or 2),
        persist=bool(payload.get("persist", False)),
        mode="cascade",
        allow_browser=bool(payload.get("allow_browser", False)),
    )


@app.get("/api/m3/opengov-discovery/resolver")
def api_m3_opengov_resolver_status():
    from opengov_discovery.route_resolver import OpenGovRouteResolver

    r = OpenGovRouteResolver()
    return {"kind": "OpenGovRouteResolverStatus", **r.telemetry_summary()}


@app.get("/api/m3/euna-auth/status")
def api_m3_euna_auth_status():
    from euna_auth import owner_connection_status

    return owner_connection_status()


@app.post("/api/m3/euna-auth/test-connection")
def api_m3_euna_auth_test_connection():
    """Async diagnostic — Playwright login must not block the proxy."""
    from m3_auth_jobs import start_euna_auth_diagnostic_job

    return start_euna_auth_diagnostic_job()


@app.post("/api/m3/euna-auth/diagnostic")
def api_m3_euna_auth_diagnostic(body: dict | None = None):
    """Stage A auth diagnostic. Default async=true."""
    payload = body or {}
    if bool(payload.get("async", True)):
        from m3_auth_jobs import start_euna_auth_diagnostic_job

        return start_euna_auth_diagnostic_job()
    from euna_auth import run_auth_diagnostic

    return run_auth_diagnostic(persist_screenshot=bool(payload.get("screenshot", True)))


@app.post("/api/m3/euna-discovery/api-probe")
def api_m3_euna_api_probe(body: dict | None = None):
    """Discover real opportunity API endpoints from authenticated session."""
    from m3_auth_jobs import start_euna_api_probe_job

    return start_euna_api_probe_job()


@app.post("/api/m3/euna-discovery/run")
@app.post("/api/m3/euna-discovery/harvest")
def api_m3_euna_discovery_run(body: dict | None = None):
    """Euna Supplier Network discovery (central by default). Default async=true."""
    payload = body or {}
    async_mode = bool(payload.get("async", True))
    max_entities = payload.get("max_entities")
    max_pages = int(payload.get("max_pages") or 40)
    max_results = int(payload.get("max_results") or 5000)
    persist = bool(payload.get("persist", True))
    mode = payload.get("mode") or "central"
    if async_mode:
        from m3_auth_jobs import start_euna_discovery_job

        return start_euna_discovery_job(
            max_entities=int(max_entities) if max_entities is not None else 10,
            max_pages=max_pages,
            max_results=max_results,
            persist=persist,
            mode=str(mode),
        )
    if str(mode).lower() in {"portals", "agency", "hubs"}:
        from euna_discovery import run_euna_discovery

        return run_euna_discovery(
            max_entities=max_entities,
            max_pages=max_pages,
            persist=persist,
            use_auth=bool(payload.get("use_auth", True)),
        )
    from euna_discovery import run_euna_central_discovery

    return run_euna_central_discovery(
        max_results=max_results,
        max_pages=max_pages,
        persist=persist,
        use_auth=bool(payload.get("use_auth", True)),
    )


@app.get("/api/m3/source-coverage/roadmap")
def api_m3_source_coverage_roadmap():
    """Free-national source priority + coverage categories (Euna = PAID_OPTIONAL)."""
    from free_source_roadmap import source_coverage_categories

    payload = source_coverage_categories()
    payload["build_version"] = APP_BUILD_VERSION
    try:
        from m3_canonical_discovery_bridge import discovery_health_payload

        health = discovery_health_payload()
        payload["canonical_live"] = health.get("currently_available")
        payload["canonical_total"] = health.get("canonical_opportunities")
    except Exception:
        payload["canonical_live"] = None
    return payload


@app.get("/api/m3/source-coverage/production")
def api_m3_source_coverage_production():
    """Owner coverage: BidNet + OpenGov free health; Euna tracked as PAID_OPTIONAL only."""
    import json

    out: dict = {
        "kind": "FreeNationalSourceCoverage",
        "build_version": APP_BUILD_VERSION,
        "strategy": "FREE_NATIONAL_MULTI_STATE_FIRST",
        "owner": {},
        "combined": {},
        "national_health_sources": ["bidnet", "opengov"],
        "paid_optional_sources": ["euna"],
    }
    try:
        from bidnet_auth import owner_connection_status as bn

        out["bidnet"] = bn()
    except Exception as exc:
        out["bidnet"] = {"error": type(exc).__name__}
    try:
        from opengov_auth import owner_connection_status as og

        out["opengov"] = og()
    except Exception as exc:
        out["opengov"] = {"error": type(exc).__name__}
    try:
        from euna_auth import owner_connection_status as eu

        out["euna"] = eu()
    except Exception as exc:
        out["euna"] = {"error": type(exc).__name__}
    try:
        from m3_data_root import data_path

        reports: dict[str, Any] = {}
        for key, rel in (
            ("bidnet_last_harvest", "bidnet_auth/last_harvest_report.json"),
            ("bidnet_partitioned", "bidnet_auth/last_partitioned_harvest.json"),
            ("opengov_last_discovery", "opengov_auth/last_discovery_report.json"),
            ("opengov_public", "opengov_auth/last_public_discovery_report.json"),
            ("euna_last_discovery", "euna_auth/last_discovery_report.json"),
        ):
            p = data_path(rel)
            if p.exists():
                reports[key] = json.loads(p.read_text(encoding="utf-8"))
                out[key] = reports[key]

        bn_p = reports.get("bidnet_partitioned") or reports.get("bidnet_last_harvest") or {}
        bn_h = bn_p.get("harvest") if isinstance(bn_p.get("harvest"), dict) else bn_p
        reported = bn_h.get("reported_open_ui") or bn_h.get("reported_total") or (out.get("bidnet") or {}).get("reported_open")
        retrieved = bn_h.get("retrieved_unique") or bn_h.get("retrieved_total") or (out.get("bidnet") or {}).get("harvested")
        out["owner"]["bidnet"] = {
            "reported_open": reported,
            "retrieved": retrieved,
            "retrieval_pct": bn_h.get("retrieval_pct")
            or (
                round(100.0 * float(retrieved) / max(1, float(reported)), 2)
                if reported and retrieved
                else None
            ),
            "pagination_complete": bn_h.get("pagination_complete"),
            "partition_method": bn_h.get("partition_method") or bn_p.get("partition_method"),
            "net_new": bn_h.get("net_new") or (bn_h.get("canonical_merge") or {}).get("new"),
            "details": (bn_h.get("detail_stats") or {}).get("details_opened")
            if isinstance(bn_h.get("detail_stats"), dict)
            else None,
            "documents": (bn_h.get("detail_stats") or {}).get("documents_found")
            if isinstance(bn_h.get("detail_stats"), dict)
            else None,
            "remaining_gap": bn_h.get("remaining_gap"),
        }

        og_d = reports.get("opengov_last_discovery") or reports.get("opengov_public") or {}
        pub = og_d.get("public_discovery") if isinstance(og_d.get("public_discovery"), dict) else {}
        out["owner"]["opengov"] = {
            "known_entities": og_d.get("known_entities") or (out.get("opengov") or {}).get("known_entities"),
            "attempted": og_d.get("entities_attempted"),
            "public_working": og_d.get("working_public") or pub.get("working_public"),
            "auth_working": og_d.get("working_auth"),
            "anti_bot": og_d.get("anti_bot_public") or pub.get("anti_bot"),
            "raw": og_d.get("raw_opportunities") or pub.get("raw_opportunities"),
            "net_new": og_d.get("net_new") or (og_d.get("canonical_merge") or {}).get("new"),
            "documents": og_d.get("documents_recovered"),
        }

        eu_d = reports.get("euna_last_discovery") or {}
        eu_status = out.get("euna") or {}
        out["owner"]["euna"] = {
            "coverage_category": "PAID_OPTIONAL",
            "source_role": eu_status.get("source_role") or "OPTIONAL_TARGETED_SOURCE",
            "owner_label": eu_status.get("owner_label") or "OPTIONAL — PAID STATE ACCESS",
            "national_discovery_enabled": eu_status.get("national_discovery_enabled", False),
            "enabled_states": eu_status.get("enabled_states_display") or "NONE",
            "counts_toward_national_health": False,
            "auth_state": eu_status.get("auth_status")
            or (
                (eu_d.get("auth") or {}).get("status")
                if isinstance(eu_d.get("auth"), dict)
                else eu_status.get("status")
            ),
            "status": eu_status.get("status") or "PAID_OPTIONAL",
            "central_reachable": eu_d.get("central_reachable"),
            "reported_open": eu_d.get("reported_total"),
            "retrieved": eu_d.get("retrieved_total") or eu_d.get("unique_records"),
            "net_new": eu_d.get("net_new") or (eu_d.get("canonical_merge") or {}).get("new"),
            "documents": eu_d.get("documents_recovered"),
            "blocker": eu_d.get("blocker"),
        }

        # Free-source national health — Euna net-new excluded from success metrics
        try:
            from m3_canonical_discovery_bridge import discovery_health_payload
            from free_source_roadmap import source_coverage_categories

            health = discovery_health_payload()
            free_net = sum(
                int(x or 0)
                for x in (
                    out["owner"]["bidnet"].get("net_new"),
                    out["owner"]["opengov"].get("net_new"),
                )
                if x is not None
            )
            out["combined"] = {
                "canonical_live": health.get("currently_available"),
                "canonical_total": health.get("canonical_opportunities"),
                "free_source_net_new_this_run": free_net,
                "paid_optional_net_new": out["owner"]["euna"].get("net_new") or 0,
                "net_new_this_run": free_net,  # national metric = free only
                "target_canonical_live": 50000,
                "product_candidates": None,
                "economics_ready": None,
                "profitable": None,
            }
            out["roadmap"] = source_coverage_categories()
            # National readiness uses BidNet + OpenGov only
            bn_ok = bool(
                out["owner"]["bidnet"].get("retrieval_pct") is None
                or (out["owner"]["bidnet"].get("retrieval_pct") or 0) >= 90
                or out["owner"]["bidnet"].get("pagination_complete")
            )
            og_partial = True  # OpenGov is active repair; not a hard fail for Euna absence
            out["national_source_health"] = {
                "bidnet_ok": bn_ok,
                "opengov_in_repair": og_partial,
                "euna_affects_score": False,
                "unattended_free_ready": bn_ok,
                "free_source_coverage_score_inputs": ["bidnet", "opengov"],
            }
        except Exception:
            out["combined"] = {"canonical_live": None}
    except Exception:
        pass
    return out


@app.get("/api/m3/opengov-recovery/funnel")
def api_m3_opengov_recovery_funnel():
    from opengov_recovery import opengov_recovery_funnel

    return opengov_recovery_funnel()


@app.post("/api/m3/opengov-recovery/run")
def api_m3_opengov_recovery_run(body: dict | None = None):
    from opengov_recovery import run_opengov_recovery

    payload = body or {}
    use_auth = payload.get("use_auth")
    return run_opengov_recovery(
        limit=payload.get("limit"),
        resume=bool(payload.get("resume", True)),
        persist=bool(payload.get("persist", True)),
        force=bool(payload.get("force", False)),
        use_auth=None if use_auth is None else bool(use_auth),
    )


@app.post("/api/m3/opengov-public-docs/run")
def api_m3_opengov_public_docs_run(body: dict | None = None):
    """Async OpenGov public document recovery (quality-gated, free routes only)."""
    from m3_auth_jobs import start_opengov_public_docs_job

    payload = body or {}
    return start_opengov_public_docs_job(
        limit=int(payload.get("limit") or 20),
        resume=bool(payload.get("resume", True)),
        download=bool(payload.get("download", True)),
        max_entities=payload.get("max_entities"),
        handoff_line_items=bool(payload.get("handoff_line_items", True)),
    )


@app.get("/api/m3/opengov-public-docs/last-report")
def api_m3_opengov_public_docs_last_report():
    from m3_data_root import data_path

    path = data_path("m3_opengov_public_docs_last_report.json")
    if not path.exists():
        return {"ok": False, "error": "no_report"}
    import json

    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/m3/official-source/run")
def api_m3_official_source_run(body: dict | None = None):
    """Async BidNet → official source resolution + free package recovery."""
    from m3_auth_jobs import start_official_source_job

    payload = body or {}
    return start_official_source_job(
        limit=int(payload.get("limit") or 500),
        resume=bool(payload.get("resume", True)),
        download=bool(payload.get("download", True)),
        handoff_line_items=bool(payload.get("handoff_line_items", True)),
    )


@app.get("/api/m3/official-source/last-report")
def api_m3_official_source_last_report():
    from m3_data_root import data_path

    path = data_path("m3_official_source_last_report.json")
    if not path.exists():
        return {"ok": False, "error": "no_report"}
    import json

    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/m3/product-identity/run")
def api_m3_product_identity_run(body: dict | None = None):
    """Async product-identity breakthrough on recovered OpenGov packages."""
    from m3_auth_jobs import start_product_identity_job

    payload = body or {}
    return start_product_identity_job(
        line_limit=int(payload.get("line_limit") or 500),
        resume=bool(payload.get("resume", True)),
        handoff=bool(payload.get("handoff", True)),
        max_packages=payload.get("max_packages"),
    )


@app.get("/api/m3/product-identity/last-report")
def api_m3_product_identity_last_report():
    from m3_data_root import data_path

    path = data_path("m3_product_identity_last_report.json")
    if not path.exists():
        return {"ok": False, "error": "no_report"}
    import json

    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/m3/evidence-breakthrough/run")
def api_m3_evidence_breakthrough_run(body: dict | None = None):
    """Async evidence breakthrough: gov-value + public acquisition cost on identity corpus."""
    from m3_auth_jobs import start_evidence_breakthrough_job

    payload = body or {}
    return start_evidence_breakthrough_job(
        stage_limit=int(payload.get("stage_limit") or payload.get("limit") or 50),
        resume=bool(payload.get("resume", True)),
        skip_acquisition=bool(payload.get("skip_acquisition", False)),
    )


@app.get("/api/m3/evidence-breakthrough/last-report")
def api_m3_evidence_breakthrough_last_report():
    from m3_data_root import data_path
    import json

    path = data_path("m3_evidence_breakthrough_last_report.json")
    if not path.exists():
        return {"ok": False, "error": "no_report"}
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/m3/evidence-breakthrough/status")
def api_m3_evidence_breakthrough_status():
    from m3_data_root import data_path
    import json

    store_path = data_path("m3_evidence_breakthrough_store.json")
    ck_path = data_path("m3_evidence_breakthrough_checkpoint.json")
    out: dict = {"ok": True, "build_version": APP_BUILD_VERSION}
    if store_path.exists():
        store = json.loads(store_path.read_text(encoding="utf-8"))
        out["stats"] = store.get("stats")
        out["lines"] = len(store.get("by_line") or {})
        out["opportunities"] = len(store.get("by_opportunity") or {})
    if ck_path.exists():
        ck = json.loads(ck_path.read_text(encoding="utf-8"))
        out["checkpoint"] = {"done": len(ck.get("done_keys") or []), "run_id": ck.get("run_id")}
    return out


@app.post("/api/m3/scale-evidence-profit/run")
def api_m3_scale_evidence_profit_run(body: dict | None = None):
    """Async scale both-sides → basket economics → lender pipeline."""
    from m3_auth_jobs import start_scale_evidence_profit_job

    payload = body or {}
    return start_scale_evidence_profit_job(
        mine_buyers=int(payload.get("mine_buyers") or 50),
        resume=bool(payload.get("resume", True)),
        identity_limit=payload.get("identity_limit"),
    )


@app.post("/api/m3/public-price-search/run")
def api_m3_public_price_search_run(body: dict | None = None):
    """Async human-like public price search (staged A→B→C)."""
    from m3_auth_jobs import start_public_price_search_job

    payload = body or {}
    grades = payload.get("grades") or ["A"]
    if isinstance(grades, str):
        grades = [g.strip() for g in grades.split(",") if g.strip()]
    return start_public_price_search_job(
        stage_limit=int(payload.get("stage_limit") or 25),
        grades=tuple(grades),
        go_metro_priority=bool(payload.get("go_metro_priority", True)),
        resume=bool(payload.get("resume", False)),
    )


@app.get("/api/m3/public-price-search/last-report")
def api_m3_public_price_search_last_report():
    from m3_data_root import data_path
    import json

    path = data_path("m3_public_price_search_last_report.json")
    if not path.exists():
        return {"ok": False, "error": "no_report"}
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/m3/scale-evidence-profit/last-report")
def api_m3_scale_evidence_profit_last_report():
    from m3_data_root import data_path
    import json

    path = data_path("m3_scale_evidence_profit_last_report.json")
    if not path.exists():
        return {"ok": False, "error": "no_report"}
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/m3/scale-evidence-profit/status")
def api_m3_scale_evidence_profit_status():
    from m3_data_root import data_path
    import json

    store_path = data_path("m3_scale_evidence_profit_store.json")
    out: dict = {"ok": True, "build_version": APP_BUILD_VERSION}
    if store_path.exists():
        store = json.loads(store_path.read_text(encoding="utf-8"))
        out["stats"] = store.get("stats")
        out["lines"] = len(store.get("by_line") or {})
        out["opportunities"] = len(store.get("by_opportunity") or {})
        out["lender_ready"] = len(store.get("lender_ready") or [])
        out["near_ready"] = len(store.get("near_ready_24h") or [])
        out["index_meta"] = store.get("index_meta")
    return out


@app.post("/api/m3/bidnet-recovery/run")
def api_m3_bidnet_recovery_run(body: dict | None = None):
    """Staged BidNet detail/document recovery. Default limit=100 for safe validation."""
    from bidnet_recovery import run_bidnet_recovery

    payload = body or {}
    use_auth = payload.get("use_auth")
    target_ids = payload.get("target_ids")
    if isinstance(target_ids, str):
        target_ids = [x.strip() for x in target_ids.split(",") if x.strip()]
    return run_bidnet_recovery(
        limit=int(payload.get("limit") or 100),
        batch_size=int(payload.get("batch_size") or 25),
        resume=bool(payload.get("resume", True)),
        persist=bool(payload.get("persist", True)),
        force=bool(payload.get("force", False)),
        min_tier=int(payload.get("min_tier") or 3),
        use_auth=None if use_auth is None else bool(use_auth),
        recovery_batch_size=payload.get("recovery_batch_size"),
        backlog_batch_size=payload.get("backlog_batch_size"),
        target_ids=list(target_ids) if isinstance(target_ids, list) else None,
        require_auth_blocker=bool(payload.get("require_auth_blocker", False)),
    )


@app.post("/api/m3/bidnet-free-package/run")
def api_m3_bidnet_free_package_run(body: dict | None = None):
    """Async BidNet free-package chase on AUTH_REQUIRED backlog (no membership)."""
    from m3_auth_jobs import start_bidnet_free_package_job

    payload = body or {}
    return start_bidnet_free_package_job(
        limit=int(payload.get("limit") or 500),
        resume=bool(payload.get("resume", True)),
        force=bool(payload.get("force", False)),
        stop_if_yield_below=payload.get("stop_if_yield_below", 0.005),
        universe_mode=bool(payload.get("universe_mode", False)),
    )


@app.get("/api/m3/bidnet-free-package/funnel")
def api_m3_bidnet_free_package_funnel():
    from bidnet_recovery import free_package_funnel_report

    return free_package_funnel_report()


@app.get("/api/m3/federal-dla/coverage")
def api_m3_federal_dla_coverage():
    """Federal + DLA coverage snapshot, reconciliation, gap queue (minimal operator surface)."""
    from discovery.federal_dla_coverage import load_federal_dla_coverage
    from discovery.federal_dla_enrichment import load_enrichment_checkpoint

    cov = load_federal_dla_coverage()
    try:
        enk = load_enrichment_checkpoint()
        cov["enrichment"] = {
            "version": enk.get("version"),
            "checkpoint_ids": len(enk.get("by_id") or {}),
            "last_campaign_metrics": enk.get("last_campaign_metrics"),
            "updated_at": enk.get("updated_at"),
        }
    except Exception as exc:  # noqa: BLE001
        cov["enrichment"] = {"error": str(exc)[:120]}
    return cov


@app.get("/api/m3/federal-dla/enrichment/{notice_id}")
def api_m3_federal_dla_enrichment_detail(notice_id: str):
    """Inspect enrichment checkpoint + readiness for one notice id."""
    from discovery.federal_dla_enrichment import load_enrichment_checkpoint

    ck = load_enrichment_checkpoint()
    row = (ck.get("by_id") or {}).get(notice_id) or (ck.get("by_id") or {}).get(notice_id.upper())
    return {
        "kind": "M3FederalDlaEnrichmentDetail",
        "notice_id": notice_id,
        "found": bool(row),
        "enrichment": row,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/product-resale/source-intelligence")
def api_m3_product_resale_source_intelligence():
    """Source recipes, ROI, categories, price paths, gaps — no bulk solicitations."""
    from product_resale_source_intelligence import load_source_intelligence, build_persisted_payload

    loaded = load_source_intelligence()
    if loaded:
        return loaded
    return build_persisted_payload()


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


@app.get("/api/m3/discovery/coverage-map")
def api_m3_discovery_coverage_map():
    """NATIONAL_PROCUREMENT_COVERAGE_MAP + yield + gaps + survivor funnel facts."""
    from m3_discovery_service import discovery_status
    from national_procurement_coverage_map import (
        build_discovery_coverage_health,
        build_discovery_gap_queue,
        build_national_procurement_coverage_map,
        build_product_survivor_universe,
        build_source_yield_analytics,
    )
    from m3_pipeline_store import M3PipelineStore

    st = discovery_status()
    per = None
    try:
        focus = st.get("last_successful_completion") or st.get("last_attempt") or {}
        metrics = focus.get("metrics") if isinstance(focus.get("metrics"), dict) else {}
        per = focus.get("per_source_summary") or metrics.get("per_source")
    except Exception:
        per = None
    store = M3PipelineStore()
    rows = store.all()
    cmap = build_national_procurement_coverage_map(per_source=per)
    yields = build_source_yield_analytics(per_source=per, opportunities=rows)
    survivors = build_product_survivor_universe(rows)
    gaps = build_discovery_gap_queue(coverage_map=cmap, yield_rows=yields, per_source=per)
    health = build_discovery_coverage_health(coverage_map=cmap, yield_rows=yields, funnel=survivors)
    return {
        "kind": "M3DiscoveryCoverageBundle",
        "coverage_map": cmap,
        "yield_analytics": yields[:80],
        "discovery_gaps": gaps,
        "coverage_health": health,
        "product_survivor_universe": survivors,
    }


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
        try:
            from operator_workflow.summary import attach_operator_workflow

            opportunity = attach_operator_workflow(dict(row), row, include_summary=True)
        except Exception:
            opportunity = row
        return {
            "opportunity": opportunity,
            "readiness": row.get("readiness_summary") or readiness_summary(row),
            "audit": store.audit_for(canonical_id)[-20:],
            "operator_workflow_state": opportunity.get("operator_workflow_state"),
            "operator_next_action": opportunity.get("operator_next_action"),
            "operator_blockers": opportunity.get("operator_blockers"),
            "state_conflict_detected": opportunity.get("state_conflict_detected"),
        }
    opps = []
    for r in store.all()[:100]:
        item = {
            "canonical_id": r["canonical_id"],
            "title": r.get("title"),
            "lifecycle": r.get("lifecycle"),
            "next_action": r.get("pending_next_action"),
            "deadline": r.get("deadline"),
            "stop_reason": r.get("stop_reason"),
        }
        try:
            from operator_workflow.summary import attach_operator_workflow

            item = attach_operator_workflow(item, r, include_summary=False)
        except Exception:
            pass
        opps.append(item)
    return {
        "count": len(store.all()),
        "opportunities": opps,
    }


@app.get("/api/m3/pipeline/next-action")
def api_m3_next_action(canonical_id: str):
    from m3_pipeline_store import M3PipelineStore
    from m3_lifecycle import determine_next_action

    store = M3PipelineStore()
    row = store.get(canonical_id)
    if not row:
        raise HTTPException(status_code=404, detail="opportunity not found")
    legacy = determine_next_action(row)
    try:
        from p0_prescale_hardening.next_action import determine_canonical_next_action

        ctx = {
            "hard_reject": str((legacy or {}).get("next_action") or "").upper()
            in {"SKIP", "REJECT", "HARD_REJECT"},
            "waiting_for_quote": "QUOTE" in str((legacy or {}).get("next_action") or "").upper(),
            "has_quote_packet": False,
            "revenue_not_ready": True,
            "needs_economics": False,
        }
        canonical = determine_canonical_next_action(ctx)
        return {
            **(legacy if isinstance(legacy, dict) else {"legacy": legacy}),
            "canonical_next_action": canonical.get("primary_next_action"),
            "canonical_detail": canonical,
            "engine": "p0_prescale_hardening.next_action",
            "build_version": APP_BUILD_VERSION,
        }
    except Exception:
        if isinstance(legacy, dict):
            return {**legacy, "build_version": APP_BUILD_VERSION}
        return {"result": legacy, "build_version": APP_BUILD_VERSION}


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


@app.get("/api/m3/portfolio/deals")
def api_m3_portfolio_deals():
    """Operator portfolio view — ranked deals with economics classes."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_portfolio_deal_analysis import portfolio_operator_view

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return portfolio_operator_view(store)


@app.post("/api/m3/portfolio/analyze")
def api_m3_portfolio_analyze(body: dict | None = None):
    """Bounded autonomous portfolio cycle (progressive tiers)."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_portfolio_deal_analysis import run_portfolio_cycle

    payload = body or {}
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return run_portfolio_cycle(
        store,
        persist=True,
        max_promote=max(1, min(25, int(payload.get("max_promote") or 12))),
        include_discovery_tick=bool(payload.get("include_discovery_tick")),
        resume=payload.get("resume", True) is not False,
    )


@app.get("/api/m3/portfolio/summary")
def api_m3_portfolio_summary():
    from m3_portfolio_deal_analysis import SUMMARY_KEY, _load_setting

    return _load_setting(SUMMARY_KEY) or {"kind": "PORTFOLIO_RUN_SUMMARY", "empty": True}


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
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_portfolio_deal_analysis import portfolio_operator_view

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    portfolio = portfolio_operator_view(store)
    dash = mobile_dashboard_summary()
    return {
        "opportunities": portfolio.get("deals") or dash.get("active_opportunities") or [],
        "top_cards": portfolio.get("top_cards") or [],
        "counts": portfolio.get("counts") or {},
        "count": len(portfolio.get("deals") or []),
        "filters_supported": portfolio.get("filters_supported"),
        "DEVELOPMENT_NO_OUTREACH": True,
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


@app.post("/api/m3/execution/analyze")
def api_m3_execution_analyze(body: dict | None = None):
    """TOP N transaction execution / capital readiness — local only, no outreach."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_execution_intelligence import analyze_execution_top_opportunities

    payload = body or {}
    limit = max(1, min(50, int(payload.get("limit") or 25)))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_execution_top_opportunities(store, limit=limit)


@app.get("/api/m3/execution/status")
def api_m3_execution_status():
    from m3_execution_intelligence import load_execution_index

    idx = load_execution_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    buckets = {"A": 0, "B": 0, "C": 0, "D": 0}
    for ei in by_id.values():
        if not isinstance(ei, dict):
            continue
        b = str(ei.get("BUCKET") or "")
        if "READY" in b:
            buckets["A"] += 1
        elif "COMMERCIAL" in b:
            buckets["B"] += 1
        elif "STRATEGIC" in b:
            buckets["C"] += 1
        else:
            buckets["D"] += 1
    return {
        "kind": "M3ExecutionStatus",
        "researched": len(by_id),
        "buckets": buckets,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.post("/api/m3/economics/analyze")
def api_m3_economics_analyze(body: dict | None = None):
    """TOP N deal economics / profit target status — local only."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_deal_economics import analyze_deal_economics_top

    payload = body or {}
    limit = max(1, min(50, int(payload.get("limit") or 25)))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_deal_economics_top(store, limit=limit)


@app.post("/api/m3/economics/simulate")
def api_m3_economics_simulate(body: dict | None = None):
    """What-if acquisition cost — does not persist as pricing evidence."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_deal_economics import simulate_cost_change, get_persisted_economics, build_deal_economics

    payload = body or {}
    cid = str(payload.get("canonical_id") or "").strip()
    cost = payload.get("acquisition_cost")
    if not cid or cost is None:
        raise HTTPException(status_code=400, detail="canonical_id and acquisition_cost required")
    try:
        cost_f = float(cost)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="acquisition_cost must be numeric") from exc
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    row = store.get(cid) or {"canonical_id": cid}
    # Attach persisted economics operator overrides if any
    de = get_persisted_economics(cid)
    if de and isinstance(row, dict):
        row = {**row, "deal_economics": de}
    return simulate_cost_change(row, cost_f)


@app.post("/api/m3/economics/operator-notes")
def api_m3_economics_operator_notes(body: dict | None = None):
    """Store strategic override notes — never hides actual economics."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_deal_economics import build_deal_economics, load_economics_index, save_economics_index

    payload = body or {}
    cid = str(payload.get("canonical_id") or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="canonical_id required")
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    row = store.get(cid)
    if not row:
        raise HTTPException(status_code=404, detail="opportunity not found")
    op = dict(row.get("operator_economics") or {})
    if "strategic_notes" in payload:
        op["strategic_notes"] = payload.get("strategic_notes")
    if "strategic_value" in payload:
        op["strategic_value"] = payload.get("strategic_value")
    if "accept_below_target" in payload:
        op["accept_below_target"] = bool(payload.get("accept_below_target"))
    if "target_profit_usd" in payload and payload.get("target_profit_usd") is not None:
        op["target_profit_usd"] = float(payload["target_profit_usd"])
    row = {**row, "operator_economics": op}
    pkg = build_deal_economics(row)
    store._rows[cid] = {**row, "deal_economics": pkg, "operator_economics": op}
    try:
        store.save()
    except Exception:
        pass
    idx = load_economics_index()
    by = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    by[cid] = pkg
    save_economics_index(by)
    return {"ok": True, "deal_economics": pkg, "DEVELOPMENT_NO_OUTREACH": True}


@app.get("/api/m3/economics/status")
def api_m3_economics_status():
    from m3_deal_economics import load_economics_index

    idx = load_economics_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    statuses = {"EXCEEDS_TARGET": 0, "MEETS_TARGET": 0, "WITHIN_ACCEPTABLE_RANGE": 0, "BELOW_TARGET": 0, "UNVIABLE": 0, "UNKNOWN": 0}
    for de in by_id.values():
        if not isinstance(de, dict):
            continue
        st = str(de.get("PROFIT_TARGET_STATUS") or "UNKNOWN")
        if st in statuses:
            statuses[st] += 1
        else:
            statuses["UNKNOWN"] += 1
    return {"kind": "M3DealEconomicsStatus", "researched": len(by_id), "profit_statuses": statuses, "DEVELOPMENT_NO_OUTREACH": True}


@app.post("/api/m3/product/analyze")
def api_m3_product_analyze(body: dict | None = None):
    """TOP N product identity + gated market pricing research."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_product_pricing import analyze_product_pricing_top

    payload = body or {}
    limit = max(1, min(50, int(payload.get("limit") or 25)))
    allow_paid = bool(payload.get("allow_paid_web", False))
    paid_limit = max(0, min(10, int(payload.get("paid_limit") or 5)))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_product_pricing_top(
        store, limit=limit, allow_paid_web=allow_paid, paid_limit=paid_limit
    )


@app.get("/api/m3/product/status")
def api_m3_product_status():
    from m3_product_pricing import load_product_index, MATCH_HIGH, MATCH_MEDIUM, MATCH_LOW, MATCH_UNKNOWN

    idx = load_product_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    matches = {MATCH_HIGH: 0, MATCH_MEDIUM: 0, MATCH_LOW: 0, MATCH_UNKNOWN: 0}
    levels = {"LEVEL_1": 0, "LEVEL_2": 0, "LEVEL_3": 0, "LEVEL_4": 0}
    for pp in by_id.values():
        if not isinstance(pp, dict):
            continue
        m = ((pp.get("PRODUCT_MATCH") or {}).get("PRODUCT_MATCH_CONFIDENCE") or MATCH_UNKNOWN)
        if m in matches:
            matches[m] += 1
        else:
            matches[MATCH_UNKNOWN] += 1
        lvl = str(((pp.get("MARKET_PRICE") or {}).get("primary_level") or "LEVEL_4"))
        if "LEVEL_1" in lvl:
            levels["LEVEL_1"] += 1
        elif "LEVEL_2" in lvl:
            levels["LEVEL_2"] += 1
        elif "LEVEL_3" in lvl:
            levels["LEVEL_3"] += 1
        else:
            levels["LEVEL_4"] += 1
    return {
        "kind": "M3ProductPricingStatus",
        "researched": len(by_id),
        "matches": matches,
        "pricing_levels": levels,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.post("/api/m3/package/analyze")
def api_m3_package_analyze(body: dict | None = None):
    """TOP N complete procurement package intelligence."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_procurement_package import analyze_procurement_packages_top

    payload = body or {}
    limit = max(1, min(50, int(payload.get("limit") or 25)))
    allow_paid = bool(payload.get("allow_paid_web", False))
    paid_limit = max(0, min(10, int(payload.get("paid_limit") or 3)))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_procurement_packages_top(
        store, limit=limit, allow_paid_web=allow_paid, paid_limit=paid_limit
    )


@app.get("/api/m3/package/status")
def api_m3_package_status():
    from m3_procurement_package import (
        load_package_index,
        MATCH_HIGH,
        MATCH_MEDIUM,
        MATCH_LOW,
        MATCH_UNKNOWN,
        READY_FOR_ECONOMICS,
        READY_FOR_PRICING,
        PARTIAL,
        NEEDS_DOCUMENTS,
        INSUFFICIENT_DATA,
    )

    idx = load_package_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    matches = {MATCH_HIGH: 0, MATCH_MEDIUM: 0, MATCH_LOW: 0, MATCH_UNKNOWN: 0}
    completeness = {
        READY_FOR_ECONOMICS: 0,
        READY_FOR_PRICING: 0,
        PARTIAL: 0,
        NEEDS_DOCUMENTS: 0,
        INSUFFICIENT_DATA: 0,
    }
    levels = {"LEVEL_1": 0, "LEVEL_2": 0, "LEVEL_3": 0, "LEVEL_4": 0}
    queues: dict[str, int] = {}
    for pkg in by_id.values():
        if not isinstance(pkg, dict):
            continue
        m = ((pkg.get("PRODUCT_IDENTITY") or {}).get("Identity_confidence") or MATCH_UNKNOWN)
        if m in matches:
            matches[m] += 1
        else:
            matches[MATCH_UNKNOWN] += 1
        st = ((pkg.get("COMMERCIAL_COMPLETENESS") or {}).get("COMMERCIAL_COMPLETENESS") or INSUFFICIENT_DATA)
        if st in completeness:
            completeness[st] += 1
        else:
            completeness[INSUFFICIENT_DATA] += 1
        lvl = str(((pkg.get("MARKET_PRICING") or {}).get("primary_level") or "LEVEL_4"))
        if "LEVEL_1" in lvl:
            levels["LEVEL_1"] += 1
        elif "LEVEL_2" in lvl:
            levels["LEVEL_2"] += 1
        elif "LEVEL_3" in lvl:
            levels["LEVEL_3"] += 1
        else:
            levels["LEVEL_4"] += 1
        q = str(((pkg.get("RESEARCH_READINESS") or {}).get("RESEARCH_QUEUE") or "UNKNOWN"))
        queues[q] = queues.get(q, 0) + 1
    return {
        "kind": "M3ProcurementPackageStatus",
        "researched": len(by_id),
        "identity": matches,
        "completeness": completeness,
        "pricing_levels": levels,
        "research_queues": queues,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.post("/api/m3/evidence/analyze")
def api_m3_evidence_analyze(body: dict | None = None):
    """TOP N document recovery + procurement evidence analysis."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_document_evidence import analyze_document_evidence_top

    payload = body or {}
    limit = max(1, min(50, int(payload.get("limit") or 25)))
    run_recovery = bool(payload.get("run_recovery", True))
    allow_paid = bool(payload.get("allow_paid", False))
    paid_limit = max(0, min(10, int(payload.get("paid_limit") or 2)))
    recovery_limit = max(0, min(25, int(payload.get("recovery_limit") or 10)))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_document_evidence_top(
        store,
        limit=limit,
        run_recovery=run_recovery,
        allow_paid=allow_paid,
        paid_limit=paid_limit,
        recovery_limit=recovery_limit,
    )


@app.get("/api/m3/evidence/status")
def api_m3_evidence_status():
    from m3_document_evidence import (
        load_evidence_index,
        COMPLETE_PURCHASE_PACKAGE,
        READY_FOR_PRICING,
        READY_FOR_SUPPLIER_RESEARCH,
        PARTIAL,
        DOCUMENT_RECOVERY_REQUIRED,
        INSUFFICIENT_DATA,
    )

    idx = load_evidence_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    completeness = {
        COMPLETE_PURCHASE_PACKAGE: 0,
        READY_FOR_PRICING: 0,
        READY_FOR_SUPPLIER_RESEARCH: 0,
        PARTIAL: 0,
        DOCUMENT_RECOVERY_REQUIRED: 0,
        INSUFFICIENT_DATA: 0,
    }
    queues: dict[str, int] = {}
    readiness_sum = 0
    readiness_n = 0
    for pkg in by_id.values():
        if not isinstance(pkg, dict):
            continue
        st = ((pkg.get("PROCUREMENT_COMPLETENESS") or {}).get("PROCUREMENT_COMPLETENESS") or INSUFFICIENT_DATA)
        if st in completeness:
            completeness[st] += 1
        else:
            completeness[INSUFFICIENT_DATA] += 1
        q = str(((pkg.get("RECOVERY_QUEUE") or {}).get("queue") or "UNKNOWN"))
        queues[q] = queues.get(q, 0) + 1
        sc = (pkg.get("COMMERCIAL_READINESS") or {}).get("score")
        if sc is not None:
            readiness_sum += int(sc)
            readiness_n += 1
    return {
        "kind": "M3DocumentEvidenceStatus",
        "researched": len(by_id),
        "completeness": completeness,
        "research_queues": queues,
        "average_commercial_readiness": round(readiness_sum / readiness_n, 1) if readiness_n else 0,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/evidence/queues")
def api_m3_evidence_queues(limit: int = Query(25, ge=1, le=100)):
    """VA-operable evidence recovery queues."""
    from m3_document_evidence import (
        load_evidence_index,
        build_queue_card,
        VA_ALLOWED_ACTIONS,
        VA_FORBIDDEN_ACTIONS,
    )

    idx = load_evidence_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    queues: dict[str, list] = {}
    for cid, pkg in by_id.items():
        if not isinstance(pkg, dict):
            continue
        card = build_queue_card({"canonical_id": cid, "title": ((pkg.get("PRODUCT_EVIDENCE") or {}).get("Manufacturer"))}, pkg)
        card["canonical_id"] = cid
        q = card.get("Queue") or "NEEDS_DOCUMENT_RECOVERY"
        queues.setdefault(q, []).append(card)
    for q in queues:
        queues[q] = queues[q][: max(1, min(limit, 50))]
    return {
        "kind": "M3EvidenceRecoveryQueues",
        "queues": queues,
        "VA_allowed_actions": sorted(VA_ALLOWED_ACTIONS),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.post("/api/m3/evidence/va/update")
def api_m3_evidence_va_update(body: dict | None = None):
    """VA evidence actions only — attach/status/escalate/note. No deals/bids/outreach."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_document_evidence import apply_va_evidence_update

    payload = body or {}
    cid = str(payload.get("canonical_id") or "")
    if not cid:
        raise HTTPException(status_code=400, detail="canonical_id required")
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return apply_va_evidence_update(
        store,
        cid,
        action=str(payload.get("action") or ""),
        note=payload.get("note"),
        status=payload.get("status"),
        attached_document=payload.get("attached_document") if isinstance(payload.get("attached_document"), dict) else None,
    )


@app.post("/api/m3/evidence-chain/validate")
def api_m3_evidence_chain_validate(body: dict | None = None):
    """Validate evidence chain preservation across pipeline opportunities."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_evidence_chain import analyze_evidence_chain_top

    payload = body or {}
    limit = max(1, min(50, int(payload.get("limit") or 25)))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_evidence_chain_top(store, limit=limit)


@app.get("/api/m3/evidence-chain/status")
def api_m3_evidence_chain_status():
    from m3_evidence_chain import load_chain_index

    idx = load_chain_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    urls = 0
    loss = 0
    for snap in by_id.values():
        if not isinstance(snap, dict):
            continue
        chain = snap.get("chain") or {}
        der = chain.get("DISCOVERY_EVIDENCE_RECORD") or {}
        if der.get("Source_URL") not in {None, "", "UNKNOWN"}:
            urls += 1
        if (snap.get("loss") or {}).get("lost_count"):
            loss += 1
    return {
        "kind": "M3EvidenceChainStatus",
        "researched": len(by_id),
        "source_urls_preserved": urls,
        "with_data_loss": loss,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.post("/api/m3/document-discovery/analyze")
def api_m3_document_discovery_analyze(body: dict | None = None):
    """Procurement document discovery adapters + attachment recovery engine."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_document_discovery import analyze_document_discovery_top

    payload = body or {}
    limit = max(1, min(50, int(payload.get("limit") or 25)))
    run_processing = bool(payload.get("run_processing", True))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_document_discovery_top(
        store,
        limit=limit,
        run_processing=run_processing,
        persist=True,
    )


@app.get("/api/m3/document-discovery/status")
def api_m3_document_discovery_status():
    from m3_document_discovery import load_discovery_index

    idx = load_discovery_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    statuses: dict[str, int] = {}
    families: dict[str, int] = {}
    for pkg in by_id.values():
        if not isinstance(pkg, dict):
            continue
        ddr = pkg.get("DOCUMENT_DISCOVERY_RESULT") or {}
        st = str(ddr.get("Access_status") or "UNKNOWN")
        statuses[st] = statuses.get(st, 0) + 1
        fam = str(ddr.get("Portal_family") or "OTHER")
        families[fam] = families.get(fam, 0) + 1
    return {
        "kind": "M3DocumentDiscoveryStatus",
        "researched": len(by_id),
        "access_status": statuses,
        "portal_families": families,
        "updated_at": idx.get("updated_at"),
        "NEXT_STATE": "PROCUREMENT_DOCUMENT_RECOVERY_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/document-discovery/queues")
def api_m3_document_discovery_queues(limit: int = Query(25, ge=1, le=100)):
    """VA-operable document recovery queues."""
    from m3_document_discovery import build_va_document_queues

    return build_va_document_queues(limit=limit)


@app.post("/api/m3/document-discovery/va/update")
def api_m3_document_discovery_va_update(body: dict | None = None):
    """VA document recovery actions — review/verify/attach/notes. No access bypass."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_document_discovery import apply_va_document_update

    payload = body or {}
    cid = str(payload.get("canonical_id") or "")
    if not cid:
        raise HTTPException(status_code=400, detail="canonical_id required")
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return apply_va_document_update(
        store,
        cid,
        action=str(payload.get("action") or ""),
        note=payload.get("note"),
        status=payload.get("status"),
        attached_document=payload.get("attached_document")
        if isinstance(payload.get("attached_document"), dict)
        else None,
    )


@app.post("/api/m3/document-intelligence/analyze")
def api_m3_document_intelligence_analyze(body: dict | None = None):
    """Extract structured commercial intelligence from recovered procurement documents."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_document_intelligence import analyze_document_intelligence_top

    payload = body or {}
    limit = max(1, min(50, int(payload.get("limit") or 25)))
    priority_only = bool(payload.get("priority_families_only", True))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_document_intelligence_top(
        store,
        limit=limit,
        persist=True,
        priority_families_only=priority_only,
    )


@app.get("/api/m3/document-intelligence/status")
def api_m3_document_intelligence_status():
    from m3_document_intelligence import load_intelligence_index, load_learning_index

    idx = load_intelligence_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    readiness: dict[str, int] = {}
    for snap in by_id.values():
        if not isinstance(snap, dict):
            continue
        st = str(((snap.get("summary") or {}).get("ECONOMICS_READINESS") or "UNKNOWN"))
        readiness[st] = readiness.get(st, 0) + 1
    learning = load_learning_index()
    return {
        "kind": "M3DocumentIntelligenceStatus",
        "researched": len(by_id),
        "economics_readiness": readiness,
        "learning_families": list((learning.get("by_portal_family") or {}).keys()),
        "updated_at": idx.get("updated_at"),
        "NEXT_STATE": "PROCUREMENT_DOCUMENT_INTELLIGENCE_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/document-intelligence/learning")
def api_m3_document_intelligence_learning():
    from m3_document_intelligence import load_learning_index

    return load_learning_index()


@app.post("/api/m3/document-intelligence/va/update")
def api_m3_document_intelligence_va_update(body: dict | None = None):
    """VA extraction review/correct/flag/notes — no deals, bids, outreach, or scoring."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_document_intelligence import apply_va_intelligence_update

    payload = body or {}
    cid = str(payload.get("canonical_id") or "")
    if not cid:
        raise HTTPException(status_code=400, detail="canonical_id required")
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return apply_va_intelligence_update(
        store,
        cid,
        action=str(payload.get("action") or ""),
        note=payload.get("note"),
        correction=payload.get("correction") if isinstance(payload.get("correction"), dict) else None,
        status=payload.get("status"),
    )


@app.post("/api/m3/product-identity/analyze")
def api_m3_product_identity_analyze(body: dict | None = None):
    """Resolve extracted descriptions into validated researchable product identities."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_product_identity_resolution import analyze_product_identity_resolution_top

    payload = body or {}
    limit = max(1, min(50, int(payload.get("limit") or 25)))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_product_identity_resolution_top(store, limit=limit, persist=True)


@app.get("/api/m3/product-identity/status")
def api_m3_product_identity_status():
    from m3_product_identity_resolution import load_resolution_index

    idx = load_resolution_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    types: dict[str, int] = {}
    ready: dict[str, int] = {}
    for snap in by_id.values():
        if not isinstance(snap, dict):
            continue
        summary = snap.get("summary") or {}
        itype = str(summary.get("primary_Identity_type") or "UNKNOWN")
        types[itype] = types.get(itype, 0) + 1
        sr = summary.get("SUPPLIER_READINESS") or {}
        for k, v in sr.items():
            ready[k] = ready.get(k, 0) + int(v or 0)
    return {
        "kind": "M3ProductIdentityResolutionStatus",
        "researched": len(by_id),
        "identity_types": types,
        "supplier_readiness": ready,
        "updated_at": idx.get("updated_at"),
        "NEXT_STATE": "PRODUCT_IDENTITY_RESOLUTION_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/product-identity/queues")
def api_m3_product_identity_queues(limit: int = Query(25, ge=1, le=100)):
    from m3_product_identity_resolution import build_va_identity_queues

    return build_va_identity_queues(limit=limit)


@app.get("/api/m3/product-identity/learning")
def api_m3_product_identity_learning():
    from m3_product_identity_resolution import load_learning_index

    return load_learning_index()


@app.post("/api/m3/product-identity/va/update")
def api_m3_product_identity_va_update(body: dict | None = None):
    """VA identity research/attach/validate/notes — no invent, substitute, bid, or scoring."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_product_identity_resolution import apply_va_identity_update

    payload = body or {}
    cid = str(payload.get("canonical_id") or "")
    if not cid:
        raise HTTPException(status_code=400, detail="canonical_id required")
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return apply_va_identity_update(
        store,
        cid,
        action=str(payload.get("action") or ""),
        note=payload.get("note"),
        evidence=payload.get("evidence") if isinstance(payload.get("evidence"), dict) else None,
        status=payload.get("status"),
        line_index=int(payload["line_index"]) if payload.get("line_index") is not None else None,
    )


@app.post("/api/m3/acquisition-targets/analyze")
def api_m3_acquisition_targets_analyze(body: dict | None = None):
    """Specification → commercial candidates → compliance → pricing → acquisition targets."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_acquisition_target_engine import analyze_acquisition_targets_top

    payload = body or {}
    limit = max(1, min(40, int(payload.get("limit") or 25)))
    paid_limit = max(0, min(5, int(payload.get("paid_limit") or 0)))
    allow_paid = bool(payload.get("allow_paid_web")) and paid_limit > 0
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_acquisition_targets_top(
        store,
        limit=limit,
        allow_paid_web=allow_paid,
        paid_limit=paid_limit,
        persist=True,
    )


@app.get("/api/m3/acquisition-targets/status")
def api_m3_acquisition_targets_status():
    from m3_acquisition_target_engine import load_acq_index

    idx = load_acq_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    return {
        "kind": "M3AcquisitionTargetStatus",
        "researched": len(by_id),
        "updated_at": idx.get("updated_at"),
        "NEXT_STATE": "SPECIFICATION_TO_ACQUISITION_ECONOMICS_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/acquisition-targets/queues")
def api_m3_acquisition_targets_queues(limit: int = Query(25, ge=1, le=100)):
    from m3_acquisition_target_engine import build_va_acquisition_queues

    return build_va_acquisition_queues(limit=limit)


@app.post("/api/m3/acquisition-targets/va/update")
def api_m3_acquisition_targets_va_update(body: dict | None = None):
    """VA public research / attach evidence / notes — no outreach, accounts, bids, or scoring."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_acquisition_target_engine import apply_va_acquisition_update

    payload = body or {}
    cid = str(payload.get("canonical_id") or "")
    if not cid:
        raise HTTPException(status_code=400, detail="canonical_id required")
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return apply_va_acquisition_update(
        store,
        cid,
        action=str(payload.get("action") or ""),
        note=payload.get("note"),
        evidence=payload.get("evidence") if isinstance(payload.get("evidence"), dict) else None,
        status=payload.get("status"),
    )


@app.post("/api/m3/commercial-matching/analyze")
def api_m3_commercial_matching_analyze(body: dict | None = None):
    """Specification → commercial product candidates → suppliers → pricing → economics handoff."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_commercial_product_matching import analyze_commercial_matching_top

    payload = body or {}
    limit = max(1, min(40, int(payload.get("limit") or 20)))
    paid_limit = max(0, min(8, int(payload.get("paid_limit") or 0)))
    allow_paid = bool(payload.get("allow_paid_web")) and paid_limit > 0
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_commercial_matching_top(
        store,
        limit=limit,
        allow_paid_web=allow_paid,
        paid_limit=paid_limit,
        persist=True,
    )


@app.get("/api/m3/commercial-matching/status")
def api_m3_commercial_matching_status():
    from m3_commercial_product_matching import load_match_index

    idx = load_match_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    return {
        "kind": "M3CommercialMatchingStatus",
        "researched": len(by_id),
        "updated_at": idx.get("updated_at"),
        "NEXT_STATE": "COMMERCIAL_PRODUCT_MATCHING_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/commercial-matching/queues")
def api_m3_commercial_matching_queues(limit: int = Query(25, ge=1, le=100)):
    from m3_commercial_product_matching import build_va_matching_queues

    return build_va_matching_queues(limit=limit)


@app.get("/api/m3/commercial-matching/learning")
def api_m3_commercial_matching_learning():
    from m3_commercial_product_matching import load_search_learning

    return load_search_learning()


@app.post("/api/m3/commercial-matching/va/update")
def api_m3_commercial_matching_va_update(body: dict | None = None):
    """VA research/attach pricing/sources/notes — no compliance approval, outreach, or bidding."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_commercial_product_matching import apply_va_matching_update

    payload = body or {}
    cid = str(payload.get("canonical_id") or "")
    if not cid:
        raise HTTPException(status_code=400, detail="canonical_id required")
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return apply_va_matching_update(
        store,
        cid,
        action=str(payload.get("action") or ""),
        note=payload.get("note"),
        evidence=payload.get("evidence") if isinstance(payload.get("evidence"), dict) else None,
        status=payload.get("status"),
    )


@app.post("/api/m3/public-pricing-evidence/analyze")
def api_m3_public_pricing_evidence_analyze(body: dict | None = None):
    """Retrieve public commercial + government pricing evidence for real opportunities."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_public_pricing_evidence import analyze_public_pricing_evidence_top

    payload = body or {}
    limit = max(1, min(20, int(payload.get("limit") or 8)))
    paid_limit = max(0, min(4, int(payload.get("paid_limit") or 0)))
    allow_paid = bool(payload.get("allow_paid_web")) and paid_limit > 0
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_public_pricing_evidence_top(
        store,
        limit=limit,
        allow_paid_web=allow_paid,
        paid_limit=paid_limit,
        persist=True,
    )


@app.get("/api/m3/public-pricing-evidence/status")
def api_m3_public_pricing_evidence_status():
    from m3_public_pricing_evidence import load_evidence_index

    idx = load_evidence_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    return {
        "kind": "M3PublicPricingEvidenceStatus",
        "researched": len(by_id),
        "updated_at": idx.get("updated_at"),
        "NEXT_STATE": "PUBLIC_PRICING_AND_REVENUE_EVIDENCE_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/public-pricing-evidence/queues")
def api_m3_public_pricing_evidence_queues(limit: int = Query(25, ge=1, le=100)):
    from m3_public_pricing_evidence import build_va_evidence_queues

    return build_va_evidence_queues(limit=limit)


@app.post("/api/m3/public-pricing-evidence/va/update")
def api_m3_public_pricing_evidence_va_update(body: dict | None = None):
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_public_pricing_evidence import apply_va_evidence_update

    payload = body or {}
    cid = str(payload.get("canonical_id") or "")
    if not cid:
        raise HTTPException(status_code=400, detail="canonical_id required")
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return apply_va_evidence_update(
        store,
        cid,
        action=str(payload.get("action") or ""),
        note=payload.get("note"),
        evidence=payload.get("evidence") if isinstance(payload.get("evidence"), dict) else None,
    )


@app.post("/api/m3/government-revenue/analyze")
def api_m3_government_revenue_analyze(body: dict | None = None):
    """Retrieve government award/bid-tab history and build revenue benchmarks."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_government_revenue_benchmark import analyze_government_revenue_top

    payload = body or {}
    limit = max(1, min(12, int(payload.get("limit") or 4)))
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_government_revenue_top(
        store,
        limit=limit,
        persist=True,
        max_lines_per_opp=max(1, min(20, int(payload.get("max_lines_per_opp") or 8))),
    )


@app.get("/api/m3/government-revenue/status")
def api_m3_government_revenue_status():
    from m3_government_revenue_benchmark import load_revenue_index

    idx = load_revenue_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    ready = sum(
        1
        for v in by_id.values()
        if isinstance(v, dict) and (v.get("summary") or {}).get("ECONOMICS_SCENARIO_READY")
    )
    return {
        "kind": "M3GovernmentRevenueStatus",
        "researched": len(by_id),
        "economics_scenario_ready": ready,
        "updated_at": idx.get("updated_at"),
        "NEXT_STATE": "GOVERNMENT_REVENUE_AND_DEAL_ECONOMICS_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/government-revenue/queues")
def api_m3_government_revenue_queues(limit: int = Query(25, ge=1, le=100)):
    from m3_government_revenue_benchmark import build_va_revenue_queues

    return build_va_revenue_queues(limit=limit)


@app.post("/api/m3/government-revenue/va/update")
def api_m3_government_revenue_va_update(body: dict | None = None):
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_government_revenue_benchmark import apply_va_revenue_update

    payload = body or {}
    cid = str(payload.get("canonical_id") or "")
    if not cid:
        raise HTTPException(status_code=400, detail="canonical_id required")
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return apply_va_revenue_update(
        store,
        cid,
        action=str(payload.get("action") or ""),
        note=payload.get("note"),
        evidence=payload.get("evidence") if isinstance(payload.get("evidence"), dict) else None,
    )


@app.post("/api/m3/seed-basket/analyze")
def api_m3_seed_basket_analyze(body: dict | None = None):
    """Multi-line seed BOM acquisition pricing + basket economics (narrow solicitation)."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_seed_basket_economics import analyze_seed_basket

    payload = body or {}
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_seed_basket(
        store,
        persist=True,
        max_species_research=max(1, min(30, int(payload.get("max_species_research") or 18))),
        max_pages_per_species=max(1, min(6, int(payload.get("max_pages_per_species") or 3))),
        second_pass_species=max(0, min(15, int(payload.get("second_pass_species") or 8))),
    )


@app.get("/api/m3/seed-basket/status")
def api_m3_seed_basket_status():
    from m3_seed_basket_economics import load_basket_index

    idx = load_basket_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    return {
        "kind": "M3SeedBasketStatus",
        "researched": len(by_id),
        "updated_at": idx.get("updated_at"),
        "NEXT_STATE": "MULTI_LINE_BASKET_ECONOMICS_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


@app.get("/api/m3/seed-basket/queues")
def api_m3_seed_basket_queues(limit: int = Query(25, ge=1, le=100)):
    from m3_seed_basket_economics import build_va_basket_queues

    return build_va_basket_queues(limit=limit)


@app.post("/api/m3/seed-basket/va/update")
def api_m3_seed_basket_va_update(body: dict | None = None):
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_seed_basket_economics import apply_va_basket_update

    payload = body or {}
    cid = str(payload.get("canonical_id") or "")
    if not cid:
        raise HTTPException(status_code=400, detail="canonical_id required")
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return apply_va_basket_update(
        store,
        cid,
        action=str(payload.get("action") or ""),
        note=payload.get("note"),
        evidence=payload.get("evidence") if isinstance(payload.get("evidence"), dict) else None,
    )


@app.post("/api/m3/material-acquisition-gaps/analyze")
def api_m3_material_acquisition_gaps_analyze(body: dict | None = None):
    """Close economically material seed BOM acquisition gaps; recalc basket."""
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_material_acquisition_gap import analyze_material_acquisition_gaps

    payload = body or {}
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    return analyze_material_acquisition_gaps(
        store,
        persist=True,
        max_gaps=max(1, min(15, int(payload.get("max_gaps") or 10))),
        max_pages_per_gap=max(1, min(10, int(payload.get("max_pages_per_gap") or 6))),
    )


@app.get("/api/m3/material-acquisition-gaps/status")
def api_m3_material_acquisition_gaps_status():
    from m3_material_acquisition_gap import load_gap_index

    idx = load_gap_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    return {
        "kind": "M3MaterialAcquisitionGapStatus",
        "researched": len(by_id),
        "updated_at": idx.get("updated_at"),
        "NEXT_STATE": ((next(iter(by_id.values()), {}) or {}).get("summary") or {}).get("NEXT_STATE")
        or "MATERIAL_GAPS_PARTIALLY_CLOSED",
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
    """Operator-facing matrix: R1 ResponseProject is canonical when present."""
    from response_engine.legacy_bridge import wrap_legacy_compliance_matrix

    analysis = _bid_compliance_for(opportunity_id)
    return wrap_legacy_compliance_matrix(opportunity_id, analysis.get("compliance_matrix") or {})


@app.get("/api/opportunities/{opportunity_id}/bid-readiness")
def api_bid_readiness(opportunity_id: str):
    """Operator-facing readiness: R1 wins; legacy READY cannot override."""
    from response_engine.legacy_bridge import wrap_legacy_bid_readiness

    analysis = _bid_compliance_for(opportunity_id)
    return wrap_legacy_bid_readiness(opportunity_id, analysis)


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


# ---------------------------------------------------------------------------
# M3 Owner / Operator UI — /api/ui/*
# Abstracts L.23 funnel + L.22 call desk into plain-language work queues.
# ---------------------------------------------------------------------------

class UiCallSaveBody(BaseModel):
    session_id: str
    answers: list[dict[str, Any]] = Field(default_factory=list)
    call_notes: str | None = None
    promised_quote_date: str | None = None
    complete: bool = False
    outcome: str | None = None


class UiQuoteBody(BaseModel):
    unit_price: float | None = None
    freight: float | None = None
    lead_time: str | None = None
    terms: str | None = None
    quote_expiration: str | None = None
    recommendation: str | None = None
    bucket: str | None = None
    supplier: str | None = None
    buyer: str | None = None
    product: str | None = None
    expected_revenue: float | None = None
    expected_profit: float | None = None
    margin: float | None = None
    max_buy: float | None = None
    financing: float | None = None


class UiRegisterBody(BaseModel):
    confirmation: str | None = None


class ResponseIngestBody(BaseModel):
    documents: list[dict[str, Any]] = Field(default_factory=list)
    buyer: str | None = None
    solicitation_number: str | None = None
    title: str | None = None
    jurisdiction: str | None = None
    discovery_source: str | None = None
    authoritative_source: str | None = None
    submission_system: str | None = None
    compile: bool = True


@app.post("/api/response-projects/from-opportunity/{opportunity_id}")
def api_response_project_from_opportunity(opportunity_id: str, body: ResponseIngestBody | None = None):
    """Create/reuse ResponseProject and run R1.1 production intake when possible."""
    from response_engine.production_intake import start_bid_prep_production
    from response_engine.service import (
        compile_project,
        create_or_get_project_from_opportunity,
        get_project_view,
        ingest_document_set,
    )

    payload = body or ResponseIngestBody()
    cid = opportunity_id[2:] if opportunity_id.startswith("c:") else opportunity_id

    # Prefer production intake (local artifacts / known package) — 0 SAM calls
    if not payload.documents:
        result = start_bid_prep_production(cid)
        if result.get("view"):
            return result["view"]
        from response_engine.store import find_by_opportunity

        proj = find_by_opportunity(cid)
        if proj:
            return get_project_view(proj["response_project_id"])
        raise HTTPException(status_code=500, detail="Bid Prep intake did not produce a project.")

    # Explicit document texts (tests / advanced) still supported
    buyer = payload.buyer
    title = payload.title
    solicitation = payload.solicitation_number
    jurisdiction = payload.jurisdiction
    discovery = payload.discovery_source
    auth = payload.authoritative_source
    submit = payload.submission_system
    try:
        from phase_l.l23_full_population_funnel import load_store

        rec = load_store().get(cid) or {}
        buyer = buyer or rec.get("buyer")
        title = title or rec.get("title")
        solicitation = solicitation or rec.get("solicitation_event_id")
        jurisdiction = jurisdiction or ("FEDERAL" if rec.get("is_federal") else rec.get("jurisdiction"))
        discovery = discovery or rec.get("platform")
        auth = auth or rec.get("authoritative_url") or rec.get("submission_path")
        submit = submit or rec.get("submission_path")
    except Exception:
        pass

    project = create_or_get_project_from_opportunity(
        canonical_opportunity_id=cid,
        buyer=buyer,
        solicitation_number=solicitation,
        title=title,
        jurisdiction=jurisdiction,
        discovery_source=discovery,
        authoritative_source=auth if isinstance(auth, str) else None,
        submission_system=submit if isinstance(submit, str) else None,
    )
    if payload.documents:
        ingest_document_set(project, payload.documents)
    if payload.compile or payload.documents:
        compile_project(project, persist=True)
    return get_project_view(project["response_project_id"])


@app.get("/api/response-projects/{response_project_id}")
def api_response_project_get(response_project_id: str):
    from response_engine.service import get_project_view

    view = get_project_view(response_project_id)
    if not view:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return view


@app.get("/api/response-projects/{response_project_id}/documents")
def api_response_project_documents(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    docs = [{k: v for k, v in d.items() if k != "text"} for d in project.get("documents") or []]
    return {
        "response_project_id": response_project_id,
        "documents": docs,
        "graph": project.get("document_graph"),
        "amendments": project.get("amendments"),
        "current_controlling_version": project.get("current_controlling_version"),
    }


@app.get("/api/response-projects/{response_project_id}/requirements")
def api_response_project_requirements(response_project_id: str, include_superseded: bool = False):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    reqs = project.get("requirements") or []
    if not include_superseded:
        reqs = [r for r in reqs if not r.get("superseded")]
    return {"response_project_id": response_project_id, "count": len(reqs), "requirements": reqs}


@app.get("/api/response-projects/{response_project_id}/compliance")
def api_response_project_compliance(response_project_id: str):
    from response_engine.compliance import operator_compliance_summary
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {
        "response_project_id": response_project_id,
        "matrix": project.get("compliance_matrix"),
        "operator_summary": operator_compliance_summary(project),
    }


@app.get("/api/response-projects/{response_project_id}/clarifications")
def api_response_project_clarifications(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {
        "response_project_id": response_project_id,
        "clarifications": project.get("clarifications") or [],
        "note": "R1 generates recommended questions only — no buyer contact automation.",
    }


@app.get("/api/response-projects/{response_project_id}/blockers")
def api_response_project_blockers(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {
        "response_project_id": response_project_id,
        "hard_blocks": project.get("hard_blocks") or [],
        "hard_block_count": project.get("hard_block_count") or 0,
        "unresolved_material_requirement_count": project.get("unresolved_material_requirement_count") or 0,
        "response_status": project.get("response_status"),
    }


@app.post("/api/response-projects/{response_project_id}/intake")
def api_response_project_intake(response_project_id: str, body: dict | None = None):
    from response_engine.production_intake import run_production_intake
    from response_engine.service import get_project_view
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    result = run_production_intake(
        project,
        local_paths=body.get("local_paths"),
        compile_after=bool(body.get("compile", True)),
        try_url_fetch=bool(body.get("try_url_fetch", False)),
    )
    return {"intake": result, "view": get_project_view(response_project_id)}


@app.post("/api/response-projects/{response_project_id}/refresh")
def api_response_project_refresh(response_project_id: str):
    from response_engine.production_intake import refresh_solicitation
    from response_engine.service import get_project_view
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    result = refresh_solicitation(project)
    return {"refresh": result, "view": get_project_view(response_project_id)}


@app.get("/api/response-projects/{response_project_id}/intake-status")
def api_response_project_intake_status(response_project_id: str):
    from response_engine.production_intake import intake_status

    return intake_status(response_project_id)


@app.get("/api/response-projects/{response_project_id}/document-graph")
def api_response_project_document_graph(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {
        "response_project_id": response_project_id,
        "graph": project.get("document_graph"),
        "amendments": project.get("amendments"),
        "package_completeness": project.get("package_completeness"),
        "current_controlling_version": project.get("current_controlling_version"),
    }


@app.post("/api/response-projects/{response_project_id}/documents/upload")
async def api_response_project_document_upload(
    response_project_id: str,
    file: UploadFile = File(...),
    mark_as_authoritative: bool = Form(False),
    document_type: str = Form(""),
):
    from response_engine.production_intake import manual_upload_document
    from response_engine.service import get_project_view
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(raw) > 40 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 40MB).")
    result = manual_upload_document(
        project,
        data=raw,
        filename=file.filename or "upload.bin",
        mark_as_authoritative=bool(mark_as_authoritative),
        document_type=document_type or None,
    )
    return {"upload": result, "view": get_project_view(response_project_id), "sam_api_calls": 0}


class OcrCorrectionBody(BaseModel):
    document_id: str
    page: int
    action: str = "CORRECT"  # CONFIRM | CORRECT | MARK_UNREADABLE
    corrected_text: str = ""
    notes: str | None = None
    user: str = "owner"
    recompile: bool = True


@app.get("/api/response-projects/{response_project_id}/ocr-review")
def api_response_project_ocr_review(response_project_id: str):
    from response_engine.ocr import build_ocr_review_queue
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    queue = build_ocr_review_queue(project)
    return {"response_project_id": response_project_id, "queue": queue, "sam_api_calls": 0}


@app.post("/api/response-projects/{response_project_id}/ocr-review")
def api_response_project_ocr_correct(response_project_id: str, body: OcrCorrectionBody):
    from response_engine.ocr import apply_ocr_human_correction, build_ocr_review_queue, gate_ocr_requirements
    from response_engine.service import compile_project, get_project_view
    from response_engine.store import load_project, save_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    result = apply_ocr_human_correction(
        project,
        document_id=body.document_id,
        page=body.page,
        corrected_text=body.corrected_text,
        user=body.user,
        notes=body.notes,
        action=body.action,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "OCR correction failed")
    if body.recompile and project.get("documents"):
        compile_project(project, persist=False)
        gate_ocr_requirements(project)
        build_ocr_review_queue(project)
        save_project(project)
    else:
        save_project(project)
    return {"correction": result, "view": get_project_view(response_project_id), "sam_api_calls": 0}


@app.get("/api/response-projects/{response_project_id}/amendment-diffs")
def api_response_project_amendment_diffs(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {
        "response_project_id": response_project_id,
        "amendment_diffs": project.get("amendment_diffs") or [],
        "amendment_review_queue": project.get("amendment_review_queue") or [],
        "sam_api_calls": 0,
    }


@app.post("/api/response-projects/{response_project_id}/amendment-diffs/acknowledge")
def api_response_project_amendment_ack(response_project_id: str, body: dict | None = None):
    from response_engine.service import compile_project, get_project_view
    from response_engine.store import load_project, save_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    amd_id = body.get("amendment_id")
    project["amendment_review_queue"] = [
        q for q in (project.get("amendment_review_queue") or []) if q.get("amendment_id") != amd_id
    ]
    if body.get("recompile", True) and project.get("documents"):
        compile_project(project, persist=True)
    else:
        save_project(project)
    return {"ok": True, "view": get_project_view(response_project_id), "sam_api_calls": 0}


# ---------------------------------------------------------------------------
# R2 — CLIN / pricing / technical compliance (canonical economics path)
# ---------------------------------------------------------------------------
@app.get("/api/response-projects/{response_project_id}/line-items")
def api_r2_line_items(response_project_id: str):
    from response_engine.r2_service import get_r2_view, run_r2_analysis
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    if not project.get("r2_analyzed_at"):
        run_r2_analysis(project)
    return {"line_items": project.get("line_items") or [], "sam_api_calls": 0}


@app.get("/api/response-projects/{response_project_id}/products")
def api_r2_products(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {"offered_products": project.get("offered_products") or [], "sam_api_calls": 0}


@app.post("/api/response-projects/{response_project_id}/products")
def api_r2_add_product(response_project_id: str, body: dict | None = None):
    from response_engine.product_offer import new_offered_product, select_offered_product
    from response_engine.r2_service import run_r2_analysis
    from response_engine.store import load_project, save_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    line_id = body.get("line_item_id")
    if not line_id:
        raise HTTPException(status_code=400, detail="line_item_id required")
    op = new_offered_product(
        response_project_id=response_project_id,
        line_item_id=line_id,
        manufacturer=body.get("manufacturer"),
        brand=body.get("brand"),
        model=body.get("model"),
        mpn=body.get("mpn") or body.get("MPN"),
        nsn=body.get("nsn"),
        description=body.get("description"),
        condition=body.get("condition") or "UNKNOWN",
        country_of_origin=body.get("country_of_origin"),
        warranty=body.get("warranty"),
        supplier=body.get("supplier"),
        product_mode=body.get("product_mode") or project.get("product_mode") or "UNKNOWN_PRODUCT_MODE",
        superseding_part=bool(body.get("superseding_part")),
        supersession_evidence_id=body.get("supersession_evidence_id"),
    )
    project.setdefault("offered_products", []).append(op)
    if body.get("select", True):
        select_offered_product(project, line_id, op["offered_product_id"], owner=True)
    save_project(project)
    run_r2_analysis(project)
    return {"ok": True, "offered_product": op, "sam_api_calls": 0}


@app.get("/api/response-projects/{response_project_id}/technical-compliance")
def api_r2_technical(response_project_id: str):
    from response_engine.r2_service import get_r2_view

    view = get_r2_view(response_project_id)
    if not view.get("ok"):
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {
        "items": view.get("technical_compliance") or [],
        "operator": (view.get("operator") or {}).get("technical"),
        "sam_api_calls": 0,
    }


@app.get("/api/response-projects/{response_project_id}/supplier-quotes")
def api_r2_quotes(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {"supplier_quotes": project.get("supplier_quotes") or [], "sam_api_calls": 0}


@app.post("/api/response-projects/{response_project_id}/supplier-quotes")
def api_r2_attach_quote(response_project_id: str, body: dict | None = None):
    from response_engine.r2_service import run_r2_analysis
    from response_engine.store import load_project
    from response_engine.supplier_evidence import attach_supplier_quote

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    result = attach_supplier_quote(project, body, line_item_id=body.get("line_item_id"))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "quote rejected")
    run_r2_analysis(project)
    return {**result, "sam_api_calls": 0}


@app.get("/api/response-projects/{response_project_id}/economics")
def api_r2_economics(response_project_id: str):
    from response_engine.r2_service import get_r2_view

    view = get_r2_view(response_project_id)
    if not view.get("ok"):
        raise HTTPException(status_code=404, detail="Response project not found.")
    op = view.get("operator") or {}
    return {
        "line_economics": view.get("line_economics") or [],
        "scenarios": view.get("pricing_scenarios") or [],
        "operator_economics": op.get("economics"),
        "financing": op.get("financing"),
        "sam_api_calls": 0,
    }


@app.post("/api/response-projects/{response_project_id}/pricing-scenarios")
def api_r2_pricing_scenarios(response_project_id: str, body: dict | None = None):
    from response_engine.r2_service import create_pricing_scenario
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    scenario = create_pricing_scenario(
        project,
        total_bid_price=body.get("total_bid_price"),
        target_profit=body.get("target_profit"),
        target_margin=body.get("target_margin"),
        scenario_type=body.get("scenario_type") or "OWNER_OR_ENGINE",
    )
    # Never return internal max-buy in a supplier-facing shape; strip for safety on this endpoint's public mirror
    public = {k: v for k, v in scenario.items() if k not in {"internal_max_buy", "internal_max_buy_namespace"}}
    return {"scenario": public, "internal_only_note": "max-buy retained on project under INTERNAL namespace", "sam_api_calls": 0}


@app.get("/api/response-projects/{response_project_id}/r2-readiness")
def api_r2_readiness(response_project_id: str):
    from response_engine.r2_service import get_r2_view

    view = get_r2_view(response_project_id)
    if not view.get("ok"):
        raise HTTPException(status_code=404, detail="Response project not found.")
    op = view.get("operator") or {}
    return {
        "readiness": view.get("readiness"),
        "recommendation": op.get("recommendation"),
        "next_action": op.get("next_action"),
        "blockers": op.get("blockers") or [],
        "never_ready_to_submit": True,
        "firewall": view.get("firewall"),
        "sam_api_calls": 0,
    }


# ---------------------------------------------------------------------------
# R3 — company compliance / NMR / trade / 889 / registrations / attestations
# ---------------------------------------------------------------------------
@app.get("/api/response-projects/{response_project_id}/r3")
def api_r3_view(response_project_id: str):
    from response_engine.r3_service import get_r3_view

    view = get_r3_view(response_project_id)
    if not view.get("ok"):
        raise HTTPException(status_code=404, detail="Response project not found.")
    return view


@app.post("/api/response-projects/{response_project_id}/r3/analyze")
def api_r3_analyze(response_project_id: str):
    from response_engine.r3_service import run_r3_analysis
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    analysis = run_r3_analysis(project, force=True)
    return {"ok": True, "analysis": analysis, "sam_api_calls": 0}


@app.get("/api/response-projects/{response_project_id}/r3/matrix")
def api_r3_matrix(response_project_id: str):
    from response_engine.r3_service import get_r3_view

    view = get_r3_view(response_project_id)
    if not view.get("ok"):
        raise HTTPException(status_code=404, detail="Response project not found.")
    a = view.get("analysis") or {}
    return {
        "matrix": a.get("compliance_matrix") or [],
        "readiness": a.get("readiness"),
        "next_action": a.get("next_action"),
        "disclaimer": a.get("legal_disclaimer"),
        "sam_api_calls": 0,
    }


@app.get("/api/response-projects/{response_project_id}/r3/attestations")
def api_r3_attestations(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {"attestations": project.get("owner_attestations") or [], "sam_api_calls": 0}


@app.post("/api/response-projects/{response_project_id}/r3/attestations/{attestation_id}/confirm")
def api_r3_confirm_attestation(response_project_id: str, attestation_id: str, body: dict | None = None):
    from response_engine.r3_service import confirm_owner_attestation_on_project
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    answer = str(body.get("answer") or "").upper()
    if answer not in ("YES", "NO", "NEED_REVIEW"):
        raise HTTPException(status_code=400, detail="answer must be YES, NO, or NEED_REVIEW")
    confirmed_by = body.get("confirmed_by") or "owner"
    result = confirm_owner_attestation_on_project(
        project,
        attestation_id=attestation_id,
        answer=answer,
        confirmed_by=confirmed_by,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "failed")
    return {**result, "sam_api_calls": 0}


@app.get("/api/company-compliance-profile")
def api_company_compliance_profile():
    """Owner Settings — Company Compliance (source-backed, no invent)."""
    from response_engine.company_profile_r3 import load_company_compliance_profile

    profile = load_company_compliance_profile()
    # Never expose unnecessary sensitive refs
    public = {k: v for k, v in profile.items() if k not in {"raw_eligibility_profile"}}
    return {"profile": public, "sam_api_calls": 0, "live_sam_api": False}


# ---------------------------------------------------------------------------
# R4 — buyer forms / spreadsheets / response document generation
# ---------------------------------------------------------------------------
@app.post("/api/response-projects/{response_project_id}/response-plan")
def api_r4_response_plan(response_project_id: str):
    from response_engine.response_plan import build_response_plan
    from response_engine.store import load_project, save_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    plan = build_response_plan(project)
    project["response_plan"] = plan
    save_project(project)
    return {"ok": True, "plan": plan, "sam_api_calls": 0}


@app.post("/api/response-projects/{response_project_id}/generate")
def api_r4_generate(response_project_id: str, body: dict | None = None):
    from response_engine.r4_service import run_r4_generation
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    package = run_r4_generation(project, force=True, created_by=body.get("created_by") or "operator")
    return {
        "ok": True,
        "package": package,
        "readiness": project.get("r4_readiness"),
        "handoff": project.get("submission_handoff"),
        "label": "DRAFT — NOT SUBMITTED",
        "never_ready_to_submit": True,
        "sam_api_calls": 0,
    }


@app.post("/api/response-projects/{response_project_id}/regenerate")
def api_r4_regenerate(response_project_id: str, body: dict | None = None):
    return api_r4_generate(response_project_id, body)


@app.get("/api/response-projects/{response_project_id}/generated-package")
def api_r4_package(response_project_id: str):
    from response_engine.r4_service import get_r4_view

    view = get_r4_view(response_project_id)
    if not view.get("ok"):
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {
        "package": view.get("package"),
        "operator": view.get("operator"),
        "label": "DRAFT — NOT SUBMITTED",
        "sam_api_calls": 0,
    }


@app.get("/api/response-projects/{response_project_id}/generated-documents")
def api_r4_documents(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    pkg = project.get("generated_package") or {}
    return {
        "documents": pkg.get("generated_documents") or [],
        "zip": pkg.get("zip"),
        "label": "DRAFT — NOT SUBMITTED",
        "sam_api_calls": 0,
    }


@app.get("/api/response-projects/{response_project_id}/portal-response-data")
def api_r4_portal_data(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    pkg = project.get("generated_package") or {}
    return {
        "portal_response_dataset": pkg.get("portal_response_dataset")
        or (project.get("submission_handoff") or {}).get("portal_response_dataset"),
        "submitted": False,
        "sam_api_calls": 0,
    }


@app.get("/api/response-projects/{response_project_id}/response-conflicts")
def api_r4_conflicts(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    pkg = project.get("generated_package") or {}
    return {"conflicts": pkg.get("conflicts") or [], "sam_api_calls": 0}


@app.get("/api/response-projects/{response_project_id}/r4-readiness")
def api_r4_readiness(response_project_id: str):
    from response_engine.r4_service import get_r4_view

    view = get_r4_view(response_project_id)
    if not view.get("ok"):
        raise HTTPException(status_code=404, detail="Response project not found.")
    op = view.get("operator") or {}
    return {
        "readiness": view.get("readiness") or op.get("readiness"),
        "package_status": op.get("package_status"),
        "next_action": op.get("next_action"),
        "blockers": op.get("blockers") or [],
        "handoff": view.get("handoff"),
        "never_ready_to_submit": True,
        "max_state": "READY_FOR_R5_PREFLIGHT",
        "sam_api_calls": 0,
    }


@app.post("/api/response-projects/{response_project_id}/select-price-scenario")
def api_r4_select_scenario(response_project_id: str, body: dict | None = None):
    from response_engine.r4_service import select_bid_price_scenario
    from response_engine.r5_service import invalidate_r5_on_change
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    sid = body.get("scenario_id")
    if not sid:
        raise HTTPException(status_code=400, detail="scenario_id required")
    result = select_bid_price_scenario(project, sid, approve_for_draft=body.get("approve_for_draft", True))
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "failed")
    invalidate_r5_on_change(project, reason="PRICE_CHANGE")
    from response_engine.store import save_project

    save_project(project)
    return {**result, "sam_api_calls": 0}


# ---------------------------------------------------------------------------
# R5 — preflight / owner approval / guided submission / receipt (DRY_RUN default)
# ---------------------------------------------------------------------------
@app.post("/api/response-projects/{response_project_id}/preflight")
def api_r5_preflight(response_project_id: str):
    from response_engine.r5_service import run_r5_preflight
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    result = run_r5_preflight(project)
    return {
        "ok": True,
        "preflight": result,
        "plain": result.get("next_action_plain"),
        "summary": f"{result.get('mandatory_passed', 0)}/{result.get('mandatory_total', 0)} mandatory checks passed",
        "sam_api_calls": 0,
        "external_side_effects": 0,
    }


@app.get("/api/response-projects/{response_project_id}/preflight")
def api_r5_preflight_get(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {"preflight": project.get("r5_preflight"), "sam_api_calls": 0}


@app.post("/api/response-projects/{response_project_id}/owner-approval")
def api_r5_owner_approval(response_project_id: str, body: dict | None = None):
    from response_engine.r5_service import r5_owner_approve
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    decision = body.get("decision") or body.get("approval_status")
    if not decision:
        raise HTTPException(status_code=400, detail="decision required (APPROVED|REJECTED|CHANGES_REQUESTED)")
    result = r5_owner_approve(
        project,
        decision=decision,
        approved_by=body.get("approved_by") or "owner",
        reason=body.get("reason"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "approval failed")
    return {**result, "sam_api_calls": 0, "external_side_effects": 0}


@app.get("/api/response-projects/{response_project_id}/signature-tasks")
def api_r5_signature_tasks(response_project_id: str):
    from response_engine.r5_signatures import ensure_signature_tasks
    from response_engine.store import load_project, save_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    tasks = ensure_signature_tasks(project)
    save_project(project)
    return {"tasks": tasks, "auto_signed": 0, "sam_api_calls": 0}


@app.post("/api/response-projects/{response_project_id}/signature-tasks/{task_id}/sign")
def api_r5_sign(response_project_id: str, task_id: str, body: dict | None = None):
    from response_engine.r5_service import r5_sign
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    result = r5_sign(project, task_id=task_id, signed_by=body.get("signed_by") or "owner")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "sign failed")
    return {**result, "sam_api_calls": 0, "external_side_effects": 0}


@app.get("/api/response-projects/{response_project_id}/submission-plan")
def api_r5_submission_plan(response_project_id: str):
    from response_engine.r5_service import r5_submission_plan
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    plan = r5_submission_plan(project, dry_run=True)
    return {"plan": plan, "label": "TEST SUBMISSION — NOT SENT" if plan.get("dry_run") else plan.get("label"), "sam_api_calls": 0, "external_side_effects": 0}


@app.post("/api/response-projects/{response_project_id}/freeze-submission")
def api_r5_freeze(response_project_id: str):
    from response_engine.r5_service import r5_freeze
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    result = r5_freeze(project)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "freeze failed")
    return {**result, "sam_api_calls": 0, "external_side_effects": 0}


@app.post("/api/response-projects/{response_project_id}/submission-events")
def api_r5_submission_event(response_project_id: str, body: dict | None = None):
    """Record submission attempt. dry_run=True by default — no live submit."""
    from response_engine.r5_service import r5_dry_run_submit
    from response_engine.store import load_project
    from response_engine.submission_audit import new_submission_event

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    if body.get("live_submit"):
        raise HTTPException(status_code=403, detail="Live external submission is disabled. Use dry_run / guided checklist + receipt.")
    if body.get("dry_run", True):
        return {**r5_dry_run_submit(project, submitted_by=body.get("submitted_by") or "operator"), "sam_api_calls": 0}
    # Non-dry-run event still does not contact portals — but must not mark SUBMITTED without
    # an approved+frozen package (prevents false "submitted" status pollution).
    approval = project.get("owner_submission_approval") or {}
    freeze = project.get("frozen_submission_package") or {}
    if approval.get("approval_status") != "APPROVED":
        raise HTTPException(status_code=400, detail="Owner approval required before recording a non-dry-run submission event.")
    if not freeze or freeze.get("invalidated"):
        raise HTTPException(status_code=400, detail="Freeze the approved package before recording a non-dry-run submission event.")
    if freeze.get("package_id") and approval.get("package_id") and freeze.get("package_id") != approval.get("package_id"):
        raise HTTPException(status_code=400, detail="Frozen package does not match owner-approved package.")
    event = new_submission_event(project, submitted_by=body.get("submitted_by") or "operator", dry_run=False)
    from response_engine.store import save_project

    save_project(project)
    return {"ok": True, "event": event, "status": "SUBMITTED_UNCONFIRMED", "note": "Capture receipt to confirm — no portal contact made", "sam_api_calls": 0, "external_side_effects": 0}


@app.post("/api/response-projects/{response_project_id}/receipt")
def api_r5_receipt(response_project_id: str, body: dict | None = None):
    from response_engine.r5_service import r5_record_receipt
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    dry_run = bool(body.get("dry_run", True))
    if not dry_run:
        approval = project.get("owner_submission_approval") or {}
        freeze = project.get("frozen_submission_package") or {}
        if approval.get("approval_status") != "APPROVED":
            raise HTTPException(status_code=400, detail="Owner approval required before confirming a live receipt.")
        if not freeze or freeze.get("invalidated"):
            raise HTTPException(status_code=400, detail="Frozen package required before confirming a live receipt.")
    receipt = r5_record_receipt(
        project,
        confirmation_number=body.get("confirmation_number"),
        receipt_path=body.get("receipt_path"),
        source=body.get("source") or "manual",
        dry_run=dry_run,
    )
    return {"ok": True, "receipt": receipt, "sam_api_calls": 0, "external_side_effects": 0}


@app.get("/api/response-projects/{response_project_id}/submission-audit")
def api_r5_audit(response_project_id: str):
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {
        "audits": project.get("submission_audits") or [],
        "latest_event": project.get("latest_submission_event"),
        "latest_receipt": project.get("latest_receipt"),
        "sam_api_calls": 0,
    }


@app.get("/api/response-projects/{response_project_id}/operator-state")
def api_r5_operator_state(response_project_id: str):
    from response_engine.operator_state_service import build_operator_state
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    return {"state": build_operator_state(project), "sam_api_calls": 0}


@app.post("/api/response-projects/{response_project_id}/validate-portal-price")
def api_r5_validate_price(response_project_id: str, body: dict | None = None):
    from response_engine.r5_service import validate_portal_price_entry
    from response_engine.store import load_project

    project = load_project(response_project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Response project not found.")
    body = body or {}
    result = validate_portal_price_entry(project, body.get("entered_price"))
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@app.get("/ops")
@app.get("/operator")
def owner_operator_console():
    """Primary operator entry — 15–30 minute training console."""
    return FileResponse(STATIC_DIR / "operator.html")


@app.get("/docs/{doc_name}")
def owner_docs(doc_name: str):
    """Serve operator training markdown from docs/."""
    allowed = {
        "M3_OPERATOR_QUICKSTART.md",
        "M3_OWNER_APPROVAL_QUICKSTART.md",
        "M3_OPERATOR_CHEATSHEET.md",
        "M3_OPERATOR_TRAINING_TEST.md",
        "CURRENT_M3_UI_ARCHITECTURE.md",
        "CURRENT_M3_ARCHITECTURE.md",
    }
    if doc_name not in allowed:
        raise HTTPException(status_code=404, detail="Document not found.")
    path = Path(__file__).resolve().parent / "docs" / doc_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found.")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


@app.get("/api/ui/home")
def api_ui_home():
    from phase_l.owner_ui_service import build_home

    return build_home()


@app.get("/api/ui/today")
def api_ui_today():
    from phase_l.owner_ui_service import build_today

    return build_today()


@app.get("/api/ui/deals")
def api_ui_deals(
    status: str | None = None,
    q: str | None = None,
    buyer: str | None = None,
    state: str | None = None,
    filter: str | None = Query("available"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    from phase_l.owner_ui_service import list_deals

    return list_deals(
        status=status,
        q=q,
        buyer=buyer,
        state=state,
        filter=filter,
        page=page,
        page_size=page_size,
    )


@app.get("/api/ui/deals/{deal_id:path}")
def api_ui_deal(deal_id: str):
    from phase_l.owner_ui_service import get_deal

    try:
        return get_deal(deal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Could not find that deal. It may have moved.") from None


@app.get("/api/ui/line-item-economics/{opportunity_id:path}")
def api_ui_line_item_economics_get(opportunity_id: str):
    from line_item_economics.engine import load_analysis, owner_summary

    oid = opportunity_id[2:] if opportunity_id.startswith("c:") else opportunity_id
    analysis = load_analysis(oid) or load_analysis(opportunity_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="No line-item economics analysis")
    return {
        "kind": "LineItemEconomicsView",
        "opportunity_id": oid,
        "owner_summary": owner_summary(analysis),
        "analysis": analysis,
    }


@app.post("/api/ui/line-item-economics/{opportunity_id:path}/analyze")
def api_ui_line_item_economics_analyze(opportunity_id: str, body: dict | None = None):
    """Analyze multi-line economics from schedule rows + attached price evidence."""
    from line_item_economics.engine import analyze_line_item_economics, owner_summary

    payload = body or {}
    oid = opportunity_id[2:] if opportunity_id.startswith("c:") else opportunity_id
    analysis = analyze_line_item_economics(
        opportunity_id=oid,
        title=payload.get("title"),
        buyer=payload.get("buyer"),
        schedule_rows=payload.get("schedule_rows") or payload.get("lines"),
        csv_text=payload.get("csv_text"),
        body_text=payload.get("body_text"),
        existing_lines=payload.get("existing_lines"),
        retail_by_line=payload.get("retail_by_line"),
        retail_equal_by_line=payload.get("retail_equal_by_line"),
        historical_by_line=payload.get("historical_by_line"),
        suppliers_by_line=payload.get("suppliers_by_line"),
        freight=payload.get("freight"),
        financing_cost=payload.get("financing_cost"),
        buyer_history=payload.get("buyer_history"),
        flags=payload.get("flags"),
        persist=payload.get("persist", True),
    )
    return {"ok": True, "owner_summary": owner_summary(analysis), "analysis": analysis}


@app.get("/api/ui/source-coverage-gaps")
def api_ui_source_coverage_gaps(limit: int = Query(100, ge=1, le=500)):
    from line_item_economics.coverage_gap import source_coverage_gap_report

    return source_coverage_gap_report(limit=limit)


@app.post("/api/ui/source-coverage-gaps")
def api_ui_source_coverage_gaps_record(body: dict | None = None):
    from line_item_economics.coverage_gap import record_source_coverage_gap

    payload = body or {}
    return record_source_coverage_gap(
        source_portal=str(payload.get("source_portal") or payload.get("source") or "unknown"),
        buyer=payload.get("buyer"),
        opportunity_id=payload.get("opportunity_id"),
        category=payload.get("category"),
        why_missed=str(payload.get("why_missed") or payload.get("why_m3_missed_it") or "unknown"),
        title=payload.get("title"),
        notes=payload.get("notes"),
    )


@app.get("/api/ui/profit-first/{opportunity_id:path}")
def api_ui_profit_first_get(opportunity_id: str):
    from line_item_economics.engine import load_analysis
    from phase_l.l23_full_population_funnel import load_store
    from profit_first.router import evaluate_opportunity_profit

    oid = opportunity_id[2:] if opportunity_id.startswith("c:") else opportunity_id
    store = load_store()
    rec = store.get(oid) or {}
    lie = load_analysis(oid)
    return evaluate_opportunity_profit(
        opportunity_id=oid,
        rec=rec,
        title=rec.get("title"),
        buyer=rec.get("buyer"),
        line_item_analysis=lie,
    )


@app.post("/api/ui/profit-first/evaluate")
def api_ui_profit_first_evaluate(body: dict | None = None):
    from profit_first.router import evaluate_opportunity_profit

    payload = body or {}
    return evaluate_opportunity_profit(
        opportunity_id=str(payload.get("opportunity_id") or "adhoc"),
        title=payload.get("title"),
        buyer=payload.get("buyer"),
        expected_revenue=payload.get("expected_revenue"),
        product_cost=payload.get("product_cost"),
        freight=payload.get("freight"),
        financing=payload.get("financing"),
        other_costs=payload.get("other_costs"),
        price_basis=payload.get("price_basis"),
        completeness_pct=payload.get("completeness_pct"),
        evidence_grade=payload.get("evidence_grade"),
        execution_pass=payload.get("execution_pass"),
        execution_blockers=payload.get("execution_blockers"),
        ranking_signals=payload.get("ranking_signals"),
        line_item_analysis=payload.get("line_item_analysis"),
        targets=payload.get("targets"),
    )


@app.get("/api/ui/profit-first-telemetry")
def api_ui_profit_first_telemetry():
    from profit_first.telemetry import profit_funnel_telemetry

    return profit_funnel_telemetry()


@app.post("/api/ui/profit-first/reevaluate")
def api_ui_profit_first_reevaluate(body: dict | None = None):
    from profit_first.reevaluate import reevaluate_canonical_population

    payload = body or {}
    return reevaluate_canonical_population(
        limit=payload.get("limit"),
        include_dead=bool(payload.get("include_dead")),
        persist=payload.get("persist", True),
        attach_to_store=bool(payload.get("attach_to_store")),
    )


@app.get("/api/ui/advanced/{deal_id:path}")
def api_ui_advanced(deal_id: str):
    from phase_l.owner_ui_service import get_advanced

    try:
        return get_advanced(deal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Could not find that deal.") from None


@app.get("/api/ui/calls")
def api_ui_calls():
    from phase_l.owner_ui_service import build_today

    today = build_today()
    return {
        "kind": "OwnerUiCalls",
        "items": (today.get("sections") or {}).get("call_today", {}).get("items") or [],
        "follow_up": (today.get("sections") or {}).get("follow_up", {}).get("items") or [],
        "empty": "No supplier calls need attention right now.",
    }


@app.get("/api/ui/calls/workspace")
def api_ui_call_workspace(deal_id: str = Query(...), supplier_id: str | None = None):
    from phase_l.owner_ui_service import build_call_workspace

    try:
        return build_call_workspace(deal_id, supplier_id=supplier_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Could not open call workspace for that deal.") from None
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Could not refresh this call sheet. Last known data retained.",
        ) from None


@app.post("/api/ui/calls/save")
def api_ui_call_save(body: UiCallSaveBody, deal_id: str = Query(...)):
    from phase_l.owner_ui_service import save_call_answers

    result = save_call_answers(
        deal_id,
        session_id=body.session_id,
        answers=body.answers,
        call_notes=body.call_notes,
        promised_quote_date=body.promised_quote_date,
        complete=body.complete,
        outcome=body.outcome,
    )
    if not result.get("ok") and result.get("still_needed"):
        return JSONResponse(status_code=200, content=result)
    if not result.get("ok"):
        return JSONResponse(status_code=502, content=result)
    return result


@app.get("/api/ui/quotes")
def api_ui_quotes():
    from phase_l.owner_ui_service import build_quotes

    return build_quotes()


@app.get("/api/ui/quotes/{deal_id:path}")
def api_ui_quote_review(deal_id: str):
    from phase_l.owner_ui_service import get_quote_review

    try:
        return get_quote_review(deal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Quote not found for that deal.") from None


@app.post("/api/ui/quotes/{deal_id:path}")
def api_ui_quote_save(deal_id: str, body: UiQuoteBody):
    from phase_l.owner_ui_service import upsert_quote_review

    return upsert_quote_review(deal_id, body.model_dump(exclude_none=True))


@app.get("/api/ui/registrations")
def api_ui_registrations():
    from phase_l.owner_ui_service import build_registrations

    return build_registrations()


@app.get("/api/ui/registrations/{portal_id}/opportunities")
def api_ui_registration_opportunities(portal_id: str):
    from phase_l.owner_ui_service import registration_opportunity_drilldown

    result = registration_opportunity_drilldown(portal_id)
    if not result.get("ok") and result.get("error") == "portal_not_found":
        raise HTTPException(status_code=404, detail="Registration unlock not found")
    return result


@app.get("/api/ui/opportunity-health")
def api_ui_opportunity_health():
    from phase_l.owner_ui_service import opportunity_data_health

    return opportunity_data_health()


@app.get("/api/ui/opportunity-diagnostics")
def api_ui_opportunity_diagnostics():
    from phase_l.owner_ui_service import opportunity_visibility_diagnostics

    return opportunity_visibility_diagnostics()


@app.get("/api/ui/discovery-health")
def api_ui_discovery_health():
    from m3_canonical_discovery_bridge import discovery_health_payload

    return discovery_health_payload()


@app.get("/api/ui/discovery-coverage")
def api_ui_discovery_coverage():
    """Owner Discovery Coverage — raw live universe vs product candidates."""
    from discovery_expansion import discovery_coverage_dashboard

    return discovery_coverage_dashboard()


@app.post("/api/ui/discovery-expansion/harvest")
def api_ui_discovery_expansion_harvest(body: dict | None = None):
    """Run free national expansion harvest (BidNet + platform families). SAM unused."""
    from discovery_expansion import run_expansion_harvest

    payload = body or {}
    return run_expansion_harvest(
        include_bidnet=bool(payload.get("include_bidnet", True)),
        include_structured=bool(payload.get("include_structured", True)),
        include_platform_catalog=bool(payload.get("include_platform_catalog", True)),
        max_pages=int(payload.get("max_pages") or 80),
        max_catalog_entities_per_family=int(payload.get("max_catalog_entities_per_family") or 40),
        persist=True,
    )


@app.get("/api/ui/universe-funnel")
def api_ui_universe_funnel():
    from universe_pass import universe_funnel_dashboard

    return universe_funnel_dashboard()


@app.post("/api/ui/universe-pass/run")
def api_ui_universe_pass_run(body: dict | None = None):
    from universe_pass import run_universe_pass

    payload = body or {}
    return run_universe_pass(
        classify=bool(payload.get("classify", True)),
        freshness=bool(payload.get("freshness", True)),
        profit_route=bool(payload.get("profit_route", True)),
        limit=payload.get("limit"),
        profit_limit=payload.get("profit_limit"),
        resume=bool(payload.get("resume", True)),
        persist=True,
        force_reclassify=bool(payload.get("force_reclassify", False)),
    )


@app.get("/api/ui/bidnet-recovery/funnel")
def api_ui_bidnet_recovery_funnel():
    from bidnet_recovery import bidnet_recovery_funnel

    return bidnet_recovery_funnel()


@app.get("/api/ui/bidnet-auth/status")
def api_ui_bidnet_auth_status():
    from bidnet_auth import owner_connection_status

    return owner_connection_status()


@app.post("/api/ui/bidnet-auth/test-connection")
def api_ui_bidnet_auth_test_connection():
    from bidnet_auth import test_connection

    return test_connection()


@app.get("/api/ui/opengov-auth/status")
def api_ui_opengov_auth_status():
    from opengov_auth import owner_connection_status

    return owner_connection_status()


@app.post("/api/ui/opengov-auth/test-connection")
def api_ui_opengov_auth_test_connection():
    from opengov_auth import test_connection

    return test_connection()


@app.get("/api/ui/euna-auth/status")
def api_ui_euna_auth_status():
    from euna_auth import owner_connection_status

    return owner_connection_status()


@app.post("/api/ui/euna-auth/test-connection")
def api_ui_euna_auth_test_connection():
    from m3_auth_jobs import start_euna_auth_diagnostic_job

    return start_euna_auth_diagnostic_job()


@app.post("/api/ui/euna-auth/diagnostic")
def api_ui_euna_auth_diagnostic(body: dict | None = None):
    from m3_auth_jobs import start_euna_auth_diagnostic_job

    return start_euna_auth_diagnostic_job()


@app.post("/api/ui/euna-discovery/run")
def api_ui_euna_discovery_run(body: dict | None = None):
    """Kick Euna central discovery as async job (UI convenience)."""
    from m3_auth_jobs import start_euna_discovery_job

    payload = body or {}
    return start_euna_discovery_job(
        max_results=int(payload.get("max_results") or 5000),
        max_pages=int(payload.get("max_pages") or 40),
        persist=True,
        mode=str(payload.get("mode") or "central"),
    )


@app.post("/api/ui/euna-recovery/run")
def api_ui_euna_recovery_run(body: dict | None = None):
    """Placeholder recovery kick — reuses central discovery with small detail-oriented batch."""
    from m3_auth_jobs import start_euna_discovery_job

    payload = body or {}
    return start_euna_discovery_job(
        max_results=int(payload.get("max_results") or 100),
        max_pages=int(payload.get("max_pages") or 10),
        persist=True,
        mode="central",
    )


@app.post("/api/ui/opengov-discovery/run")
def api_ui_opengov_discovery_run(body: dict | None = None):
    from m3_auth_jobs import start_opengov_discovery_job

    payload = body or {}
    raw_me = payload.get("max_entities")
    if raw_me in (None, "", "all", "ALL"):
        me = None
    else:
        me = int(raw_me)
    return start_opengov_discovery_job(
        max_entities=me,
        max_pages=int(payload.get("max_pages") or 8),
        persist=bool(payload.get("persist", True)),
        mode=str(payload.get("mode") or "cascade"),
        allow_browser=bool(payload.get("allow_browser", False)),
    )


@app.get("/api/ui/opengov-recovery/funnel")
def api_ui_opengov_recovery_funnel():
    from opengov_recovery import opengov_recovery_funnel

    return opengov_recovery_funnel()


@app.post("/api/ui/opengov-recovery/run")
def api_ui_opengov_recovery_run(body: dict | None = None):
    from opengov_recovery import run_opengov_recovery

    payload = body or {}
    use_auth = payload.get("use_auth")
    return run_opengov_recovery(
        limit=payload.get("limit"),
        resume=bool(payload.get("resume", True)),
        persist=True,
        force=bool(payload.get("force", False)),
        use_auth=None if use_auth is None else bool(use_auth),
    )


@app.post("/api/ui/bidnet-recovery/run")
def api_ui_bidnet_recovery_run(body: dict | None = None):
    from bidnet_recovery import run_bidnet_recovery

    payload = body or {}
    use_auth = payload.get("use_auth")
    return run_bidnet_recovery(
        limit=int(payload.get("limit") or 100),
        batch_size=int(payload.get("batch_size") or 25),
        resume=bool(payload.get("resume", True)),
        persist=True,
        force=bool(payload.get("force", False)),
        min_tier=int(payload.get("min_tier") or 3),
        use_auth=None if use_auth is None else bool(use_auth),
        recovery_batch_size=payload.get("recovery_batch_size"),
        backlog_batch_size=payload.get("backlog_batch_size"),
    )


@app.get("/api/ui/discovery-diagnostics")
def api_ui_discovery_diagnostics():
    from m3_canonical_discovery_bridge import discovery_diagnostics

    return discovery_diagnostics()


@app.get("/api/ui/discovery-history")
def api_ui_discovery_history():
    from m3_canonical_discovery_bridge import load_daily_history, load_run_history

    return {
        "kind": "DiscoveryHistory",
        "daily": load_daily_history(),
        "runs": load_run_history(limit=14),
    }


@app.post("/api/ui/discovery/run")
def api_ui_discovery_run(body: dict | None = None):
    """Owner RUN DISCOVERY NOW — same lock/budget as /api/m3/discovery/run."""
    from m3_discovery_service import TRIGGER_MANUAL, request_discovery_run

    payload = body or {}
    return request_discovery_run(
        trigger_type=TRIGGER_MANUAL,
        profile=payload.get("profile"),
        bootstrap=bool(payload.get("bootstrap")),
    )


@app.post("/api/ui/registrations/{portal_id}/mark")
def api_ui_registration_mark(portal_id: str, body: UiRegisterBody):
    from phase_l.owner_ui_service import mark_registered

    result = mark_registered(portal_id, confirmation=body.confirmation)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message") or "Not found")
    return result


@app.get("/api/ui/blocked")
def api_ui_blocked(page: int = Query(1, ge=1), page_size: int = Query(40, ge=1, le=100)):
    from phase_l.owner_ui_service import build_blocked

    return build_blocked(page=page, page_size=page_size)


@app.get("/api/ui/bid-prep")
def api_ui_bid_prep():
    from phase_l.owner_ui_service import build_bid_prep

    return build_bid_prep()


@app.get("/api/ui/watch")
def api_ui_watch(page: int = Query(1, ge=1), page_size: int = Query(40, ge=1, le=100)):
    from phase_l.owner_ui_service import build_watch

    return build_watch(page=page, page_size=page_size)


@app.get("/api/ui/search")
def api_ui_search(q: str = Query(""), limit: int = Query(30, ge=1, le=50)):
    from phase_l.owner_ui_service import global_search

    return global_search(q, limit=limit)


@app.get("/api/ui/settings")
def api_ui_settings():
    from phase_l.owner_ui_service import settings_payload

    return settings_payload()


@app.get("/api/ui/preservation")
def api_ui_preservation():
    from phase_l.owner_ui_service import preservation_snapshot

    return preservation_snapshot()


@app.get("/api/ui/financing")
def api_ui_financing(tab: str = "sources"):
    from financing_intelligence.service import ui_page

    return ui_page(tab=tab)


@app.get("/api/financing/dashboard")
def api_financing_dashboard():
    from financing_intelligence.service import dashboard

    return dashboard()


@app.get("/api/financing/sources")
def api_financing_sources():
    from financing_intelligence.sources import list_sources, source_display

    return {"sources": [source_display(s) for s in list_sources()], "sam_api_calls": 0}


@app.post("/api/financing/sources")
def api_financing_upsert_source(body: dict | None = None):
    from financing_intelligence.sources import upsert_source

    return {"ok": True, "source": upsert_source(body or {})}


@app.post("/api/financing/notes")
def api_financing_notes(body: dict | None = None):
    from financing_intelligence.notes_extract import ingest_call_notes

    body = body or {}
    return ingest_call_notes(
        raw_notes=str(body.get("raw_notes") or body.get("notes") or ""),
        source_id=body.get("source_id"),
        company_name=body.get("company_name") or body.get("source"),
        contact=body.get("contact"),
        call_date=body.get("call_date"),
        follow_up_date=body.get("follow_up_date"),
    )


@app.get("/api/financing/facts")
def api_financing_facts(status: str | None = None, source_id: str | None = None):
    from financing_intelligence.facts import list_facts

    return {"facts": list_facts(status=status, source_id=source_id)}


@app.post("/api/financing/facts/{fact_id}/decide")
def api_financing_decide_fact(fact_id: str, body: dict | None = None):
    from financing_intelligence.facts import decide_fact

    body = body or {}
    return decide_fact(
        fact_id,
        decision=str(body.get("decision") or ""),
        edited_value=body.get("edited_value"),
        decided_by=str(body.get("decided_by") or "operator"),
    )


@app.get("/api/financing/capital")
def api_financing_capital_get():
    from financing_intelligence.capital import capital_snapshot

    return capital_snapshot()


@app.post("/api/financing/capital")
def api_financing_capital_update(body: dict | None = None):
    from financing_intelligence.capital import capital_snapshot, update_capital

    update_capital(**{k: v for k, v in (body or {}).items() if k in {
        "business_cash",
        "unrestricted_additional_capital",
        "minimum_operating_reserve",
        "max_deploy_per_deal",
        "owner_contribution_allowed",
        "max_owner_contribution",
        "notes",
    }})
    return {"ok": True, "capital": capital_snapshot()}


@app.post("/api/financing/preferences")
def api_financing_prefs(body: dict | None = None):
    from financing_intelligence.store import load_owner_prefs, save_owner_prefs

    prefs = load_owner_prefs()
    prefs.update({k: v for k, v in (body or {}).items() if k in prefs or k.endswith("_allowed")})
    return {"ok": True, "preferences": save_owner_prefs(prefs)}


@app.post("/api/financing/opportunities/{opportunity_id}/assess")
def api_financing_assess(opportunity_id: str, body: dict | None = None):
    from financing_intelligence.assess import assess_opportunity_financing

    body = body or {}
    return assess_opportunity_financing(
        opportunity_id=opportunity_id,
        contract_value=body.get("contract_value") or body.get("estimated_value") or 0,
        supplier_cost=body.get("supplier_cost") or body.get("estimated_supplier_cost") or 0,
        freight=body.get("freight") or 0,
        other_prepay=body.get("other_prepay") or 0,
        verified_upfront_fees=body.get("verified_upfront_fees") or 0,
        supplier_terms=body.get("supplier_terms"),
        jurisdiction=str(body.get("jurisdiction") or "FEDERAL"),
        timing_dates=body.get("timing_dates") or body.get("financing_timing"),
        financed_days=body.get("financed_days") or body.get("expected_financed_days"),
        deal_type=str(body.get("deal_type") or "PRODUCT_RESALE"),
        persist=True,
    )


@app.post("/api/financing/opportunities/{opportunity_id}/capital-reserve")
def api_financing_propose_reserve(opportunity_id: str, body: dict | None = None):
    from financing_intelligence.capital import propose_reservation

    body = body or {}
    return {"ok": True, "reservation": propose_reservation(
        opportunity_id=opportunity_id,
        amount=body.get("amount") or 0,
        reason=str(body.get("reason") or "deal_capital_need"),
    )}


@app.post("/api/financing/capital-reservations/{reservation_id}/confirm")
def api_financing_confirm_reserve(reservation_id: str, body: dict | None = None):
    from financing_intelligence.capital import confirm_reservation

    body = body or {}
    return confirm_reservation(
        reservation_id=reservation_id,
        confirmed_by=str(body.get("confirmed_by") or "owner"),
        actual_available_balance_today=body.get("actual_available_balance_today"),
    )


@app.get("/api/financing/blocked-profit")
def api_financing_blocked_profit():
    from financing_intelligence.assess import blocked_profit_summary

    return blocked_profit_summary()


@app.get("/api/financing/outcomes")
def api_financing_outcomes(source_id: str | None = None, opportunity_id: str | None = None):
    from financing_intelligence.outcomes import list_outcomes

    return {"ok": True, "outcomes": list_outcomes(source_id=source_id, opportunity_id=opportunity_id)}


@app.post("/api/financing/outcomes")
def api_financing_record_outcome(body: dict | None = None):
    from financing_intelligence.outcomes import record_outcome

    return {"ok": True, "outcome": record_outcome(body or {})}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

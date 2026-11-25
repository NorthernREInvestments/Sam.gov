"""M3 2.0 Build 002 — FastAPI application entrypoint."""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from m3 import BUILD_ID
from m3.audit import record_audit
from m3.bootstrap import init_database
from m3.config import get_settings
from m3.db import get_db
from m3.invites import (
    accept_invitation,
    bump_session_version,
    create_invitation,
    detach_user_for_safe_delete,
    find_valid_invitation,
    is_protected_samantha,
    user_has_business_activity,
)
from m3.web.activity import enrich_activity_events, format_activity_time
from m3.models import (
    AuditEvent,
    CatalogProduct,
    CatalogUpload,
    Opportunity,
    PackageDocument,
    ResearchRun,
    ResearchSpendEvent,
    SearchProfile,
    SolicitationPackage,
    Supplier,
    Task,
    User,
    UserInvitation,
)
from m3.provenance import ACTIVE_OPP_STATUSES, build_cleanup_preview, classify_user_origin
from m3.research.budget import ensure_budget_policy, spending_snapshot, update_owner_policy
from m3.research.commercial import build_commercial_evidence
from m3.research.engine import (
    cancel_research_run,
    run_contract_discovery,
    run_historical_award_research,
    run_supplier_discovery,
)
from m3.security import hash_password, has_perm, is_primary_owner, issue_session_token
from m3.services import (
    authenticate,
    create_user,
    exact_mpn_matches,
    ingest_catalog,
    normalize_supplier_key,
    save_opportunity,
    save_supplier_from_discovery,
)
from m3.web.deps import current_user, require_user
from m3.web.display import enrich_discovery_item, unknown
from m3.web.labels import label
from m3.web.titles import build_display_title

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "m3" / "web" / "templates"))


def _b64json(value: object) -> str:
    # Drop nested display-only keys before persisting payload size
    if isinstance(value, dict):
        payload = {k: v for k, v in value.items() if k not in {"display", "saved_id"}}
    else:
        payload = value
    return base64.urlsafe_b64encode(json.dumps(payload, default=str).encode("utf-8")).decode("ascii")


TEMPLATES.env.filters["b64json"] = _b64json
TEMPLATES.env.filters["urlencode"] = lambda v: quote(str(v or ""), safe="")


def _opp_heading(o: Opportunity) -> str:
    if getattr(o, "display_title", None):
        return str(o.display_title)
    return build_display_title(
        official_title=o.title,
        buyer_name=o.buyer_name,
        solicitation_number=o.solicitation_number,
    )["display_title"]


TEMPLATES.env.filters["opp_heading"] = _opp_heading

app = FastAPI(title="M3 Supply", version="2.0.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "m3" / "web" / "static")), name="static")


def _active_opps(db: Session):
    """Genuine active saved/pursuing product-resale opportunities for dashboard counts."""
    return (
        db.query(Opportunity)
        .filter(Opportunity.status.in_(list(ACTIVE_OPP_STATUSES)))
        .filter(or_(Opportunity.data_origin.is_(None), Opportunity.data_origin != "acceptance_harness"))
        .filter(
            or_(
                Opportunity.requirement_class.is_(None),
                ~Opportunity.requirement_class.in_(["services", "construction"]),
            )
        )
    )


def _opp_item_from_model(o: Opportunity) -> dict:
    ev = o.raw_evidence or {}
    return {
        "external_key": o.external_key,
        "title": o.title,
        "display_title": o.display_title,
        "solicitation_number": o.solicitation_number,
        "buyer_name": o.buyer_name,
        "geo_state": o.geo_state,
        "geo_city": ev.get("geo_city"),
        "geo_location": o.geo_location,
        "deadline_at": o.deadline_at.isoformat() if o.deadline_at else None,
        "deadline_timezone": ev.get("deadline_timezone"),
        "procurement_type": o.procurement_type,
        "product_category": o.product_category,
        "set_aside": o.set_aside,
        "set_aside_verified": o.set_aside_verified,
        "estimated_value_min": o.estimated_value_min,
        "estimated_value_max": o.estimated_value_max,
        "estimated_value_unknown": o.estimated_value_unknown,
        "value_estimate_method": o.value_estimate_method,
        "value_evidence": ev.get("value_evidence"),
        "value_evidence_class": ev.get("value_evidence_class"),
        "official_url": o.official_url,
        "documents_available": o.documents_available,
        "quantity": o.quantity,
        "requirement_class": o.requirement_class,
        "classification_evidence": ev.get("classification_evidence"),
        "source_key": o.source_key,
        "source_verified_at": o.source_verified_at.isoformat() if o.source_verified_at else None,
        "evidence": ev,
        "field_status": ev.get("field_status") or {},
        "unavailable_reasons": ev.get("unavailable_reasons") or {},
        "saved_id": str(o.id),
        "status": o.status,
    }


def _find_package(db: Session, item: dict, *, opportunity_id=None) -> SolicitationPackage | None:
    from m3.research.package import _identity_key

    pkg = None
    if opportunity_id:
        pkg = (
            db.query(SolicitationPackage)
            .filter(SolicitationPackage.opportunity_id == opportunity_id)
            .order_by(SolicitationPackage.updated_at.desc())
            .first()
        )
    if not pkg:
        ek = item.get("external_key")
        if ek:
            pkg = (
                db.query(SolicitationPackage)
                .filter(SolicitationPackage.external_key == ek)
                .order_by(SolicitationPackage.updated_at.desc())
                .first()
            )
    if not pkg:
        pkg = db.query(SolicitationPackage).filter_by(identity_key=_identity_key(item)).one_or_none()
    return pkg


def _package_view(db: Session, item: dict, *, opportunity_id=None, action_base: str) -> dict:
    pkg = _find_package(db, item, opportunity_id=opportunity_id)
    docs = []
    if pkg:
        for d in pkg.documents or []:
            docs.append(
                {
                    "id": str(d.id),
                    "document_title": d.document_title,
                    "original_filename": d.original_filename,
                    "document_type": d.document_type,
                    "download_status": d.download_status,
                    "extraction_status": d.extraction_status,
                    "failure_reason": d.failure_reason,
                    "file_size": d.file_size,
                    "is_amendment": d.is_amendment,
                    "download_href": (
                        f"/packages/{pkg.id}/documents/{d.id}/file"
                        if d.download_status == "downloaded" and d.storage_path
                        else None
                    ),
                }
            )
    package_dict = None
    if pkg:
        package_dict = {
            "id": str(pkg.id),
            "package_status": pkg.package_status,
            "authoritative_url": pkg.authoritative_url,
            "url_kind": pkg.url_kind,
            "platform": pkg.platform,
            "identified_count": pkg.identified_count,
            "downloaded_count": pkg.downloaded_count,
            "missing_count": pkg.missing_count,
            "extracted_count": pkg.extracted_count,
            "extraction_status": pkg.extraction_status,
            "last_verified_at": pkg.last_verified_at.isoformat() if pkg.last_verified_at else None,
            "access_issues": list(pkg.access_issues or []),
            "amendment_summary": pkg.amendment_summary or {},
        }
    return {
        "package": package_dict,
        "package_documents": docs,
        "package_actions": {
            "acquire": f"{action_base}/package/acquire",
            "refresh": f"{action_base}/package/refresh",
            "retry": f"{action_base}/package/retry",
            "upload": f"{action_base}/package/upload",
            "zip": f"/packages/{pkg.id}/zip" if pkg and pkg.downloaded_count else None,
        },
    }


def _catalog_match_count(db: Session, item: dict) -> int:
    title = str(item.get("title") or "")
    # Look for obvious part-like tokens; exact MPN search only
    tokens = re.findall(r"\b[A-Z0-9][A-Z0-9\-]{3,}\b", title.upper())
    n = 0
    seen = set()
    for t in tokens[:8]:
        if t in seen:
            continue
        seen.add(t)
        n += len(exact_mpn_matches(db, t))
    return n


def _detail_display(item: dict) -> dict:
    enriched = enrich_discovery_item(item)
    d = enriched["display"]
    d["procurement_type"] = label(item.get("procurement_type")) if item.get("procurement_type") else "Unknown"
    d["source_key"] = unknown(item.get("source_key"))
    d["source_verified_at"] = unknown(item.get("source_verified_at"))
    evidence = item.get("classification_evidence") or (item.get("evidence") or {}).get("classification_evidence")
    if isinstance(evidence, list):
        d["classification_evidence"] = "; ".join(str(x) for x in evidence) if evidence else "Unknown"
    else:
        d["classification_evidence"] = unknown(evidence)
    d["value_evidence"] = unknown(
        item.get("value_evidence") or (item.get("evidence") or {}).get("value_evidence"),
        "Not Yet Researched",
    )
    d["value_method"] = label(item.get("value_estimate_method")) if item.get("value_estimate_method") else "Not Yet Researched"
    return d


def _identify_test_accounts(users: list[User]) -> list[dict]:
    out = []
    for u in users:
        c = classify_user_origin(u)
        if c:
            out.append({**c, "reason": ", ".join(c.get("reasons") or [])})
    return out


def _supplier_form_options() -> dict:
    return {
        "relationship_options": [(v, label(v)) for v in ["lead", "contacted", "active", "inactive", "blocked"]],
        "app_status_options": [(v, label(v)) for v in ["not_started", "in_progress", "submitted", "pending", "approved", "denied"]],
        "approval_options": [(v, label(v)) for v in ["not_started", "pending", "approved", "denied"]],
        "dealer_options": [(v, label(v)) for v in ["not_started", "pending", "approved", "denied", "n_a"]],
        "yn_options": [(v, label(v)) for v in ["unknown", "yes", "no"]],
        "address_types": [
            ("headquarters", "Headquarters"),
            ("sales_office", "Sales Office"),
            ("regional_office", "Regional Office"),
            ("distribution_location", "Distribution Location"),
        ],
    }


@app.on_event("startup")
def _startup() -> None:
    init_database()
    # Resume interrupted full-population acquisition jobs (Build 002E)
    try:
        from m3.db import SessionLocal
        from m3.research.population_acquire import resume_incomplete_jobs

        db = SessionLocal()
        try:
            resume_incomplete_jobs(db)
        finally:
            db.close()
    except Exception:
        pass
    # NextBid: engage killswitch status; NEVER auto-start under emergency stop
    try:
        from m3.research.nextbid_killswitch import engage_emergency_stop, emergency_stop_status
        from m3.research.nextbid_schedule import pause_scheduler, start_nextbid_scheduler

        settings = get_settings()
        if settings.nextbid_emergency_stop or not settings.nextbid_enabled:
            engage_emergency_stop(reason="startup_emergency_stop_or_disabled")
            pause_scheduler(reason="startup_emergency_stop_or_disabled")
        elif emergency_stop_status().get("metered_calls_allowed"):
            start_nextbid_scheduler()
        else:
            pause_scheduler(reason="killswitch_engaged")
    except Exception:
        pass


def _flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session_flash = {"message": message, "kind": kind}  # type: ignore[attr-defined]


def _ctx(request: Request, user: User | None, nav: str = "", **extra):
    flash = getattr(request, "session_flash", None)
    return {"request": request, "user": user, "nav": nav, "flash": flash, "build": BUILD_ID, **extra}


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True, "build": BUILD_ID}


@app.get("/api/m3/procurement/queues")
def procurement_queues_api(user: User | None = Depends(current_user)):
    """Owner queues for verified fits vs promising/missing/restricted (Build 002P).

    Unresolved opportunities are never returned under verified_procurement_fits.
    """
    if user is None:
        return JSONResponse({"ok": False, "error": "auth_required"}, status_code=401)
    from m3.config import get_settings

    root = get_settings().data_root() / "procurement_queues"
    index_path = root / "index.json"
    if not index_path.exists():
        return {"ok": True, "queues": {}, "note": "queues_not_built_yet"}
    index = json.loads(index_path.read_text(encoding="utf-8"))
    detail: dict = {}
    for qname in (index.get("queues") or {}):
        qp = root / f"{qname}.json"
        if qp.exists():
            payload = json.loads(qp.read_text(encoding="utf-8"))
            detail[qname] = {
                "count": payload.get("count"),
                "sample": (payload.get("rows") or [])[:25],
            }
    # Guard: never surface unresolved under verified / bid-ready keys
    for protected in (
        "verified_procurement_fits",
        "qualified_commercial_evaluation",
        "bids_ready_for_owner_approval",
        "verified_bid_opportunities",
        "awaiting_supplier_quotations",
    ):
        if protected in detail:
            rows = detail[protected].get("sample") or []
            detail[protected]["sample"] = [
                r for r in rows if not r.get("unresolved") and (r.get("disposition") or protected)
            ]
    return {
        "ok": True,
        "build": BUILD_ID,
        "index": index,
        "queues": detail,
        "verified_never_includes_unresolved": True,
        "stage_c_is_not_bid_eligible": True,
    }


@app.post("/api/m3/procurement/queues/publish")
async def procurement_queues_publish(
    request: Request,
    user: User | None = Depends(current_user),
):
    """Owner publish of supplier-pricing / needs-one-more queue rows (no metered calls)."""
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "expected_object"}, status_code=400)
    from m3.research.owner_queues import QUEUE_KEYS, empty_queues, queues_root, write_queues

    merged = empty_queues()
    root = queues_root()
    for qname in QUEUE_KEYS:
        qp = root / f"{qname}.json"
        if qp.exists():
            try:
                prior = json.loads(qp.read_text(encoding="utf-8"))
                merged[qname] = list(prior.get("rows") or [])
            except Exception:
                merged[qname] = []
    for qname, rows in (body.get("queues") or {}).items():
        if qname not in QUEUE_KEYS or not isinstance(rows, list):
            continue
        by_id = {
            str(r.get("nb_id") or r.get("source_record_id") or i): r
            for i, r in enumerate(merged.get(qname) or [])
            if isinstance(r, dict)
        }
        for r in rows:
            if not isinstance(r, dict):
                continue
            key = str(r.get("nb_id") or "")
            if key:
                by_id[key] = r
        merged[qname] = list(by_id.values())
    summary = write_queues(merged, build=str(body.get("build") or "live_pilot_publish"))
    return {"ok": True, "build": BUILD_ID, "queues": summary}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse("login.html", _ctx(request, None))


@app.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = authenticate(db, email, password)
    if not user:
        return TEMPLATES.TemplateResponse(
            "login.html",
            _ctx(request, None, flash={"message": "Invalid email or password", "kind": "err"}),
            status_code=401,
        )
    resp = RedirectResponse("/", status_code=303)
    settings = get_settings()
    # Secure cookies on Railway HTTPS; allow local http for development
    host = (request.headers.get("host") or "").lower()
    secure = not host.startswith("127.0.0.1") and not host.startswith("localhost")
    resp.set_cookie(
        settings.session_cookie,
        issue_session_token(str(user.id), getattr(user, "session_version", 1) or 1),
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=settings.session_max_age_seconds,
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(get_settings().session_cookie)
    return resp


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse("/", status_code=303)
    token = (request.query_params.get("token") or "").strip()
    invitation = find_valid_invitation(db, token) if token else None
    flash = None
    if token and not invitation:
        flash = {"message": "Invitation is invalid, expired, or already used.", "kind": "err"}
    return TEMPLATES.TemplateResponse(
        "signup.html",
        _ctx(
            request,
            None,
            invitation=invitation,
            token=token,
            role_label=label(invitation.role) if invitation else "",
            flash=flash,
        ),
    )


@app.post("/signup")
def signup_submit(
    request: Request,
    token: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if password != password_confirm or len(password) < 10:
        return TEMPLATES.TemplateResponse(
            "signup.html",
            _ctx(
                request,
                None,
                invitation=find_valid_invitation(db, token),
                token=token,
                role_label="",
                flash={"message": "Passwords must match and be at least 10 characters.", "kind": "err"},
            ),
            status_code=400,
        )
    created = accept_invitation(db, raw_token=token, full_name=full_name, password=password)
    if not created:
        return TEMPLATES.TemplateResponse(
            "signup.html",
            _ctx(
                request,
                None,
                invitation=None,
                token=token,
                flash={"message": "Could not accept invitation. It may be expired or the email already exists.", "kind": "err"},
            ),
            status_code=400,
        )
    resp = RedirectResponse("/", status_code=303)
    settings = get_settings()
    host = (request.headers.get("host") or "").lower()
    secure = not host.startswith("127.0.0.1") and not host.startswith("localhost")
    resp.set_cookie(
        settings.session_cookie,
        issue_session_token(str(created.id), created.session_version or 1),
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=settings.session_max_age_seconds,
    )
    return resp


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    tab = (request.query_params.get("tab") or "overview").lower()
    allowed = {"overview", "deadlines", "tasks", "followups", "applications", "activity"}
    if tab not in allowed:
        tab = "overview"
    if tab in {"followups", "applications"} and user.role not in {"owner", "supplier_manager"}:
        tab = "overview"
    if tab == "activity" and not (user.is_primary_owner or user.role == "owner"):
        tab = "overview"

    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=14)
    active = _active_opps(db)
    open_tasks = db.query(Task).filter(Task.assigned_to == user.id, Task.status == "open").count()
    # Only count follow-ups / apps that operators actually recorded
    followups = (
        db.query(Supplier)
        .filter(
            Supplier.follow_up_on != None,  # noqa: E711
            Supplier.follow_up_on <= date.today(),
            or_(Supplier.data_origin.is_(None), Supplier.data_origin != "acceptance_harness"),
        )
        .count()
    )
    saved_opps = active.count()
    supplier_count = (
        db.query(Supplier)
        .filter(or_(Supplier.data_origin.is_(None), Supplier.data_origin != "acceptance_harness"))
        .count()
    )
    deadlines = (
        active.filter(Opportunity.deadline_at != None, Opportunity.deadline_at >= now, Opportunity.deadline_at <= soon)  # noqa: E711
        .order_by(Opportunity.deadline_at.asc())
        .limit(20)
        .all()
    )
    tasks = (
        db.query(Task)
        .filter(Task.assigned_to == user.id, Task.status == "open")
        .order_by(Task.due_on.asc().nulls_last())
        .limit(20)
        .all()
    )
    overdue_suppliers = (
        db.query(Supplier)
        .filter(
            Supplier.follow_up_on != None,  # noqa: E711
            Supplier.follow_up_on <= date.today(),
            or_(Supplier.data_origin.is_(None), Supplier.data_origin != "acceptance_harness"),
        )
        .order_by(Supplier.follow_up_on.asc())
        .limit(20)
        .all()
    )
    app_suppliers = (
        db.query(Supplier)
        .filter(
            or_(
                Supplier.reseller_application_status.in_(["submitted", "pending", "in_progress"]),
                Supplier.credit_application_status.in_(["submitted", "pending", "in_progress"]),
            ),
            or_(Supplier.data_origin.is_(None), Supplier.data_origin != "acceptance_harness"),
        )
        .order_by(Supplier.updated_at.desc())
        .limit(20)
        .all()
    )
    recent_suppliers = (
        db.query(Supplier)
        .filter(or_(Supplier.data_origin.is_(None), Supplier.data_origin != "acceptance_harness"))
        .order_by(Supplier.created_at.desc())
        .limit(8)
        .all()
    )
    recent_opps = active.order_by(Opportunity.created_at.desc()).limit(12).all()
    recent_activity = []
    if user.is_primary_owner or user.role == "owner":
        raw_events = db.query(AuditEvent).order_by(AuditEvent.at.desc()).limit(20).all()
        actor_ids = {e.actor_user_id for e in raw_events if e.actor_user_id}
        users_by_id = {}
        if actor_ids:
            for u in db.query(User).filter(User.id.in_(list(actor_ids))).all():
                users_by_id[u.id] = u
        recent_activity = enrich_activity_events(raw_events, users_by_id)
    return TEMPLATES.TemplateResponse(
        "home.html",
        _ctx(
            request,
            user,
            "home",
            tab=tab,
            open_tasks=open_tasks,
            followups=followups,
            saved_opps=saved_opps,
            supplier_count=supplier_count,
            deadlines=deadlines,
            deadlines_count=len(deadlines),
            tasks=tasks,
            overdue_suppliers=overdue_suppliers,
            app_suppliers=app_suppliers,
            apps_in_progress=len(app_suppliers),
            recent_suppliers=recent_suppliers,
            recent_opps=recent_opps,
            recent_activity=recent_activity,
        ),
    )


# ---- Opportunities ----
def _load_working_contract_queues() -> dict[str, list]:
    """Owner working queues from durable procurement_queues + live JSONL overlays."""
    from m3.config import get_settings

    pricing: list[dict] = []
    needs: list[dict] = []
    root = get_settings().data_root() / "procurement_queues"
    for qname, dest in (
        ("awaiting_supplier_quotations", pricing),
        ("verified_bid_opportunities", pricing),
        ("verified_product_information_needed", needs),
        ("missing_document", needs),
        ("promising_awaiting_verification", needs),
    ):
        qp = root / f"{qname}.json"
        if not qp.exists():
            continue
        try:
            rows = json.loads(qp.read_text(encoding="utf-8")).get("rows") or []
        except Exception:
            continue
        for r in rows:
            if not isinstance(r, dict) or not r.get("nb_id"):
                continue
            if dest is pricing and r.get("unresolved"):
                continue
            dest.append(r)
    owner = get_settings().staged_inventory_root() / "owner"
    for name, dest, ready in (
        ("supplier_pricing_live.jsonl", pricing, True),
        ("needs_one_more_live.jsonl", needs, False),
    ):
        path = owner / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("nb_id"):
                row.setdefault("supplier_quote_ready", ready)
                dest.append(row)

    def _dedupe(rows: list[dict]) -> list[dict]:
        by: dict[str, dict] = {}
        for r in rows:
            by[str(r.get("nb_id"))] = r
        return list(by.values())

    pricing = _dedupe(pricing)
    needs = _dedupe([r for r in needs if str(r.get("nb_id")) not in {str(p.get("nb_id")) for p in pricing}])

    def _deadline_key(r: dict) -> str:
        return str(r.get("deadline_at") or "9999")

    pricing.sort(key=_deadline_key)
    needs.sort(key=_deadline_key)
    actions = sorted(pricing + needs, key=_deadline_key)
    return {"pricing": pricing, "needs": needs, "actions": actions}


@app.get("/contracts", response_class=HTMLResponse)
def contracts_working(request: Request, user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    if not has_perm(user.role, user.extra_permissions, "opportunities.read"):
        return RedirectResponse("/", status_code=303)
    tab = (request.query_params.get("tab") or "pricing").lower()
    if tab not in {"pricing", "needs", "deadlines"}:
        tab = "pricing"
    queues = _load_working_contract_queues()
    return TEMPLATES.TemplateResponse(
        "contracts_working.html",
        _ctx(
            request,
            user,
            "contracts",
            tab=tab,
            pricing=queues["pricing"],
            needs=queues["needs"],
            actions=queues["actions"],
        ),
    )


@app.get("/contracts/{nb_id}", response_class=HTMLResponse)
def contract_working_detail(nb_id: str, request: Request, user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    if not has_perm(user.role, user.extra_permissions, "opportunities.read"):
        return RedirectResponse("/", status_code=303)
    queues = _load_working_contract_queues()
    item = None
    for r in queues["pricing"] + queues["needs"]:
        if str(r.get("nb_id")) == str(nb_id):
            item = r
            break
    if item is None:
        item = {
            "nb_id": nb_id,
            "title": "UNKNOWN title",
            "open_status": "OPEN STATUS UNVERIFIED",
            "requirements_completeness": "UNKNOWN",
            "next_action": "Locate retained evidence or acquire missing requirements.",
        }
    return TEMPLATES.TemplateResponse(
        "contract_working_detail.html",
        _ctx(request, user, "contracts", item=item),
    )


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities_list(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    if not has_perm(user.role, user.extra_permissions, "opportunities.read"):
        return RedirectResponse("/", status_code=303)
    status_filter = (request.query_params.get("status") or "active").lower()
    q = db.query(Opportunity)
    if status_filter == "active":
        q = q.filter(Opportunity.status.in_(list(ACTIVE_OPP_STATUSES)))
    elif status_filter in {"saved", "pursuing", "archived", "not_interested"}:
        q = q.filter(Opportunity.status == status_filter)
    items = q.order_by(Opportunity.created_at.desc()).limit(100).all()
    rows = []
    for o in items:
        item = _opp_item_from_model(o)
        row = enrich_discovery_item(item, saved=True, catalog_matches=_catalog_match_count(db, item))
        row["saved_id"] = str(o.id)
        row["official_url"] = o.official_url
        row["status_label"] = label(o.status)
        rows.append(row)
    return TEMPLATES.TemplateResponse(
        "opportunities_list.html",
        _ctx(request, user, "opportunities", rows=rows, opp_tab="saved", status_filter=status_filter),
    )


@app.get("/opportunities/find", response_class=HTMLResponse)
def opportunities_find(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    if not has_perm(user.role, user.extra_permissions, "opportunities.search"):
        return RedirectResponse("/", status_code=303)

    qp = request.query_params
    profile_id = qp.get("profile")
    filters = {
        "keyword": qp.get("keyword") or "",
        "search_mode": qp.get("search_mode") or "broad",
        "exclude_keywords": qp.get("exclude_keywords") or "",
        "value_bands": qp.getlist("value_bands"),
        "custom_value_min": qp.get("custom_value_min") or None,
        "custom_value_max": qp.get("custom_value_max") or None,
        "include_unknown_value": qp.get("include_unknown_value") == "1" or not qp.getlist("value_bands"),
        "set_asides": qp.getlist("set_asides"),
        "states": [s.strip().upper() for s in (qp.get("states") or "").split(",") if s.strip()],
        "states_raw": qp.get("states") or "",
        "product_categories": [c.strip() for c in (qp.get("product_categories") or "").split(",") if c.strip()],
        "categories_raw": qp.get("product_categories") or "",
        "days_remaining_max": qp.get("days_remaining_max") or None,
        "product_resale_only": qp.get("product_resale_only", "1") == "1",
        "discover_sources": (qp.getlist("discover_sources") or ["1"])[-1],
    }
    if profile_id:
        prof = db.get(SearchProfile, profile_id)
        if prof and prof.owner_id == user.id:
            filters.update(prof.filters or {})
            filters["states_raw"] = ",".join(filters.get("states") or [])
            filters["categories_raw"] = ",".join(filters.get("product_categories") or [])
            filters["product_resale_only"] = bool(filters.get("product_resale_only", True))

    ran = any(
        k in qp
        for k in (
            "keyword",
            "value_bands",
            "states",
            "product_categories",
            "run",
            "profile",
            "set_asides",
            "continue_run",
        )
    )
    result = {
        "count": 0,
        "items": [],
        "sources_succeeded": [],
        "limitations": [],
        "unfiltered_count": 0,
        "run_id": None,
        "progress": {},
        "incomplete": False,
        "all_matching_count": 0,
        "estimated_cost_usd": 0,
    }
    if ran:
        for key in ("custom_value_min", "custom_value_max", "days_remaining_max"):
            if filters[key] in ("", None):
                filters[key] = None
            else:
                try:
                    filters[key] = float(filters[key]) if key != "days_remaining_max" else int(filters[key])
                except ValueError:
                    filters[key] = None
        continue_id = None
        if qp.get("continue_run"):
            try:
                continue_id = uuid.UUID(qp.get("continue_run") or "")
            except ValueError:
                continue_id = None
        try:
            page_offset = int(qp.get("offset") or 0)
        except ValueError:
            page_offset = 0
        page_size = get_settings().research_display_page_size
        result = run_contract_discovery(
            db,
            user_id=user.id,
            filters=filters,
            limit=page_size,
            offset=page_offset,
            continue_run_id=continue_id,
            # Build 002A: OpenAI source discovery on by default; opt out with discover_sources=0
            # Hidden+checkbox may yield ["0","1"] — use last value
            enable_openai_sources=(
                (qp.getlist("discover_sources") or ["1"])[-1] != "0"
            ),
        )
        if qp.get("save_profile"):
            name = f"Search {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
            db.add(SearchProfile(owner_id=user.id, name=name, kind="contracts", filters=filters))
            db.commit()

    saved_keys = {r[0] for r in db.query(Opportunity.external_key).all() if r[0]}
    rows = []
    for item in result.get("items") or []:
        matches = _catalog_match_count(db, item)
        row = enrich_discovery_item(item, saved=item.get("external_key") in saved_keys, catalog_matches=matches)
        if item.get("external_key") in saved_keys:
            opp = db.query(Opportunity).filter_by(external_key=item["external_key"]).one_or_none()
            if opp:
                row["saved_id"] = str(opp.id)
        rows.append(row)

    # Preserve filter context for return links / save redirects
    qdict = []
    for k, v in qp.multi_items():
        if k not in {"save_profile"}:
            qdict.append((k, v))
    if not any(k == "run" for k, _ in qdict) and ran:
        qdict.append(("run", "1"))
    return_to = "/opportunities/find"
    if qdict:
        return_to += "?" + urlencode(qdict)

    value_options = [
        ("under_5k", "Under $5,000"),
        ("5k_15k", "$5,000–$15,000"),
        ("15k_50k", "$15,000–$50,000"),
        ("50k_150k", "$50,000–$150,000"),
        ("150k_500k", "$150,000–$500,000"),
        ("500k_1m", "$500,000–$1M"),
        ("over_1m", "Over $1M"),
        ("unknown", "Unknown value"),
    ]
    set_aside_options = [
        ("total_small_business", "Total small business"),
        ("partial_small_business", "Partial small business"),
        ("8a", "8(a)"),
        ("hubzone", "HUBZone"),
        ("sdvosb", "SDVOSB"),
        ("vosb", "VOSB"),
        ("wosb", "WOSB"),
        ("edwosb", "EDWOSB"),
        ("unrestricted", "Unrestricted"),
        ("state_local_preference", "State/local preference"),
        ("other", "Other"),
        ("unknown", "Unknown"),
    ]
    flash = None
    if qp.get("saved") == "1":
        flash = {"message": "Opportunity saved. You are still in your search results.", "kind": "ok"}
    return TEMPLATES.TemplateResponse(
        "opportunities_find.html",
        _ctx(
            request,
            user,
            "opportunities",
            filters=filters,
            ran=ran,
            result=result,
            progress=result.get("progress") or {},
            rows=rows,
            run_id=result.get("run_id"),
            return_to=return_to,
            value_options=value_options,
            set_aside_options=set_aside_options,
            search_modes=[
                ("broad", "Broad product search"),
                ("exact_phrase", "Exact phrase"),
                ("exact_mpn", "Exact part number"),
            ],
            opp_tab="filter",
            flash=flash,
            spend=spending_snapshot(db, user_id=user.id) if is_primary_owner(user) else None,
        ),
    )


@app.get("/api/research/runs/{run_id}")
def api_research_run(run_id: uuid.UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return {"ok": False, "error": "login_required"}
    run = db.get(ResearchRun, run_id)
    if not run:
        return {"ok": False, "error": "not_found"}
    return {
        "ok": True,
        "run_id": str(run.id),
        "status": run.status,
        "result_count": run.result_count,
        "progress": run.progress or {},
        "estimated_cost_usd": run.estimated_cost_usd,
        "sources_succeeded": run.sources_succeeded or [],
        "limitations": (run.limitations or [])[:10],
        "incomplete": (run.progress or {}).get("incomplete"),
    }


@app.post("/api/research/runs/{run_id}/cancel")
def api_research_cancel(run_id: uuid.UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return {"ok": False}
    return {"ok": cancel_research_run(db, run_id)}


@app.post("/opportunities/save")
async def opportunities_save(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    raw = str(form.get("payload") or "")
    return_to = str(form.get("return_to") or "/opportunities/find")
    anchor = str(form.get("anchor") or "")
    try:
        item = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
    except Exception:
        return RedirectResponse(return_to or "/opportunities/find", status_code=303)
    save_opportunity(db, item, user)
    # Stay in filter/results context
    sep = "&" if "?" in return_to else "?"
    dest = f"{return_to}{sep}saved=1"
    if anchor:
        dest += f"#{anchor}"
    return RedirectResponse(dest, status_code=303)


@app.get("/opportunities/searches", response_class=HTMLResponse)
def opportunity_searches(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    profiles = db.query(SearchProfile).filter_by(owner_id=user.id, kind="contracts").order_by(SearchProfile.created_at.desc()).all()
    return TEMPLATES.TemplateResponse(
        "searches.html", _ctx(request, user, "opportunities", profiles=profiles, opp_tab="searches")
    )


@app.get("/opportunities/discover/{run_id}/{external_key}", response_class=HTMLResponse)
def opportunity_discover_detail(
    run_id: uuid.UUID,
    external_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return _login_redirect()
    run = db.get(ResearchRun, run_id)
    item = None
    if run and isinstance(run.result_snapshot, list):
        for it in run.result_snapshot:
            if isinstance(it, dict) and it.get("external_key") == external_key:
                item = it
                break
    if not item:
        return RedirectResponse("/opportunities/find", status_code=303)
    saved = db.query(Opportunity).filter_by(external_key=external_key).one_or_none()
    matches = []
    for t in re.findall(r"\b[A-Z0-9][A-Z0-9\-]{3,}\b", str(item.get("title") or "").upper())[:8]:
        matches.extend(exact_mpn_matches(db, t))
    return_to = request.query_params.get("return_to") or "/opportunities/find"
    hist = item.get("_historical_awards") or []
    commercial = _commercial_from_item(item, matches)
    action_base = f"/opportunities/discover/{run_id}/{external_key}"
    pkg_view = _package_view(db, item, opportunity_id=saved.id if saved else None, action_base=action_base)
    return TEMPLATES.TemplateResponse(
        "opportunity_detail.html",
        _ctx(
            request,
            user,
            "opportunities",
            display=_detail_display(item),
            official_url=item.get("official_url"),
            saved_id=str(saved.id) if saved else None,
            payload=_b64json(item) if not saved else None,
            notes=saved.notes if saved else "",
            assigned_label="Unassigned",
            assigned_id=None,
            saved_by_label="Not saved",
            status_label="Discovered",
            can_write=has_perm(user.role, user.extra_permissions, "opportunities.write"),
            assignable_users=[],
            catalog_matches=matches[:20],
            historical_awards=hist,
            commercial=commercial,
            research_target=f"/opportunities/discover/{run_id}/{external_key}/research",
            return_to=return_to,
            opp_tab="filter",
            **pkg_view,
        ),
    )


def _commercial_from_item(item: dict, matches: list) -> dict:
    qty = None
    try:
        raw_q = item.get("quantity")
        if raw_q not in (None, ""):
            qty = float(re.sub(r"[^\d.]", "", str(raw_q).split()[0]))
    except Exception:
        qty = None
    cat = matches[0] if matches else None
    if isinstance(cat, dict):
        cat_price = cat.get("published_price")
        cat_supplier = str(cat.get("supplier_id") or "")
        cat_date = str(cat.get("price_effective_date") or "")
    else:
        cat_price = getattr(cat, "published_price", None) if cat else None
        cat_supplier = str(getattr(cat, "supplier_id", "") or "") if cat else None
        cat_date = str(getattr(cat, "price_effective_date", "") or "") if cat else None
    hist = (item.get("_historical_awards") or [None])[0]
    hist_price = None
    hist_match = None
    if isinstance(hist, dict):
        try:
            hist_price = float(re.sub(r"[^\d.]", "", str(hist.get("unit_price") or ""))) if hist.get("unit_price") else None
        except Exception:
            hist_price = None
        hist_match = hist.get("match_type")
    return build_commercial_evidence(
        quantity=qty,
        historical_unit_price=hist_price,
        historical_match_type=hist_match,
        catalog_unit_price=cat_price,
        catalog_supplier=cat_supplier,
        catalog_effective_date=cat_date,
    )


@app.post("/opportunities/discover/{run_id}/{external_key}/research")
def opportunity_discover_research(
    run_id: uuid.UUID,
    external_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return _login_redirect()
    run = db.get(ResearchRun, run_id)
    item = None
    idx = None
    if run and isinstance(run.result_snapshot, list):
        for i, it in enumerate(run.result_snapshot):
            if isinstance(it, dict) and it.get("external_key") == external_key:
                item = it
                idx = i
                break
    if not item:
        return RedirectResponse("/opportunities/find", status_code=303)
    q = f"{item.get('title') or ''} {item.get('buyer_name') or ''} {item.get('solicitation_number') or ''}".strip()
    hist = run_historical_award_research(
        db,
        user_id=user.id,
        query=q,
        product=item.get("title"),
        buyer=item.get("buyer_name"),
        part_number=item.get("part_number"),
    )
    item["_historical_awards"] = hist.get("awards") or []
    item["_historical_research_cost"] = hist.get("estimated_cost_usd")
    if idx is not None and run:
        snap = list(run.result_snapshot or [])
        snap[idx] = item
        run.result_snapshot = snap
        db.add(run)
        db.commit()
    return_to = request.query_params.get("return_to") or f"/opportunities/discover/{run_id}/{external_key}"
    return RedirectResponse(return_to, status_code=303)


@app.get("/opportunities/{opportunity_id}", response_class=HTMLResponse)
def opportunity_detail(
    opportunity_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return _login_redirect()
    opp = db.get(Opportunity, opportunity_id)
    if not opp:
        return RedirectResponse("/opportunities", status_code=303)
    item = _opp_item_from_model(opp)
    matches = []
    for t in re.findall(r"\b[A-Z0-9][A-Z0-9\-]{3,}\b", (opp.title or "").upper())[:8]:
        matches.extend(exact_mpn_matches(db, t))
    assignee = db.get(User, opp.assigned_to) if opp.assigned_to else None
    saver = db.get(User, opp.saved_by) if opp.saved_by else None
    return_to = request.query_params.get("return_to") or "/opportunities"
    assignable = []
    if has_perm(user.role, user.extra_permissions, "opportunities.write"):
        assignable = db.query(User).filter(User.is_active.is_(True)).order_by(User.full_name.asc()).all()
    commercial = _commercial_from_item(item, matches)
    pkg_view = _package_view(db, item, opportunity_id=opp.id, action_base=f"/opportunities/{opp.id}")
    return TEMPLATES.TemplateResponse(
        "opportunity_detail.html",
        _ctx(
            request,
            user,
            "opportunities",
            display=_detail_display(item),
            official_url=opp.official_url,
            saved_id=str(opp.id),
            payload=None,
            notes=opp.notes or "",
            assigned_label=assignee.full_name if assignee else "Unassigned",
            assigned_id=opp.assigned_to,
            saved_by_label=(f"{saver.full_name} ({opp.created_at})" if saver else "Unknown"),
            status_label=label(opp.status),
            can_write=has_perm(user.role, user.extra_permissions, "opportunities.write"),
            assignable_users=assignable,
            catalog_matches=matches[:20],
            historical_awards=item.get("_historical_awards") or [],
            commercial=commercial,
            research_target=f"/opportunities/{opp.id}/research-awards",
            return_to=return_to,
            opp_tab="saved",
            **pkg_view,
        ),
    )


@app.post("/opportunities/{opportunity_id}/research-awards")
def opportunity_research_awards(
    opportunity_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return _login_redirect()
    opp = db.get(Opportunity, opportunity_id)
    if not opp:
        return RedirectResponse("/opportunities", status_code=303)
    hist = run_historical_award_research(
        db,
        user_id=user.id,
        query=f"{opp.title} {opp.buyer_name or ''} {opp.solicitation_number or ''}",
        product=opp.title,
        buyer=opp.buyer_name,
    )
    # Store on opportunity notes meta via audit only — do not invent CRM fields
    record_audit(
        db,
        action="research.historical_awards",
        entity_type="opportunity",
        entity_id=str(opp.id),
        actor_user_id=user.id,
        actor_label=user.email,
        after={"count": len(hist.get("awards") or []), "estimated_cost_usd": hist.get("estimated_cost_usd")},
        research_run_id=uuid.UUID(hist["run_id"]) if hist.get("run_id") else None,
    )
    db.commit()
    return RedirectResponse(f"/opportunities/{opportunity_id}?awards_run={hist.get('run_id')}", status_code=303)


def _run_package_acquire(db: Session, item: dict, *, opportunity_id=None, research_run_id=None, refresh: bool = False, uploaded=None):
    from m3.research.package import acquire_solicitation_package

    return acquire_solicitation_package(
        db,
        item,
        opportunity_id=opportunity_id,
        research_run_id=research_run_id,
        refresh=refresh,
        uploaded_files=uploaded,
    )


@app.post("/opportunities/{opportunity_id}/package/acquire")
@app.post("/opportunities/{opportunity_id}/package/refresh")
@app.post("/opportunities/{opportunity_id}/package/retry")
async def opportunity_package_acquire(
    opportunity_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return _login_redirect()
    opp = db.get(Opportunity, opportunity_id)
    if not opp:
        return RedirectResponse("/opportunities", status_code=303)
    item = _opp_item_from_model(opp)
    refresh = request.url.path.endswith("/refresh") or request.url.path.endswith("/retry")
    _run_package_acquire(db, item, opportunity_id=opp.id, refresh=refresh)
    return RedirectResponse(f"/opportunities/{opportunity_id}", status_code=303)


@app.post("/opportunities/{opportunity_id}/package/upload")
async def opportunity_package_upload(
    opportunity_id: uuid.UUID,
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return _login_redirect()
    opp = db.get(Opportunity, opportunity_id)
    if not opp:
        return RedirectResponse("/opportunities", status_code=303)
    uploaded = []
    for f in files or []:
        data = await f.read()
        if data:
            uploaded.append({"filename": f.filename or "upload.bin", "data": data, "content_type": f.content_type or ""})
    item = _opp_item_from_model(opp)
    _run_package_acquire(db, item, opportunity_id=opp.id, uploaded=uploaded)
    return RedirectResponse(f"/opportunities/{opportunity_id}", status_code=303)


@app.post("/opportunities/discover/{run_id}/{external_key}/package/acquire")
@app.post("/opportunities/discover/{run_id}/{external_key}/package/refresh")
@app.post("/opportunities/discover/{run_id}/{external_key}/package/retry")
async def discover_package_acquire(
    run_id: uuid.UUID,
    external_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return _login_redirect()
    run = db.get(ResearchRun, run_id)
    item = None
    if run and isinstance(run.result_snapshot, list):
        for it in run.result_snapshot:
            if isinstance(it, dict) and it.get("external_key") == external_key:
                item = it
                break
    if not item:
        return RedirectResponse("/opportunities/find", status_code=303)
    saved = db.query(Opportunity).filter_by(external_key=external_key).one_or_none()
    refresh = request.url.path.endswith("/refresh") or request.url.path.endswith("/retry")
    _run_package_acquire(
        db, item, opportunity_id=saved.id if saved else None, research_run_id=run_id, refresh=refresh
    )
    return RedirectResponse(f"/opportunities/discover/{run_id}/{external_key}", status_code=303)


@app.post("/opportunities/discover/{run_id}/{external_key}/package/upload")
async def discover_package_upload(
    run_id: uuid.UUID,
    external_key: str,
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return _login_redirect()
    run = db.get(ResearchRun, run_id)
    item = None
    if run and isinstance(run.result_snapshot, list):
        for it in run.result_snapshot:
            if isinstance(it, dict) and it.get("external_key") == external_key:
                item = it
                break
    if not item:
        return RedirectResponse("/opportunities/find", status_code=303)
    uploaded = []
    for f in files or []:
        data = await f.read()
        if data:
            uploaded.append({"filename": f.filename or "upload.bin", "data": data, "content_type": f.content_type or ""})
    saved = db.query(Opportunity).filter_by(external_key=external_key).one_or_none()
    _run_package_acquire(
        db, item, opportunity_id=saved.id if saved else None, research_run_id=run_id, uploaded=uploaded
    )
    return RedirectResponse(f"/opportunities/discover/{run_id}/{external_key}", status_code=303)


@app.get("/packages/{package_id}/documents/{document_id}/file")
def package_document_file(
    package_id: uuid.UUID,
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return _login_redirect()
    doc = db.get(PackageDocument, document_id)
    if not doc or doc.package_id != package_id or not doc.storage_path:
        return RedirectResponse("/", status_code=303)
    path = Path(doc.storage_path)
    if not path.exists():
        return RedirectResponse("/", status_code=303)
    return FileResponse(path, filename=doc.original_filename or path.name)


@app.get("/packages/{package_id}/zip")
def package_zip_download(
    package_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return _login_redirect()
    pkg = db.get(SolicitationPackage, package_id)
    if not pkg:
        return RedirectResponse("/", status_code=303)
    import io
    import zipfile

    from fastapi.responses import Response

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for d in pkg.documents or []:
            if d.download_status != "downloaded" or not d.storage_path:
                continue
            p = Path(d.storage_path)
            if p.exists():
                zf.write(p, arcname=d.original_filename or p.name)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="package_{package_id}.zip"'},
    )


@app.get("/api/m3/nextbid/status")
def api_nextbid_status(
    user: User | None = Depends(current_user),
    live_usage: int = 0,
):
    """Controller + killswitch status. Never returns the API key. No metered calls.

    Does not call NextBid on startup/health. Optional free /v1/usage when live_usage=1.
    """
    if not user:
        return {"ok": False, "error": "auth_required"}
    settings = get_settings()
    from m3.research.nextbid_controller import controller_status, refresh_account_usage
    from m3.research.nextbid_schedule import scheduler_status

    ctrl = controller_status()
    usage_probe = None
    if int(live_usage or 0) == 1:
        try:
            usage_probe = refresh_account_usage(allow_when_locked=True)
        except Exception as exc:
            usage_probe = {"ok": False, "error": type(exc).__name__}
    return {
        "ok": True,
        "build": BUILD_ID,
        "enabled": bool(settings.nextbid_enabled),
        "controller": ctrl,
        "acquisition_locked": ctrl.get("locked"),
        "key_configured": bool((settings.nextbid_api_key or "").strip()),
        "scheduler": scheduler_status(),
        "usage_free_probe": usage_probe,
        "usage_note": "Pass live_usage=1 for free /v1/usage probe only; never metered from this endpoint.",
        "budgets": {
            "discovery": settings.nextbid_budget_discovery,
            "detail": settings.nextbid_budget_detail,
            "documents": settings.nextbid_budget_documents,
            "reserve": settings.nextbid_budget_reserve,
            "daily_limit": settings.nextbid_daily_call_limit,
            "job_limit": settings.nextbid_job_call_limit,
            "hard_stop_remaining": settings.nextbid_hard_stop_remaining,
            "page_size": settings.nextbid_page_size,
        },
        "resume_requires_owner_authorization": True,
        "do_not_begin_build_003": True,
    }


@app.post("/api/m3/nextbid/live-pilot-10")
def api_nextbid_live_pilot_10(user: User | None = Depends(current_user)):
    """Owner single-use 10-credit live pilot (Lane A≤6, Lane B≤4). Always restores killswitch.

    Does not enable recurring schedules. Authorization is consumed inside the pilot runner.
    """
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return {"ok": False, "error": "forbidden"}
    from m3.research.nextbid_controller import acquisition_locked
    from m3.research.nextbid_live_pilot import run_live_pilot

    result = run_live_pilot()
    return {
        "ok": bool(result.get("ok")),
        "build": BUILD_ID,
        "credits_consumed_account": result.get("credits_consumed_account"),
        "lane_a_credits": result.get("lane_a_credits"),
        "lane_b_credits": result.get("lane_b_credits"),
        "usage_before": result.get("usage_before"),
        "usage_after": result.get("usage_after"),
        "best_opportunities": result.get("best_opportunities"),
        "queue_summary": result.get("queue_summary"),
        "acquisition_locked": acquisition_locked(),
        "scheduler_not_activated": True,
        "reserve_intact": result.get("reserve_intact"),
        "error": result.get("error"),
        "stop": "AWAIT_OWNER_REVIEW",
    }


@app.post("/api/m3/nextbid/campaign-250")
def api_nextbid_campaign_250(user: User | None = Depends(current_user)):
    """Owner single-use max-250 NextBid product campaign. Always restores killswitch.

    Not permission for recurring daily acquisition. Authorization auto-revokes on finish/stop.
    """
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return {"ok": False, "error": "forbidden"}
    from m3.research.nextbid_campaign_250 import run_campaign_250
    from m3.research.nextbid_controller import acquisition_locked

    result = run_campaign_250()
    return {
        "ok": bool(result.get("ok")),
        "build": BUILD_ID,
        "campaign_id": result.get("campaign_id"),
        "credits_consumed_account": result.get("credits_consumed_account"),
        "lane_a_credits": result.get("lane_a_credits"),
        "lane_b_credits": result.get("lane_b_credits"),
        "checkpoints_hit": result.get("checkpoints_hit"),
        "stop_reason": result.get("stop_reason"),
        "newly_quote_ready": result.get("newly_quote_ready"),
        "shortlist": result.get("shortlist"),
        "queue_summary": result.get("queue_summary"),
        "usage_before": result.get("usage_before"),
        "usage_after": result.get("usage_after"),
        "acquisition_locked": acquisition_locked(),
        "authorization_revoked": True,
        "scheduler_not_activated": True,
        "reserve_intact": result.get("reserve_intact"),
        "error": result.get("error"),
        "stop": "AWAIT_OWNER_REVIEW",
    }


@app.post("/api/m3/nextbid/run")
def api_nextbid_run(
    user: User | None = Depends(current_user),
    force: bool = False,
    max_discovery: int = 40,
    max_detail: int = 50,
):
    """Owner-triggered acquisition — blocked by centralized controller unless authorized."""
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return {"ok": False, "error": "forbidden"}
    from m3.research.nextbid_controller import acquisition_locked, controller_status

    if acquisition_locked():
        return {
            "ok": False,
            "error": "acquisition_locked",
            "message": "NextBid acquisition locked. Owner authorization required.",
            "controller": controller_status(),
        }
    settings = get_settings()
    if not (settings.nextbid_api_key or "").strip():
        return {"ok": False, "error": "NEXTBID_API_KEY not configured", "requires_secure_configuration": True}
    from m3.research.nextbid_acquire import run_nextbid_acquisition

    # Hard caps: job ≤50, and discovery+detail must fit daily/test ceilings
    result = run_nextbid_acquisition(
        max_discovery_calls=min(max(int(max_discovery), 1), 50),
        max_detail=min(max(int(max_detail), 1), 50),
        detail_workers=2,
        checkpoint_name="nextbid_acquire_v1",
    )
    return {
        "ok": bool(result.get("ok")),
        "forced": bool(force),
        "usage_before": result.get("usage_before"),
        "usage_after": result.get("usage_after"),
        "discovery": result.get("discovery"),
        "qualification": {
            "stage_counts": (result.get("qualification") or {}).get("stage_counts"),
            "strict_m3_procurement_fits": (result.get("qualification") or {}).get(
                "strict_m3_procurement_fits"
            ),
            "fits": (result.get("qualification") or {}).get("fits") or [],
        },
        "budget": result.get("budget"),
        "source_yield": result.get("source_yield"),
    }


@app.get("/api/m3/package-jobs/{job_id}")
def api_package_job_status(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return {"ok": False, "error": "auth_required"}
    from m3.models import PackageAcquisitionJob
    from m3.research.population_acquire import job_metrics

    job = db.get(PackageAcquisitionJob, job_id)
    if not job:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, **job_metrics(db, job)}


@app.post("/api/m3/package-jobs/{job_id}/resume")
def api_package_job_resume(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return {"ok": False, "error": "forbidden"}
    from m3.models import PackageAcquisitionJob
    from m3.research.population_acquire import start_background_worker, unpause_job

    job = db.get(PackageAcquisitionJob, job_id)
    if not job:
        return {"ok": False, "error": "not_found"}
    if job.status == "completed":
        return {"ok": True, "status": "completed", "started": False}
    if job.status in {"cancelled"}:
        return {"ok": False, "error": "cancelled"}
    if job.status == "paused":
        unpause_job(db, job_id)
        job = db.get(PackageAcquisitionJob, job_id)
    started = start_background_worker(job.id, workers=2)
    return {"ok": True, "status": job.status if job else None, "started": started}


@app.post("/api/m3/package-jobs/{job_id}/replan")
def api_package_job_replan(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    """Apply progressive qualification to pending items (Build 002F)."""
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return {"ok": False, "error": "forbidden"}
    from m3.research.population_acquire import replan_pending_progressive

    return replan_pending_progressive(db, job_id)


@app.post("/api/m3/package-jobs/{job_id}/batch")
def api_package_job_batch(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
    batch_size: int = 40,
):
    """Process one bounded batch (for harness / ops). Production uses background worker."""
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return {"ok": False, "error": "forbidden"}
    from m3.research.population_acquire import process_job_batch

    return process_job_batch(db, job_id, batch_size=min(max(batch_size, 1), 100))


@app.post("/opportunities/{opportunity_id}/status")
def opportunity_set_status(
    opportunity_id: uuid.UUID,
    status: str = Form(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return RedirectResponse("/login", status_code=303)
    if status not in {"saved", "pursuing", "archived", "not_interested"}:
        return RedirectResponse(f"/opportunities/{opportunity_id}", status_code=303)
    opp = db.get(Opportunity, opportunity_id)
    if opp:
        before = {"status": opp.status}
        opp.status = status
        db.add(opp)
        record_audit(
            db,
            action="opportunity.status",
            entity_type="opportunity",
            entity_id=str(opp.id),
            actor_user_id=user.id,
            actor_label=user.email,
            before=before,
            after={"status": status},
        )
        db.commit()
    return RedirectResponse(f"/opportunities/{opportunity_id}", status_code=303)


@app.post("/opportunities/{opportunity_id}/unsave")
def opportunity_unsave(
    opportunity_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return RedirectResponse("/login", status_code=303)
    opp = db.get(Opportunity, opportunity_id)
    if opp:
        record_audit(
            db,
            action="opportunity.unsave",
            entity_type="opportunity",
            entity_id=str(opp.id),
            actor_user_id=user.id,
            actor_label=user.email,
            before={"title": opp.title, "status": opp.status},
        )
        db.delete(opp)
        db.commit()
    return RedirectResponse("/opportunities", status_code=303)


@app.post("/opportunities/{opportunity_id}/assign")
def opportunity_assign(
    opportunity_id: uuid.UUID,
    assigned_to: str = Form(""),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return RedirectResponse("/login", status_code=303)
    opp = db.get(Opportunity, opportunity_id)
    if opp:
        before = {"assigned_to": str(opp.assigned_to) if opp.assigned_to else None}
        if assigned_to:
            try:
                opp.assigned_to = uuid.UUID(assigned_to)
            except ValueError:
                opp.assigned_to = None
        else:
            opp.assigned_to = None
        db.add(opp)
        record_audit(
            db,
            action="opportunity.assign",
            entity_type="opportunity",
            entity_id=str(opp.id),
            actor_user_id=user.id,
            actor_label=user.email,
            before=before,
            after={"assigned_to": str(opp.assigned_to) if opp.assigned_to else None},
        )
        db.commit()
    return RedirectResponse(f"/opportunities/{opportunity_id}", status_code=303)


@app.post("/opportunities/{opportunity_id}/notes")
def opportunity_notes(
    opportunity_id: uuid.UUID,
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return RedirectResponse("/login", status_code=303)
    opp = db.get(Opportunity, opportunity_id)
    if opp:
        before = {"notes": opp.notes}
        opp.notes = notes
        db.add(opp)
        record_audit(
            db,
            action="opportunity.notes",
            entity_type="opportunity",
            entity_id=str(opp.id),
            actor_user_id=user.id,
            actor_label=user.email,
            before=before,
            after={"notes": notes[:500]},
        )
        db.commit()
    return RedirectResponse(f"/opportunities/{opportunity_id}", status_code=303)


# ---- Suppliers ----
@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_list(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    q = (request.query_params.get("q") or "").strip()
    query = db.query(Supplier)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Supplier.name.ilike(like), Supplier.website.ilike(like)))
    items = query.order_by(Supplier.updated_at.desc()).limit(100).all()
    return TEMPLATES.TemplateResponse("suppliers_list.html", _ctx(request, user, "suppliers", items=items, q=q))


@app.get("/suppliers/find", response_class=HTMLResponse)
def suppliers_find(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    if not has_perm(user.role, user.extra_permissions, "suppliers.search"):
        return RedirectResponse("/", status_code=303)
    qp = request.query_params
    categories_raw = qp.get("product_categories") or ""
    keyword = qp.get("keyword") or ""
    ran = "product_categories" in qp or "keyword" in qp or qp.get("run") == "1"
    result = {"count": 0, "items": [], "sources_succeeded": [], "limitations": []}
    if ran:
        cats = [c.strip() for c in categories_raw.split(",") if c.strip()]
        result = run_supplier_discovery(
            db,
            user_id=user.id,
            filters={"product_categories": cats, "keyword": keyword},
            limit=None,
            enable_openai=qp.get("ai") != "0",
        )
    return TEMPLATES.TemplateResponse(
        "suppliers_find.html",
        _ctx(
            request,
            user,
            "suppliers",
            ran=ran,
            result=result,
            categories_raw=categories_raw,
            keyword=keyword,
            spend=spending_snapshot(db, user_id=user.id) if is_primary_owner(user) else None,
        ),
    )


@app.get("/suppliers/{supplier_id}/find-contracts", response_class=HTMLResponse)
def supplier_reverse_search(
    supplier_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return _login_redirect()
    if not has_perm(user.role, user.extra_permissions, "opportunities.search"):
        return RedirectResponse("/suppliers", status_code=303)
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        return RedirectResponse("/suppliers", status_code=303)
    mode = request.query_params.get("mode") or "category"
    cats = list(supplier.product_categories or [])
    products = (
        db.query(CatalogProduct)
        .filter(CatalogProduct.supplier_id == supplier.id)
        .limit(100)
        .all()
    )
    keyword = ""
    search_mode = "broad"
    if mode == "exact_catalog" and products:
        # Use first available MPN as exact search; UI can refine
        mpns = [p.mpn for p in products if p.mpn]
        keyword = mpns[0] if mpns else (products[0].description or "")[:80]
        search_mode = "exact_mpn" if mpns else "broad"
    elif mode == "category":
        keyword = ", ".join(cats[:4]) if cats else (supplier.name or "")
        search_mode = "broad"
    else:
        keyword = ", ".join(cats[:3] + [supplier.name or ""])
        search_mode = "broad"
    filters = {
        "keyword": keyword,
        "search_mode": search_mode,
        "product_categories": cats,
        "product_resale_only": True,
        "include_unknown_value": True,
    }
    result = run_contract_discovery(db, user_id=user.id, filters=filters, limit=get_settings().research_display_page_size)
    rows = [enrich_discovery_item(i, catalog_matches=_catalog_match_count(db, i)) for i in (result.get("items") or [])]
    return TEMPLATES.TemplateResponse(
        "supplier_find_contracts.html",
        _ctx(
            request,
            user,
            "suppliers",
            supplier=supplier,
            mode=mode,
            result=result,
            rows=rows,
            run_id=result.get("run_id"),
        ),
    )


@app.post("/suppliers/save-discovery")
async def suppliers_save_discovery(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user or not has_perm(user.role, user.extra_permissions, "suppliers.write"):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    try:
        item = json.loads(base64.urlsafe_b64decode(str(form.get("payload") or "").encode("ascii")).decode("utf-8"))
    except Exception:
        return RedirectResponse("/suppliers/find", status_code=303)
    save_supplier_from_discovery(db, item, user)
    return RedirectResponse("/suppliers", status_code=303)


@app.get("/suppliers/new", response_class=HTMLResponse)
def supplier_new_form(request: Request, user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    return TEMPLATES.TemplateResponse("supplier_new.html", _ctx(request, user, "suppliers"))


@app.post("/suppliers/new")
def supplier_new_submit(
    request: Request,
    name: str = Form(...),
    website: str = Form(""),
    supplier_type: str = Form(""),
    product_categories: str = Form(""),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "suppliers.write"):
        return RedirectResponse("/login", status_code=303)
    key = normalize_supplier_key(name, website or None)
    if db.query(Supplier).filter_by(normalized_key=key).one_or_none():
        return RedirectResponse("/suppliers", status_code=303)
    sup = Supplier(
        name=name.strip(),
        website=website or None,
        normalized_key=key,
        supplier_type=supplier_type or None,
        product_categories=[c.strip() for c in product_categories.split(",") if c.strip()],
        created_by=user.id,
        assigned_to=user.id,
    )
    db.add(sup)
    db.flush()
    record_audit(
        db,
        action="supplier.create",
        entity_type="supplier",
        entity_id=str(sup.id),
        actor_user_id=user.id,
        actor_label=user.email,
        after={"name": name},
    )
    db.commit()
    return RedirectResponse(f"/suppliers/{sup.id}", status_code=303)


@app.get("/suppliers/follow-ups", response_class=HTMLResponse)
def supplier_followups(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    items = (
        db.query(Supplier)
        .filter(Supplier.follow_up_on != None)  # noqa: E711
        .order_by(Supplier.follow_up_on.asc())
        .limit(100)
        .all()
    )
    return TEMPLATES.TemplateResponse("followups.html", _ctx(request, user, "suppliers", items=items))


@app.get("/suppliers/catalogs", response_class=HTMLResponse)
def catalogs_page(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    q = (request.query_params.get("q") or "").strip()
    products = []
    if q:
        products = exact_mpn_matches(db, q)
        if not products:
            like = f"%{q}%"
            products = (
                db.query(CatalogProduct)
                .filter(
                    or_(
                        CatalogProduct.mpn.ilike(like),
                        CatalogProduct.supplier_sku.ilike(like),
                        CatalogProduct.description.ilike(like),
                    )
                )
                .limit(50)
                .all()
            )
    uploads = db.query(CatalogUpload).order_by(CatalogUpload.created_at.desc()).limit(20).all()
    return TEMPLATES.TemplateResponse(
        "catalogs.html", _ctx(request, user, "suppliers", q=q, products=products, uploads=uploads)
    )


@app.get("/suppliers/{supplier_id}", response_class=HTMLResponse)
def supplier_detail(supplier_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        return RedirectResponse("/suppliers", status_code=303)
    opts = _supplier_form_options()
    return TEMPLATES.TemplateResponse(
        "supplier_detail.html",
        _ctx(
            request,
            user,
            "suppliers",
            supplier=supplier,
            address_type_label=label(supplier.address_type) if supplier.address_type else "",
            **opts,
        ),
    )


@app.post("/suppliers/{supplier_id}/company")
async def supplier_company_update(
    supplier_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "suppliers.write"):
        return RedirectResponse("/login", status_code=303)
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        return RedirectResponse("/suppliers", status_code=303)
    form = await request.form()
    before = {
        "website": supplier.website,
        "business_address": supplier.business_address,
        "address_type": supplier.address_type,
        "general_phone": supplier.general_phone,
        "general_email": supplier.general_email,
    }
    supplier.website = (str(form.get("website") or "").strip() or None)
    supplier.business_address = (str(form.get("business_address") or "").strip() or None)
    supplier.address_type = (str(form.get("address_type") or "").strip() or None)
    supplier.general_phone = (str(form.get("general_phone") or "").strip() or None)
    supplier.general_email = (str(form.get("general_email") or "").strip() or None)
    db.add(supplier)
    record_audit(
        db,
        action="supplier.company_contact",
        entity_type="supplier",
        entity_id=str(supplier.id),
        actor_user_id=user.id,
        actor_label=user.email,
        before=before,
        after={
            "website": supplier.website,
            "business_address": supplier.business_address,
            "address_type": supplier.address_type,
            "general_phone": supplier.general_phone,
            "general_email": supplier.general_email,
        },
    )
    db.commit()
    return RedirectResponse(f"/suppliers/{supplier.id}", status_code=303)


@app.post("/suppliers/{supplier_id}/update")
async def supplier_update(supplier_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user or not has_perm(user.role, user.extra_permissions, "suppliers.write"):
        return RedirectResponse("/login", status_code=303)
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        return RedirectResponse("/suppliers", status_code=303)
    form = await request.form()
    before = {
        "reseller_application_status": supplier.reseller_application_status,
        "credit_application_status": supplier.credit_application_status,
        "payment_terms": supplier.payment_terms,
    }
    for field in (
        "relationship_status",
        "reseller_application_status",
        "credit_application_status",
        "account_approval_status",
        "dealer_authorization_status",
        "payment_terms",
        "personal_guarantee_required",
        "personal_credit_required",
        "next_action",
    ):
        if field in form:
            setattr(supplier, field, str(form.get(field) or ""))
    if form.get("approved_credit_limit"):
        try:
            supplier.approved_credit_limit = float(str(form.get("approved_credit_limit")))
        except ValueError:
            pass
    if form.get("follow_up_on"):
        try:
            supplier.follow_up_on = date.fromisoformat(str(form.get("follow_up_on")))
        except ValueError:
            supplier.follow_up_on = None
    else:
        supplier.follow_up_on = None
    db.add(supplier)
    record_audit(
        db,
        action="supplier.update",
        entity_type="supplier",
        entity_id=str(supplier.id),
        actor_user_id=user.id,
        actor_label=user.email,
        before=before,
        after={
            "reseller_application_status": supplier.reseller_application_status,
            "credit_application_status": supplier.credit_application_status,
            "payment_terms": supplier.payment_terms,
        },
    )
    db.commit()
    return RedirectResponse(f"/suppliers/{supplier.id}", status_code=303)


@app.post("/suppliers/{supplier_id}/contacts")
def supplier_add_contact(
    supplier_id: uuid.UUID,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "suppliers.write"):
        return RedirectResponse("/login", status_code=303)
    from m3.models import SupplierContact

    db.add(SupplierContact(supplier_id=supplier_id, name=name or None, email=email or None, phone=phone or None))
    record_audit(
        db,
        action="supplier.contact_add",
        entity_type="supplier",
        entity_id=str(supplier_id),
        actor_user_id=user.id,
        actor_label=user.email,
        after={"email": email, "phone": phone},
    )
    db.commit()
    return RedirectResponse(f"/suppliers/{supplier_id}", status_code=303)


@app.post("/suppliers/{supplier_id}/notes")
def supplier_add_note(
    supplier_id: uuid.UUID,
    body: str = Form(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "suppliers.write"):
        return RedirectResponse("/login", status_code=303)
    from m3.models import SupplierNote

    db.add(SupplierNote(supplier_id=supplier_id, author_id=user.id, body=body, kind="note"))
    record_audit(
        db,
        action="supplier.note",
        entity_type="supplier",
        entity_id=str(supplier_id),
        actor_user_id=user.id,
        actor_label=user.email,
        after={"len": len(body)},
    )
    db.commit()
    return RedirectResponse(f"/suppliers/{supplier_id}", status_code=303)


@app.post("/suppliers/{supplier_id}/catalogs")
async def supplier_upload_catalog(
    supplier_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "catalogs.write"):
        return RedirectResponse("/login", status_code=303)
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        return RedirectResponse("/suppliers", status_code=303)
    raw = await file.read()
    if len(raw) > get_settings().upload_max_bytes:
        return RedirectResponse(f"/suppliers/{supplier_id}", status_code=303)
    name = file.filename or "upload.bin"
    if not name.lower().endswith((".csv", ".xlsx", ".xls", ".pdf")):
        return RedirectResponse(f"/suppliers/{supplier_id}", status_code=303)
    ingest_catalog(
        db,
        supplier=supplier,
        user=user,
        filename=name,
        content=raw,
        content_type=file.content_type or "",
    )
    return RedirectResponse("/suppliers/catalogs", status_code=303)


# ---- Tasks ----
@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    if user.role == "owner":
        tasks = db.query(Task).order_by(Task.created_at.desc()).limit(100).all()
    else:
        tasks = (
            db.query(Task)
            .filter(or_(Task.assigned_to == user.id, Task.created_by == user.id))
            .order_by(Task.created_at.desc())
            .limit(100)
            .all()
        )
    users = db.query(User).filter(User.is_active.is_(True)).all() if has_perm(user.role, user.extra_permissions, "tasks.assign") else []
    return TEMPLATES.TemplateResponse(
        "tasks.html",
        _ctx(request, user, "tasks", tasks=tasks, users=users, can_assign=bool(users)),
    )


@app.post("/tasks")
def tasks_create(
    title: str = Form(...),
    body: str = Form(""),
    due_on: str = Form(""),
    assigned_to: str = Form(""),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "tasks.write"):
        return RedirectResponse("/login", status_code=303)
    assignee = user.id
    if assigned_to and has_perm(user.role, user.extra_permissions, "tasks.assign"):
        try:
            assignee = uuid.UUID(assigned_to)
        except ValueError:
            assignee = user.id
    due = None
    if due_on:
        try:
            due = date.fromisoformat(due_on)
        except ValueError:
            due = None
    task = Task(title=title.strip(), body=body, due_on=due, assigned_to=assignee, created_by=user.id)
    db.add(task)
    db.flush()
    record_audit(
        db,
        action="task.create",
        entity_type="task",
        entity_id=str(task.id),
        actor_user_id=user.id,
        actor_label=user.email,
        after={"title": title},
    )
    db.commit()
    return RedirectResponse("/tasks", status_code=303)


@app.post("/tasks/{task_id}/done")
def task_done(task_id: uuid.UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    task = db.get(Task, task_id)
    if task:
        task.status = "done"
        db.add(task)
        record_audit(
            db,
            action="task.done",
            entity_type="task",
            entity_id=str(task.id),
            actor_user_id=user.id,
            actor_label=user.email,
        )
        db.commit()
    return RedirectResponse("/tasks", status_code=303)


def _invite_redirect(request: Request, raw: str, expires_at) -> RedirectResponse:
    base = str(request.base_url).rstrip("/")
    link = f"{base}/signup?token={raw}"
    exp = format_activity_time(expires_at)
    return RedirectResponse(
        f"/settings?invite_link={quote(link, safe='')}&invite_expires={quote(exp, safe='')}",
        status_code=303,
    )


# ---- Settings / users / audit ----
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    can = is_primary_owner(user)
    users = []
    invitations = []
    if can:
        users = (
            db.query(User)
            .filter(or_(User.team_status.is_(None), User.team_status == "active"))
            .order_by(User.created_at.asc())
            .all()
        )
        for u in users:
            u.role_label = label(u.role)  # type: ignore[attr-defined]
            if u.is_primary_owner:
                u.status_label = "Primary owner"  # type: ignore[attr-defined]
            elif u.pending_email:
                u.status_label = "Pending email confirmation"  # type: ignore[attr-defined]
            elif u.is_active:
                u.status_label = "Active"  # type: ignore[attr-defined]
            else:
                u.status_label = "Inactive"  # type: ignore[attr-defined]
        for inv in (
            db.query(UserInvitation)
            .filter(UserInvitation.status.in_(["pending", "expired"]))
            .order_by(UserInvitation.created_at.desc())
            .limit(40)
            .all()
        ):
            invitations.append(
                type(
                    "Inv",
                    (),
                    {
                        "id": inv.id,
                        "email": inv.email,
                        "invited_name": inv.invited_name,
                        "role": inv.role,
                        "role_label": label(inv.role),
                        "status": inv.status,
                        "status_label": label(inv.status),
                        "expires_at": inv.expires_at,
                        "expires_human": format_activity_time(inv.expires_at),
                    },
                )()
            )
    invite_link = request.query_params.get("invite_link") or ""
    invite_expires = request.query_params.get("invite_expires") or ""
    flash = None
    if invite_link:
        flash = {"message": "Invitation created. Copy the link and send it to the person.", "kind": "ok"}
    elif request.query_params.get("err") == "password":
        flash = {"message": "Password update failed — check length and confirmation.", "kind": "err"}
    elif request.query_params.get("err") == "duplicate":
        flash = {"message": "An active team member already uses that email.", "kind": "err"}
    elif request.query_params.get("removed") == "1":
        flash = {"message": "Team member removed from the active list. Login access revoked.", "kind": "ok"}
    elif request.query_params.get("archived") == "1":
        flash = {"message": "Team member archived. Historical attribution preserved; login revoked.", "kind": "ok"}
    return TEMPLATES.TemplateResponse(
        "settings.html",
        _ctx(
            request,
            user,
            "settings",
            can_manage_users=can,
            users=users,
            invitations=invitations,
            invite_link=invite_link,
            invite_expires=invite_expires,
            show_add_form=request.query_params.get("add") == "1",
            settings_tab="team",
            flash=flash,
        ),
    )


@app.post("/settings/profile")
def settings_profile(
    full_name: str = Form(...),
    password: str = Form(""),
    password_confirm: str = Form(""),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    before = {"full_name": user.full_name}
    user.full_name = full_name.strip() or user.full_name
    if password:
        if password != password_confirm or len(password) < 10:
            return RedirectResponse("/settings?err=password", status_code=303)
        user.password_hash = hash_password(password)
        bump_session_version(db, user, actor=user, reason="self_password_change")
    db.add(user)
    record_audit(
        db,
        action="user.profile_update",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=user.id,
        actor_label=user.email,
        before=before,
        after={"full_name": user.full_name, "password_changed": bool(password)},
    )
    db.commit()
    if password:
        # Re-issue cookie with new session_version
        resp = RedirectResponse("/settings", status_code=303)
        settings = get_settings()
        resp.set_cookie(
            settings.session_cookie,
            issue_session_token(str(user.id), user.session_version or 1),
            httponly=True,
            samesite="lax",
            max_age=settings.session_max_age_seconds,
        )
        return resp
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/invitations")
def settings_create_invitation(
    request: Request,
    email: str = Form(...),
    role: str = Form(...),
    full_name: str = Form(""),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    if role not in {"supplier_manager", "contract_specialist"}:
        role = "contract_specialist"
    try:
        inv, raw = create_invitation(
            db, email=email, role=role, actor=user, full_name=full_name or None
        )
    except ValueError:
        return RedirectResponse("/settings?add=1&err=duplicate", status_code=303)
    return _invite_redirect(request, raw, inv.expires_at)


@app.post("/settings/invitations/{invite_id}/revoke")
def settings_revoke_invitation(
    invite_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    inv = db.get(UserInvitation, invite_id)
    if inv and inv.status in {"pending", "expired"}:
        inv.status = "revoked"
        db.add(inv)
        record_audit(
            db,
            action="user.invite_revoke",
            entity_type="invitation",
            entity_id=str(inv.id),
            actor_user_id=user.id,
            actor_label=user.email,
            after={"email": inv.email},
        )
        db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/invitations/{invite_id}/renew")
def settings_renew_invitation(
    request: Request,
    invite_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    inv = db.get(UserInvitation, invite_id)
    if not inv:
        return RedirectResponse("/settings", status_code=303)
    new_inv, raw = create_invitation(
        db,
        email=inv.email,
        role=inv.role,
        actor=user,
        full_name=inv.invited_name,
        extra_permissions=inv.extra_permissions,
        purpose=inv.purpose or "new_user",
        target_user_id=inv.target_user_id,
    )
    return _invite_redirect(request, raw, new_inv.expires_at)


@app.get("/settings/users/{user_id}", response_class=HTMLResponse)
def settings_user_edit_page(
    user_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    target = db.get(User, user_id)
    if not target or target.is_primary_owner or (target.team_status or "active") != "active":
        return RedirectResponse("/settings", status_code=303)
    flash = None
    if request.query_params.get("email_pending") == "1":
        flash = {
            "message": "Email change started. Copy the invitation link from Team and send it to the new address.",
            "kind": "ok",
        }
    return TEMPLATES.TemplateResponse(
        "settings_user_edit.html",
        _ctx(request, user, "settings", target=target, flash=flash),
    )


@app.post("/settings/users/{user_id}/edit")
def settings_edit_user(
    user_id: uuid.UUID,
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    role: str = Form(...),
    is_active: str = Form("1"),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    target = db.get(User, user_id)
    if not target or target.is_primary_owner:
        return RedirectResponse("/settings", status_code=303)
    if role not in {"supplier_manager", "contract_specialist"}:
        role = target.role
    new_email = email.lower().strip()
    before = {
        "full_name": target.full_name,
        "email": target.email,
        "role": target.role,
        "is_active": target.is_active,
    }
    target.full_name = full_name.strip() or target.full_name
    target.role = role
    want_active = is_active == "1"
    email_changed = new_email and new_email != (target.email or "").lower()
    invite_raw = None
    invite_exp = None
    if email_changed:
        conflict = (
            db.query(User)
            .filter(User.email == new_email, User.id != target.id, User.team_status == "active")
            .one_or_none()
        )
        if conflict:
            return RedirectResponse(f"/settings/users/{user_id}?err=duplicate", status_code=303)
        target.pending_email = new_email
        target.is_active = False
        bump_session_version(db, target, actor=user, reason="email_change_pending")
        inv, invite_raw = create_invitation(
            db,
            email=new_email,
            role=target.role,
            actor=user,
            full_name=target.full_name,
            purpose="email_change",
            target_user_id=target.id,
        )
        invite_exp = inv.expires_at
        record_audit(
            db,
            action="user.email_change_pending",
            entity_type="user",
            entity_id=str(target.id),
            actor_user_id=user.id,
            actor_label=user.email,
            before=before,
            after={"pending_email": new_email},
        )
    else:
        target.is_active = want_active
        if not want_active:
            bump_session_version(db, target, actor=user, reason="deactivated")
    db.add(target)
    record_audit(
        db,
        action="user.edit",
        entity_type="user",
        entity_id=str(target.id),
        actor_user_id=user.id,
        actor_label=user.email,
        before=before,
        after={
            "full_name": target.full_name,
            "email": target.email,
            "role": target.role,
            "is_active": target.is_active,
            "pending_email": target.pending_email,
        },
    )
    db.commit()
    if invite_raw:
        return _invite_redirect(request, invite_raw, invite_exp)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/users/{user_id}/reset-password")
def settings_reset_password(
    user_id: uuid.UUID,
    password: str = Form(...),
    password_confirm: str = Form(""),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    target = db.get(User, user_id)
    if not target or target.is_primary_owner:
        return RedirectResponse("/settings", status_code=303)
    if password != password_confirm or len(password) < 10:
        return RedirectResponse(f"/settings/users/{user_id}?err=password", status_code=303)
    target.password_hash = hash_password(password)
    db.add(target)
    record_audit(
        db,
        action="user.password_reset",
        entity_type="user",
        entity_id=str(target.id),
        actor_user_id=user.id,
        actor_label=user.email,
        after={"reset_by_primary_owner": True},
    )
    bump_session_version(db, target, actor=user, reason="password_reset")
    db.commit()
    return RedirectResponse(f"/settings/users/{user_id}", status_code=303)


@app.post("/settings/users/{user_id}/reinvite")
def settings_user_reinvite(
    user_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    target = db.get(User, user_id)
    if not target or target.is_primary_owner:
        return RedirectResponse("/settings", status_code=303)
    # Only useful while an email change is pending (activation for the corrected address).
    email = target.pending_email or target.email
    inv, raw = create_invitation(
        db,
        email=email,
        role=target.role,
        actor=user,
        full_name=target.full_name,
        purpose="email_change",
        target_user_id=target.id,
    )
    if not target.pending_email:
        target.pending_email = email
        target.is_active = False
        bump_session_version(db, target, actor=user, reason="reinvite_activation")
        db.add(target)
        db.commit()
    return _invite_redirect(request, raw, inv.expires_at)


@app.get("/settings/users/{user_id}/remove", response_class=HTMLResponse)
def settings_user_remove_page(
    user_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    target = db.get(User, user_id)
    if not target or target.is_primary_owner:
        return RedirectResponse("/settings", status_code=303)
    return TEMPLATES.TemplateResponse(
        "settings_user_remove.html",
        _ctx(
            request,
            user,
            "settings",
            target=target,
            role_label=label(target.role),
            has_activity=user_has_business_activity(db, target.id),
            is_samantha=is_protected_samantha(target),
        ),
    )


@app.post("/settings/users/{user_id}/remove")
def settings_user_remove(
    user_id: uuid.UUID,
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    if confirm != "1":
        return RedirectResponse(f"/settings/users/{user_id}/remove", status_code=303)
    target = db.get(User, user_id)
    if not target or target.is_primary_owner or target.id == user.id:
        return RedirectResponse("/settings", status_code=303)

    # Revoke pending invitations for this email
    for inv in db.query(UserInvitation).filter(
        UserInvitation.email.in_([target.email, target.pending_email or ""]),
        UserInvitation.status == "pending",
    ).all():
        inv.status = "revoked"
        db.add(inv)

    has_activity = user_has_business_activity(db, target.id)
    bump_session_version(db, target, actor=user, reason="removed_from_team")
    if has_activity:
        target.is_active = False
        target.team_status = "archived"
        target.pending_email = None
        db.add(target)
        record_audit(
            db,
            action="user.archive",
            entity_type="user",
            entity_id=str(target.id),
            actor_user_id=user.id,
            actor_label=user.email,
            after={"email": target.email, "team_status": "archived"},
        )
        db.commit()
        return RedirectResponse("/settings?archived=1", status_code=303)

    email = target.email
    record_audit(
        db,
        action="user.remove",
        entity_type="user",
        entity_id=str(target.id),
        actor_user_id=user.id,
        actor_label=user.email,
        after={"email": email, "deleted": True},
    )
    # Keep audit history; drop FK links so unused test accounts can be removed.
    detach_user_for_safe_delete(db, target)
    db.delete(target)
    db.commit()
    return RedirectResponse("/settings?removed=1", status_code=303)


@app.get("/settings/research", response_class=HTMLResponse)
def settings_research(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    ensure_budget_policy(db)
    snap = spending_snapshot(db, user_id=user.id)
    jobs = (
        db.query(ResearchRun)
        .order_by(ResearchRun.started_at.desc())
        .limit(40)
        .all()
    )
    spends = (
        db.query(ResearchSpendEvent)
        .order_by(ResearchSpendEvent.created_at.desc())
        .limit(40)
        .all()
    )
    flash = None
    if request.query_params.get("saved") == "1":
        flash = {"message": "Research budget policy updated.", "kind": "ok"}
    return TEMPLATES.TemplateResponse(
        "settings_research.html",
        _ctx(
            request,
            user,
            "settings",
            settings_tab="research",
            spend=snap,
            jobs=jobs,
            spends=spends,
            flash=flash,
            can_manage_users=True,
        ),
    )


@app.post("/settings/research")
def settings_research_update(
    paid_research_enabled: str = Form("0"),
    daily_budget_usd: str = Form("1"),
    monthly_budget_usd: str = Form("30"),
    per_job_budget_usd: str = Form("0.5"),
    per_user_daily_budget_usd: str = Form("1"),
    max_research_depth: str = Form("2"),
    default_model: str = Form("gpt-4o-mini"),
    capable_model: str = Form("gpt-5-mini"),
    search_context_size: str = Form("low"),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)

    def _f(v: str, default: float) -> float:
        try:
            return float(v)
        except Exception:
            return default

    update_owner_policy(
        db,
        actor_id=user.id,
        paid_research_enabled=paid_research_enabled == "1",
        daily_budget_usd=max(0.0, _f(daily_budget_usd, 1.0)),
        monthly_budget_usd=max(0.0, _f(monthly_budget_usd, 30.0)),
        per_job_budget_usd=max(0.0, _f(per_job_budget_usd, 0.5)),
        per_user_daily_budget_usd=max(0.0, _f(per_user_daily_budget_usd, 1.0)),
        max_research_depth=max(1, int(_f(max_research_depth, 2))),
        default_model=(default_model or "gpt-4o-mini").strip(),
        capable_model=(capable_model or "gpt-5-mini").strip(),
        search_context_size=(search_context_size or "low").strip() if search_context_size in {"low", "medium", "high"} else "low",
    )
    record_audit(
        db,
        action="research.budget_update",
        entity_type="research_budget_policy",
        entity_id="policy",
        actor_user_id=user.id,
        actor_label=user.email,
        after=spending_snapshot(db, user_id=user.id),
    )
    db.commit()
    return RedirectResponse("/settings/research?saved=1", status_code=303)


@app.get("/settings/cleanup", response_class=HTMLResponse)
def settings_cleanup(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    preview = build_cleanup_preview(db)
    return TEMPLATES.TemplateResponse(
        "cleanup.html",
        _ctx(request, user, "settings", preview=preview, settings_tab="cleanup"),
    )


@app.post("/settings/cleanup/apply")
async def settings_cleanup_apply(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    form = await request.form()
    if form.get("confirm") != "1":
        return RedirectResponse("/settings/cleanup", status_code=303)
    preview = build_cleanup_preview(db)
    allowed_opps = {r["id"] for r in preview["opportunities"]}
    allowed_sups = {r["id"] for r in preview["suppliers"]}
    allowed_users = {r["id"] for r in preview["users"]}
    removed = {"opportunities": 0, "suppliers": 0, "users": 0}
    for oid in form.getlist("opp_ids"):
        if str(oid) not in allowed_opps:
            continue
        opp = db.get(Opportunity, uuid.UUID(str(oid)))
        if not opp:
            continue
        opp.status = "archived"
        opp.data_origin = "acceptance_harness"
        db.add(opp)
        record_audit(
            db,
            action="cleanup.opportunity_archive",
            entity_type="opportunity",
            entity_id=str(opp.id),
            actor_user_id=user.id,
            actor_label=user.email,
            after={"status": "archived", "data_origin": "acceptance_harness", "title": opp.title[:120]},
        )
        removed["opportunities"] += 1
    for sid in form.getlist("supplier_ids"):
        if str(sid) not in allowed_sups:
            continue
        sup = db.get(Supplier, uuid.UUID(str(sid)))
        if not sup:
            continue
        # Soft-remove: mark origin + deactivate relationship rather than hard-delete catalogs blindly
        before = {"name": sup.name, "relationship_status": sup.relationship_status}
        sup.data_origin = "acceptance_harness"
        sup.relationship_status = "inactive"
        sup.follow_up_on = None
        db.add(sup)
        record_audit(
            db,
            action="cleanup.supplier_deactivate",
            entity_type="supplier",
            entity_id=str(sup.id),
            actor_user_id=user.id,
            actor_label=user.email,
            before=before,
            after={"relationship_status": "inactive", "data_origin": "acceptance_harness"},
        )
        removed["suppliers"] += 1
    for uid in form.getlist("user_ids"):
        if str(uid) not in allowed_users:
            continue
        target = db.get(User, uuid.UUID(str(uid)))
        if not target or target.is_primary_owner:
            continue
        target.is_active = False
        db.add(target)
        bump_session_version(db, target, actor=user, reason="cleanup_test_account")
        record_audit(
            db,
            action="cleanup.user_deactivate",
            entity_type="user",
            entity_id=str(target.id),
            actor_user_id=user.id,
            actor_label=user.email,
            after={"email": target.email, "is_active": False},
        )
        removed["users"] += 1
    db.commit()
    return TEMPLATES.TemplateResponse(
        "cleanup.html",
        _ctx(
            request,
            user,
            "settings",
            preview=build_cleanup_preview(db),
            settings_tab="cleanup",
            flash={
                "message": f"Cleanup applied: {removed['opportunities']} opportunities archived, "
                f"{removed['suppliers']} suppliers deactivated, {removed['users']} users deactivated.",
                "kind": "ok",
            },
        ),
    )


@app.get("/settings/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user or not (is_primary_owner(user) or has_perm(user.role, user.extra_permissions, "audit.read")):
        return RedirectResponse("/", status_code=303)
    actor = (request.query_params.get("actor") or "").strip()
    action = (request.query_params.get("action") or "").strip()
    q = db.query(AuditEvent).order_by(AuditEvent.at.desc())
    if actor:
        q = q.filter(AuditEvent.actor_label.ilike(f"%{actor}%"))
    if action:
        q = q.filter(AuditEvent.action.ilike(f"%{action}%"))
    raw_events = q.limit(200).all()
    actor_ids = {e.actor_user_id for e in raw_events if e.actor_user_id}
    users_by_id = {}
    if actor_ids:
        for u in db.query(User).filter(User.id.in_(list(actor_ids))).all():
            users_by_id[u.id] = u
    events = enrich_activity_events(raw_events, users_by_id)
    return TEMPLATES.TemplateResponse(
        "audit.html", _ctx(request, user, "settings", events=events, actor=actor, action=action)
    )

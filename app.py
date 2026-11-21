"""M3 2.0 Build 001A — FastAPI application entrypoint."""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from m3 import BUILD_ID
from m3.audit import record_audit
from m3.bootstrap import init_database
from m3.config import get_settings
from m3.db import get_db
from m3.models import AuditEvent, CatalogProduct, CatalogUpload, Opportunity, ResearchRun, SearchProfile, Supplier, Task, User
from m3.research.engine import run_contract_discovery, run_supplier_discovery
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
from m3.web.display import enrich_discovery_item, format_deadline, format_value, unknown, value_class

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

app = FastAPI(title="M3 Supply", version="2.0.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "m3" / "web" / "static")), name="static")


def _opp_item_from_model(o: Opportunity) -> dict:
    ev = o.raw_evidence or {}
    return {
        "external_key": o.external_key,
        "title": o.title,
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
        "saved_id": str(o.id),
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
    d["procurement_type"] = unknown(item.get("procurement_type"))
    d["source_key"] = unknown(item.get("source_key"))
    d["source_verified_at"] = unknown(item.get("source_verified_at"))
    evidence = item.get("classification_evidence") or (item.get("evidence") or {}).get("classification_evidence")
    if isinstance(evidence, list):
        d["classification_evidence"] = "; ".join(str(x) for x in evidence) if evidence else "Unknown"
    else:
        d["classification_evidence"] = unknown(evidence)
    d["value_evidence"] = unknown(item.get("value_evidence") or (item.get("evidence") or {}).get("value_evidence"), "Not Yet Researched")
    d["value_method"] = unknown(item.get("value_estimate_method"), "Not Yet Researched")
    return d


def _identify_test_accounts(users: list[User]) -> list[dict]:
    out = []
    for u in users:
        reasons = []
        email = (u.email or "").lower()
        if email.endswith("@m3.local") or ".local" in email:
            reasons.append("local test domain")
        if "test" in email or "demo" in email or email.startswith("sam.supplier"):
            reasons.append("test/demo naming")
        if reasons and not u.is_primary_owner:
            out.append({"email": u.email, "full_name": u.full_name, "role": u.role, "is_active": u.is_active, "reason": ", ".join(reasons)})
    return out


def _identify_test_records(db: Session) -> list[dict]:
    out = []
    for o in db.query(Opportunity).order_by(Opportunity.created_at.desc()).limit(200):
        title = (o.title or "").lower()
        if title in {"word", "excel", "external"} or title.startswith("form"):
            out.append({"kind": "opportunity", "label": o.title, "reason": "likely portal template/link noise"})
    for s in db.query(Supplier).order_by(Supplier.created_at.desc()).limit(200):
        name = (s.name or "").lower()
        if "sample" in name or "test" in name or "works international" in name:
            out.append({"kind": "supplier", "label": s.name, "reason": "possible test/seed record"})
    return out[:40]


@app.on_event("startup")
def _startup() -> None:
    init_database()


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
        issue_session_token(str(user.id)),
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


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=14)
    open_tasks = db.query(Task).filter(Task.assigned_to == user.id, Task.status == "open").count()
    followups = (
        db.query(Supplier)
        .filter(Supplier.follow_up_on != None, Supplier.follow_up_on <= date.today())  # noqa: E711
        .count()
    )
    saved_opps = db.query(Opportunity).count()
    supplier_count = db.query(Supplier).count()
    deadlines = (
        db.query(Opportunity)
        .filter(Opportunity.deadline_at != None, Opportunity.deadline_at >= now, Opportunity.deadline_at <= soon)  # noqa: E711
        .order_by(Opportunity.deadline_at.asc())
        .limit(8)
        .all()
    )
    tasks = (
        db.query(Task)
        .filter(Task.assigned_to == user.id, Task.status == "open")
        .order_by(Task.due_on.asc().nulls_last())
        .limit(8)
        .all()
    )
    overdue_suppliers = (
        db.query(Supplier)
        .filter(Supplier.follow_up_on != None, Supplier.follow_up_on <= date.today())  # noqa: E711
        .order_by(Supplier.follow_up_on.asc())
        .limit(8)
        .all()
    )
    app_suppliers = (
        db.query(Supplier)
        .filter(
            or_(
                Supplier.reseller_application_status.in_(["submitted", "pending", "in_progress"]),
                Supplier.credit_application_status.in_(["submitted", "pending", "in_progress"]),
            )
        )
        .order_by(Supplier.updated_at.desc())
        .limit(8)
        .all()
    )
    recent_suppliers = db.query(Supplier).order_by(Supplier.created_at.desc()).limit(6).all()
    recent_opps = db.query(Opportunity).order_by(Opportunity.created_at.desc()).limit(8).all()
    recent_activity = []
    if user.is_primary_owner or user.role == "owner":
        recent_activity = db.query(AuditEvent).order_by(AuditEvent.at.desc()).limit(10).all()
    return TEMPLATES.TemplateResponse(
        "home.html",
        _ctx(
            request,
            user,
            "home",
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
@app.get("/opportunities", response_class=HTMLResponse)
def opportunities_list(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    if not has_perm(user.role, user.extra_permissions, "opportunities.read"):
        return RedirectResponse("/", status_code=303)
    items = db.query(Opportunity).order_by(Opportunity.created_at.desc()).limit(100).all()
    rows = []
    for o in items:
        item = _opp_item_from_model(o)
        row = enrich_discovery_item(item, saved=True, catalog_matches=_catalog_match_count(db, item))
        row["saved_id"] = str(o.id)
        row["official_url"] = o.official_url
        rows.append(row)
    return TEMPLATES.TemplateResponse(
        "opportunities_list.html",
        _ctx(request, user, "opportunities", rows=rows, opp_tab="saved"),
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
    }
    if profile_id:
        prof = db.get(SearchProfile, profile_id)
        if prof and prof.owner_id == user.id:
            filters.update(prof.filters or {})
            filters["states_raw"] = ",".join(filters.get("states") or [])
            filters["categories_raw"] = ",".join(filters.get("product_categories") or [])
            filters["product_resale_only"] = bool(filters.get("product_resale_only", True))

    ran = any(k in qp for k in ("keyword", "value_bands", "states", "product_categories", "run", "profile", "set_asides"))
    result = {"count": 0, "items": [], "sources_succeeded": [], "limitations": [], "unfiltered_count": 0, "run_id": None}
    if ran:
        for key in ("custom_value_min", "custom_value_max", "days_remaining_max"):
            if filters[key] in ("", None):
                filters[key] = None
            else:
                try:
                    filters[key] = float(filters[key]) if key != "days_remaining_max" else int(filters[key])
                except ValueError:
                    filters[key] = None
        result = run_contract_discovery(db, user_id=user.id, filters=filters, limit=80)
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
            rows=rows,
            run_id=result.get("run_id"),
            return_to=return_to,
            value_options=value_options,
            set_aside_options=set_aside_options,
            opp_tab="filter",
            flash=flash,
        ),
    )


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
            catalog_matches=matches[:20],
            return_to=return_to,
            opp_tab="filter",
        ),
    )


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
    return_to = request.query_params.get("return_to") or "/opportunities"
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
            catalog_matches=matches[:20],
            return_to=return_to,
            opp_tab="saved",
        ),
    )


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
            db, user_id=user.id, filters={"product_categories": cats, "keyword": keyword}, limit=40
        )
    return TEMPLATES.TemplateResponse(
        "suppliers_find.html",
        _ctx(request, user, "suppliers", ran=ran, result=result, categories_raw=categories_raw, keyword=keyword),
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
    return TEMPLATES.TemplateResponse("supplier_detail.html", _ctx(request, user, "suppliers", supplier=supplier))


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


# ---- Settings / users / audit ----
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    can = is_primary_owner(user)
    users = db.query(User).order_by(User.created_at.asc()).all() if can else []
    for u in users:
        u.review_flag = None  # type: ignore[attr-defined]
        if (u.email or "").endswith("@m3.local") or "test" in (u.email or "").lower():
            u.review_flag = "review"  # type: ignore[attr-defined]
    test_accounts = _identify_test_accounts(users) if can else []
    test_records = _identify_test_records(db) if can else []
    return TEMPLATES.TemplateResponse(
        "settings.html",
        _ctx(
            request,
            user,
            "settings",
            can_manage_users=can,
            users=users,
            test_accounts=test_accounts,
            test_records=test_records,
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
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/users")
def settings_create_user(
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    # Primary owner only — not role-delegable
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    if role not in {"supplier_manager", "contract_specialist"}:
        role = "contract_specialist"
    created = create_user(db, email=email, full_name=full_name, password=password, role=role, actor=user)
    # Never grant primary owner via create
    created.is_primary_owner = False
    db.add(created)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/users/{user_id}/toggle")
def settings_toggle_user(user_id: uuid.UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    target = db.get(User, user_id)
    if target and target.id != user.id and not target.is_primary_owner:
        target.is_active = not target.is_active
        db.add(target)
        record_audit(
            db,
            action="user.toggle",
            entity_type="user",
            entity_id=str(target.id),
            actor_user_id=user.id,
            actor_label=user.email,
            after={"is_active": target.is_active},
        )
        db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/users/{user_id}/reset-password")
def settings_reset_password(
    user_id: uuid.UUID,
    password: str = Form(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not is_primary_owner(user):
        return RedirectResponse("/settings", status_code=303)
    target = db.get(User, user_id)
    if target and not target.is_primary_owner and len(password) >= 10:
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
        db.commit()
    return RedirectResponse("/settings", status_code=303)


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
    events = q.limit(200).all()
    return TEMPLATES.TemplateResponse(
        "audit.html", _ctx(request, user, "settings", events=events, actor=actor, action=action)
    )

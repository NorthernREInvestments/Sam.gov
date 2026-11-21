"""M3 2.0 Build 001 — FastAPI application entrypoint."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import date, datetime
from pathlib import Path

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
from m3.models import AuditEvent, CatalogProduct, CatalogUpload, Opportunity, SearchProfile, Supplier, Task, User
from m3.research.engine import run_contract_discovery, run_supplier_discovery
from m3.security import has_perm, issue_session_token
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

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "m3" / "web" / "templates"))


def _b64json(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, default=str).encode("utf-8")).decode("ascii")


TEMPLATES.env.filters["b64json"] = _b64json

app = FastAPI(title="M3 Supply", version="2.0.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "m3" / "web" / "static")), name="static")


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
    open_tasks = db.query(Task).filter(Task.assigned_to == user.id, Task.status == "open").count()
    followups = (
        db.query(Supplier)
        .filter(Supplier.follow_up_on != None, Supplier.follow_up_on <= date.today())  # noqa: E711
        .count()
    )
    saved_opps = db.query(Opportunity).count()
    supplier_count = db.query(Supplier).count()
    attention = []
    if user.role in {"owner", "supplier_manager", "custom"} or has_perm(
        user.role, user.extra_permissions, "suppliers.read"
    ):
        for s in (
            db.query(Supplier)
            .filter(Supplier.follow_up_on != None, Supplier.follow_up_on <= date.today())  # noqa: E711
            .limit(5)
        ):
            attention.append(
                {
                    "title": s.name,
                    "kind": "follow-up",
                    "detail": f"Due {s.follow_up_on}",
                    "href": f"/suppliers/{s.id}",
                }
            )
    for t in db.query(Task).filter(Task.assigned_to == user.id, Task.status == "open").limit(5):
        attention.append({"title": t.title, "kind": "task", "detail": f"Due {t.due_on or '—'}", "href": "/tasks"})
    if user.role == "owner":
        for o in db.query(Opportunity).order_by(Opportunity.created_at.desc()).limit(3):
            attention.append(
                {
                    "title": o.title,
                    "kind": "opportunity",
                    "detail": o.buyer_name or o.geo_state or "Saved",
                    "href": "/opportunities",
                }
            )
    return TEMPLATES.TemplateResponse(
        "home.html",
        _ctx(
            request,
            user,
            "home",
            open_tasks=open_tasks,
            followups=followups,
            attention=attention,
            saved_opps=saved_opps,
            supplier_count=supplier_count,
        ),
    )


# ---- Opportunities ----
@app.get("/opportunities", response_class=HTMLResponse)
def opportunities_list(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    if not has_perm(user.role, user.extra_permissions, "opportunities.read"):
        return TEMPLATES.TemplateResponse(
            "home.html",
            _ctx(request, user, "home", open_tasks=0, followups=0, attention=[], flash={"message": "No access", "kind": "err"}),
        )
    items = db.query(Opportunity).order_by(Opportunity.created_at.desc()).limit(100).all()
    return TEMPLATES.TemplateResponse("opportunities_list.html", _ctx(request, user, "opportunities", items=items))


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
        "include_unknown_value": qp.get("include_unknown_value") == "1" or not qp.get("value_bands"),
        "set_asides": qp.getlist("set_asides"),
        "states": [s.strip().upper() for s in (qp.get("states") or "").split(",") if s.strip()],
        "states_raw": qp.get("states") or "NE",
        "product_categories": [c.strip() for c in (qp.get("product_categories") or "").split(",") if c.strip()],
        "categories_raw": qp.get("product_categories") or "",
        "days_remaining_max": qp.get("days_remaining_max") or None,
    }
    if profile_id:
        prof = db.get(SearchProfile, profile_id)
        if prof and prof.owner_id == user.id:
            filters.update(prof.filters or {})
            filters["states_raw"] = ",".join(filters.get("states") or [])
            filters["categories_raw"] = ",".join(filters.get("product_categories") or [])

    ran = bool(qp.get("keyword") is not None or qp.get("value_bands") or qp.get("run") or profile_id or qp.get("states"))
    # Auto-run when arriving with defaults from nav? Only when explicit query present
    ran = any(k in qp for k in ("keyword", "value_bands", "states", "product_categories", "run", "profile", "set_asides"))
    result = {"count": 0, "items": [], "sources_succeeded": [], "limitations": [], "unfiltered_count": 0}
    if ran:
        # coerce numbers
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
    return TEMPLATES.TemplateResponse(
        "opportunities_find.html",
        _ctx(
            request,
            user,
            "opportunities",
            filters=filters,
            ran=ran,
            result=result,
            value_options=value_options,
            set_aside_options=set_aside_options,
        ),
    )


@app.post("/opportunities/save")
async def opportunities_save(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user or not has_perm(user.role, user.extra_permissions, "opportunities.write"):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    raw = str(form.get("payload") or "")
    try:
        item = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
    except Exception:
        return RedirectResponse("/opportunities/find", status_code=303)
    save_opportunity(db, item, user)
    return RedirectResponse("/opportunities", status_code=303)


@app.get("/opportunities/searches", response_class=HTMLResponse)
def opportunity_searches(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        return _login_redirect()
    profiles = db.query(SearchProfile).filter_by(owner_id=user.id, kind="contracts").order_by(SearchProfile.created_at.desc()).all()
    return TEMPLATES.TemplateResponse("searches.html", _ctx(request, user, "opportunities", profiles=profiles))


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
    can = has_perm(user.role, user.extra_permissions, "users.manage")
    users = db.query(User).order_by(User.created_at.asc()).all() if can else []
    return TEMPLATES.TemplateResponse(
        "settings.html", _ctx(request, user, "settings", can_manage_users=can, users=users)
    )


@app.post("/settings/users")
def settings_create_user(
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if not user or not has_perm(user.role, user.extra_permissions, "users.manage"):
        return RedirectResponse("/login", status_code=303)
    if role not in {"owner", "supplier_manager", "contract_specialist"}:
        role = "contract_specialist"
    create_user(db, email=email, full_name=full_name, password=password, role=role, actor=user)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/users/{user_id}/toggle")
def settings_toggle_user(user_id: uuid.UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user or not has_perm(user.role, user.extra_permissions, "users.manage"):
        return RedirectResponse("/login", status_code=303)
    target = db.get(User, user_id)
    if target and target.id != user.id:
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


@app.get("/settings/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user or not has_perm(user.role, user.extra_permissions, "audit.read"):
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

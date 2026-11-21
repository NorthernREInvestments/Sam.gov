"""Business operations helpers."""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.audit import record_audit
from m3.config import get_settings
from m3.models import CatalogProduct, CatalogUpload, Opportunity, Supplier, SupplierContact, SupplierNote, Task, User
from m3.security import hash_password, verify_password


def normalize_supplier_key(name: str, website: str | None = None) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    host = ""
    if website:
        host = re.sub(r"^https?://(www\.)?", "", website.lower()).split("/")[0]
    return f"{base}|{host}"[:500]


def find_user_by_email(db: Session, email: str) -> User | None:
    return db.query(User).filter(User.email == email.lower().strip()).one_or_none()


def create_user(
    db: Session,
    *,
    email: str,
    full_name: str,
    password: str,
    role: str,
    actor: User | None = None,
) -> User:
    user = User(
        email=email.lower().strip(),
        full_name=full_name.strip(),
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        extra_permissions={},
    )
    db.add(user)
    db.flush()
    record_audit(
        db,
        action="user.create",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=actor.id if actor else None,
        actor_label=actor.email if actor else "system:bootstrap",
        after={"email": user.email, "role": user.role},
        origin="manual" if actor else "automated",
    )
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = find_user_by_email(db, email)
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.utcnow()
    db.add(user)
    record_audit(
        db,
        action="user.login",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=user.id,
        actor_label=user.email,
        origin="manual",
    )
    db.commit()
    return user


def save_opportunity(
    db: Session,
    item: dict[str, Any],
    user: User,
    *,
    data_origin: str = "operator",
) -> Opportunity:
    from m3.research.enrich import enrich_opportunity_fields
    from m3.web.titles import build_display_title

    enriched = enrich_opportunity_fields(item)
    key = str(enriched.get("external_key") or "")
    existing = db.query(Opportunity).filter_by(external_key=key).one_or_none() if key else None
    deadline = None
    if enriched.get("deadline_at"):
        try:
            raw = str(enriched["deadline_at"]).replace("Z", "+00:00")
            try:
                deadline = datetime.fromisoformat(raw)
            except ValueError:
                deadline = datetime.fromisoformat(raw[:19])
        except Exception:
            deadline = None
    titles = build_display_title(
        official_title=enriched.get("title"),
        buyer_name=enriched.get("buyer_name"),
        solicitation_number=enriched.get("solicitation_number"),
    )
    payload = {
        "title": enriched.get("title"),
        "display_title": titles["display_title"],
        "solicitation_number": enriched.get("solicitation_number"),
        "buyer_name": enriched.get("buyer_name"),
        "geo_state": enriched.get("geo_state"),
        "geo_location": enriched.get("geo_location"),
        "deadline_at": deadline,
        "procurement_type": enriched.get("procurement_type"),
        "product_category": enriched.get("product_category"),
        "set_aside": enriched.get("set_aside"),
        "set_aside_verified": bool(enriched.get("set_aside_verified")),
        "estimated_value_min": enriched.get("estimated_value_min"),
        "estimated_value_max": enriched.get("estimated_value_max"),
        "estimated_value_unknown": bool(enriched.get("estimated_value_unknown", True)),
        "value_estimate_method": enriched.get("value_estimate_method"),
        "value_evidence": enriched.get("value_evidence") or {},
        "official_url": enriched.get("official_url"),
        "documents_available": enriched.get("documents_available") or "unknown",
        "source_key": enriched.get("source_key"),
        "source_verified_at": datetime.utcnow(),
        "requirement_class": enriched.get("requirement_class"),
        "quantity": enriched.get("quantity"),
        "status": "saved",
        "data_origin": data_origin if data_origin in {"operator", "acceptance_harness", "ambiguous"} else "operator",
        "raw_evidence": {
            **(enriched.get("evidence") or {}),
            "classification_evidence": enriched.get("classification_evidence"),
            "value_evidence_class": enriched.get("value_evidence_class"),
            "value_evidence": enriched.get("value_evidence"),
            "deadline_timezone": enriched.get("deadline_timezone"),
            "geo_city": enriched.get("geo_city"),
            "field_status": enriched.get("field_status"),
            "unavailable_reasons": enriched.get("unavailable_reasons"),
            "official_title": enriched.get("title"),
            "display_title": titles["display_title"],
        },
    }
    if existing:
        before = {"title": existing.title, "status": existing.status}
        for k, v in payload.items():
            if k == "data_origin" and existing.data_origin == "operator":
                continue  # never downgrade operator provenance
            if k == "status":
                continue  # preserve lifecycle status on re-save
            setattr(existing, k, v)
        db.add(existing)
        record_audit(
            db,
            action="opportunity.update",
            entity_type="opportunity",
            entity_id=str(existing.id),
            actor_user_id=user.id,
            actor_label=user.email,
            before=before,
            after={"title": existing.title, "display_title": existing.display_title},
        )
        db.commit()
        return existing
    opp = Opportunity(external_key=key or str(uuid.uuid4()), saved_by=user.id, **payload)
    db.add(opp)
    db.flush()
    record_audit(
        db,
        action="opportunity.save",
        entity_type="opportunity",
        entity_id=str(opp.id),
        actor_user_id=user.id,
        actor_label=user.email,
        after={"title": opp.title, "display_title": opp.display_title, "url": opp.official_url, "data_origin": opp.data_origin},
    )
    db.commit()
    db.refresh(opp)
    return opp


def save_supplier_from_discovery(db: Session, item: dict[str, Any], user: User) -> tuple[Supplier, bool]:
    key = normalize_supplier_key(item.get("name") or "", item.get("website"))
    existing = db.query(Supplier).filter_by(normalized_key=key).one_or_none()
    if existing:
        return existing, False
    # Do not invent Net-30 or application progress — leave statuses at Not Started / empty terms
    phone = item.get("phone") or item.get("general_phone")
    email = item.get("email") or item.get("general_email")
    address = item.get("business_address") or item.get("address")
    # Only store company contact when source provided it (never invent)
    origin = item.get("data_origin") or "operator"
    sup = Supplier(
        name=item.get("name") or "Unknown",
        website=item.get("website"),
        business_address=address,
        address_type=item.get("address_type"),
        general_phone=phone,
        general_email=email,
        normalized_key=key,
        supplier_type=item.get("supplier_type"),
        product_categories=list(item.get("product_categories") or []),
        brands=list(item.get("brands") or []),
        geo_coverage=item.get("geo_coverage"),
        reseller_application_url=item.get("reseller_application_url"),
        credit_application_url=item.get("credit_application_url"),
        catalog_available=item.get("catalog_available") or "unknown",
        verification_status=item.get("verification_status") or "unverified",
        source_urls=list(item.get("source_urls") or []),
        next_action=item.get("next_action"),
        payment_terms=None,  # unknown until confirmed — never default Net-30
        created_by=user.id,
        assigned_to=user.id if user.role == "supplier_manager" else None,
        relationship_status="lead",
        data_origin=origin if origin in {"operator", "acceptance_harness", "ambiguous"} else "operator",
    )
    db.add(sup)
    db.flush()
    # Individual sales contacts stay empty until an operator adds them after communication.
    # Company-level phone/email already stored on Supplier when provided by the source.
    record_audit(
        db,
        action="supplier.create",
        entity_type="supplier",
        entity_id=str(sup.id),
        actor_user_id=user.id,
        actor_label=user.email,
        after={"name": sup.name, "website": sup.website},
    )
    db.commit()
    db.refresh(sup)
    return sup, True


def parse_catalog_csv(content: bytes) -> list[dict[str, Any]]:
    text = content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for i, row in enumerate(reader):
        if not row:
            continue
        lower = {str(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        def pick(*names: str) -> str | None:
            for n in names:
                if lower.get(n):
                    return lower[n]
            return None
        price_raw = pick("price", "published_price", "unit_price", "list_price")
        price = None
        uncertain = False
        if price_raw:
            m = re.search(r"([\d,.]+)", price_raw.replace("$", ""))
            if m:
                try:
                    price = float(m.group(1).replace(",", ""))
                except ValueError:
                    uncertain = True
            else:
                uncertain = True
        mpn = pick("mpn", "manufacturer_part_number", "part_number", "part #", "mfr part")
        rows.append(
            {
                "row_index": i,
                "manufacturer": pick("manufacturer", "mfr", "mfg"),
                "brand": pick("brand"),
                "mpn": mpn,
                "supplier_sku": pick("sku", "supplier_sku", "item", "item_number"),
                "description": pick("description", "product", "item_description", "title"),
                "unit_of_measure": pick("uom", "unit", "unit_of_measure"),
                "package_qty": pick("package", "pkg_qty", "pack"),
                "published_price": price,
                "price_basis": pick("price_basis", "per") or "each",
                "currency": pick("currency") or "USD",
                "availability": pick("availability", "stock"),
                "extraction_uncertain": uncertain or not (mpn or pick("description", "product")),
                "raw": dict(row),
            }
        )
    return rows


def ingest_catalog(
    db: Session,
    *,
    supplier: Supplier,
    user: User,
    filename: str,
    content: bytes,
    content_type: str,
) -> CatalogUpload:
    settings = get_settings()
    root = settings.data_root() / "uploads" / "catalogs"
    root.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9._\-]+", "_", filename)[:180]
    stored = root / f"{uuid.uuid4().hex}_{safe_name}"
    stored.write_bytes(content)

    upload = CatalogUpload(
        supplier_id=supplier.id,
        uploaded_by=user.id,
        original_filename=filename,
        stored_path=str(stored),
        content_type=content_type or "application/octet-stream",
        status="processing",
    )
    db.add(upload)
    db.flush()

    products: list[dict] = []
    if filename.lower().endswith(".csv") or "csv" in (content_type or ""):
        products = parse_catalog_csv(content)
    elif filename.lower().endswith((".xlsx", ".xls")):
        try:
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            headers = [str(c.value or "").strip().lower() for c in next(ws.iter_rows(max_row=1))]
            for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True)):
                mapped = {headers[j]: row[j] for j in range(min(len(headers), len(row)))}
                # reuse csv mapper via fake csv
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=list(mapped.keys()))
                w.writeheader()
                w.writerow({k: "" if v is None else str(v) for k, v in mapped.items()})
                parsed = parse_catalog_csv(buf.getvalue().encode("utf-8"))
                if parsed:
                    p = parsed[0]
                    p["row_index"] = i
                    products.append(p)
        except Exception:
            upload.status = "needs_review"
            upload.notes = "Excel parse failed — file stored for manual review"
            db.add(upload)
            db.commit()
            return upload
    else:
        upload.status = "needs_review"
        upload.notes = "PDF/other stored; structured extraction limited in Build 001"
        db.add(upload)
        record_audit(
            db,
            action="catalog.upload",
            entity_type="catalog_upload",
            entity_id=str(upload.id),
            actor_user_id=user.id,
            actor_label=user.email,
            after={"filename": filename, "status": upload.status},
        )
        db.commit()
        return upload

    uncertain = 0
    for p in products:
        if p.get("extraction_uncertain"):
            uncertain += 1
        db.add(
            CatalogProduct(
                catalog_upload_id=upload.id,
                supplier_id=supplier.id,
                row_index=int(p["row_index"]),
                manufacturer=p.get("manufacturer"),
                brand=p.get("brand"),
                mpn=p.get("mpn"),
                supplier_sku=p.get("supplier_sku"),
                description=p.get("description"),
                unit_of_measure=p.get("unit_of_measure"),
                package_qty=p.get("package_qty"),
                published_price=p.get("published_price"),
                price_basis=p.get("price_basis"),
                currency=p.get("currency") or "USD",
                availability=p.get("availability"),
                extraction_uncertain=bool(p.get("extraction_uncertain")),
                raw=p.get("raw") or {},
            )
        )
    upload.product_count = len(products)
    upload.uncertain_count = uncertain
    upload.status = "imported"
    db.add(upload)
    record_audit(
        db,
        action="catalog.import",
        entity_type="catalog_upload",
        entity_id=str(upload.id),
        actor_user_id=user.id,
        actor_label=user.email,
        after={"products": len(products), "uncertain": uncertain},
    )
    db.commit()
    db.refresh(upload)
    return upload


def exact_mpn_matches(db: Session, mpn: str) -> list[CatalogProduct]:
    q = (mpn or "").strip()
    if not q:
        return []
    return db.query(CatalogProduct).filter(CatalogProduct.mpn.ilike(q)).limit(50).all()

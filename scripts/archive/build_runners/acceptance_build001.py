"""Build 001 commercial acceptance harness (live sources only — no fixtures)."""

from __future__ import annotations

from datetime import date, timedelta

from m3.db import SessionLocal
from m3.models import AuditEvent, CatalogProduct, Opportunity, SearchProfile, Supplier, Task, User
from m3.research.engine import run_contract_discovery, run_supplier_discovery
from m3.research.filters import apply_contract_filters
from m3.security import has_perm
from m3.services import (
    create_user,
    exact_mpn_matches,
    find_user_by_email,
    ingest_catalog,
    save_opportunity,
    save_supplier_from_discovery,
)


def main() -> None:
    db = SessionLocal()
    owner = db.query(User).filter_by(role="owner").first()
    assert owner, "owner missing"
    sm = find_user_by_email(db, "sam.supplier@m3.local")
    if not sm:
        sm = create_user(
            db,
            email="sam.supplier@m3.local",
            full_name="Sam Supplier",
            password="ChangeMe-Supplier-001!",
            role="supplier_manager",
            actor=owner,
        )

    print("=== MULTI_USER ===")
    print("owner", owner.email, "perms.users.manage", has_perm(owner.role, owner.extra_permissions, "users.manage"))
    print("sm", sm.email, "perms.users.manage", has_perm(sm.role, sm.extra_permissions, "users.manage"))
    print("sm suppliers.write", has_perm(sm.role, sm.extra_permissions, "suppliers.write"))
    assert has_perm(owner.role, owner.extra_permissions, "users.manage")
    assert not has_perm(sm.role, sm.extra_permissions, "users.manage")

    task = Task(
        title="Call Graybar about reseller account",
        body="Confirm credit application path",
        assigned_to=sm.id,
        created_by=owner.id,
        status="open",
        due_on=date.today() + timedelta(days=3),
        entity_type="supplier",
    )
    db.add(task)
    db.commit()
    print("task_assigned", task.id, "to", sm.email)

    print("=== CONTRACTS ===")
    contracts = run_contract_discovery(
        db,
        user_id=owner.id,
        filters={"include_unknown_value": True, "value_bands": ["unknown", "under_5k", "5k_15k", "15k_50k", "50k_150k", "150k_500k", "500k_1m", "over_1m"]},
        limit=120,
    )
    live = [i for i in contracts["items"] if not i.get("is_forecast")]
    print("live_count", len(live), "sources", contracts["sources_succeeded"])
    # Filter demos
    ca = apply_contract_filters(live, {"states": ["CA"], "include_unknown_value": True, "value_bands": ["unknown"]})
    print("filter_CA_unknown_value", len(ca))
    multi = apply_contract_filters(
        live,
        {
            "states": ["CA", "DE"],
            "value_bands": ["unknown", "15k_50k", "50k_150k"],
            "include_unknown_value": True,
        },
    )
    print("filter_CA_or_DE_and_value", len(multi))
    # Build 001B: do NOT bulk-save discovery into production CRM.
    # Explicit operator Save Opportunity is required. Harness may mark isolated samples only.
    print("skip_bulk_opportunity_save", "use Filter + Save Opportunity in UI")
    # Optional single sample under acceptance_harness origin for isolated tests:
    # if live: save_opportunity(db, live[0], owner, data_origin="acceptance_harness")
    # Save search profile
    db.add(
        SearchProfile(
            name="CA commodities open",
            owner_id=owner.id,
            kind="contracts",
            filters={"states": ["CA"], "value_bands": ["unknown"], "include_unknown_value": True},
        )
    )
    db.commit()
    print("saved_opportunities", db.query(Opportunity).count(), "search_profiles", db.query(SearchProfile).count())

    print("=== SUPPLIERS ===")
    sups = run_supplier_discovery(db, user_id=sm.id, filters={}, limit=80)
    print("live_supplier_count", sups["count"])
    # Build 001B: discovery must not auto-create CRM relationships.
    print("skip_bulk_supplier_save", "use Save to CRM explicitly in UI")
    for s in (sups["items"] or [])[:0]:
        s = dict(s)
        s["data_origin"] = "acceptance_harness"
        save_supplier_from_discovery(db, s, sm)
    # CRM demo statuses
    gray = db.query(Supplier).filter(Supplier.name.ilike("%graybar%")).first()
    if gray:
        gray.relationship_status = "prospect"
        gray.reseller_application_status = "in_progress"
        gray.credit_application_status = "not_started"
        gray.account_approval_status = "pending"
        gray.dealer_authorization_status = "not_started"
        gray.follow_up_on = date.today()
        gray.assigned_to = sm.id
        db.add(gray)
        db.commit()
        print("crm_graybar_statuses", gray.reseller_application_status, gray.credit_application_status)

    print("=== CATALOG ===")
    target = gray or db.query(Supplier).first()
    csv = (
        b"manufacturer,mpn,sku,description,price,uom\n"
        b"Acme,ABC-123,S1,Widget Bolt Pack,12.50,EA\n"
        b"Acme,ABC-124,S2,Widget Nut,3.25,EA\n"
        b"Acme,XYZ-999,S3,Ambiguous Row,,EA\n"
    )
    up = ingest_catalog(db, supplier=target, user=sm, filename="acceptance.csv", content=csv, content_type="text/csv")
    print("catalog", up.status, "products", up.product_count, "uncertain", up.uncertain_count)
    matches = exact_mpn_matches(db, "ABC-123")
    print("exact_mpn_ABC-123", len(matches), matches[0].description if matches else None)

    print("=== AUDIT ===")
    owner_events = db.query(AuditEvent).filter(AuditEvent.actor_user_id == owner.id).count()
    sm_events = db.query(AuditEvent).filter(AuditEvent.actor_user_id == sm.id).count()
    system_events = db.query(AuditEvent).filter(AuditEvent.actor_label.like("system:%")).count()
    print("audit_owner", owner_events, "audit_sm", sm_events, "audit_system", system_events)
    print("tasks_open_for_sm", db.query(Task).filter_by(assigned_to=sm.id, status="open").count())
    print("DONE")
    db.close()


if __name__ == "__main__":
    main()

"""Identify development/acceptance pollution vs operator records. Never silent-delete ambiguous."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from m3.models import AuditEvent, Opportunity, Supplier, User
from m3.research.seeds import SEED_SOURCES

# Distributor names seeded into source_registry and often bulk-saved by acceptance_build001
_SEED_SUPPLIER_NAMES = {
    (s.get("name") or "").strip().lower()
    for s in SEED_SOURCES
    if (s.get("adapter") == "supplier_page" or (s.get("source_key") or "").startswith("supplier_"))
}

_NOISE_TITLES = {"word", "excel", "external", "form", "pdf", "doc", "docx", "xlsx", "xls"}


def classify_opportunity_origin(o: Opportunity, audit_labels: set[str] | None = None) -> dict[str, Any]:
    title = (o.title or "").strip()
    low = title.lower()
    reasons: list[str] = []
    confidence = "ambiguous"

    if low in _NOISE_TITLES or low.startswith("form ") or low in {"click here", "download"}:
        reasons.append("portal template/link noise title")
        confidence = "confirmed_test"
    if o.requirement_class in {"services", "construction"} and (o.status or "saved") == "saved":
        reasons.append("service/construction class retained as saved (likely bulk acceptance save)")
        if confidence != "confirmed_test":
            confidence = "likely_test"
    if audit_labels and "acceptance" in " ".join(audit_labels).lower():
        reasons.append("audit references acceptance harness")
        confidence = "confirmed_test"

    # Bulk acceptance often left notes empty and value unknown with short titles from HTML scrapes
    if not reasons and o.saved_by and o.source_key and (o.notes or "") == "":
        # Not enough alone
        pass

    if not reasons:
        return {
            "id": str(o.id),
            "kind": "opportunity",
            "label": title[:120],
            "confidence": "operator_or_ambiguous",
            "reasons": ["no strong test evidence — preserve"],
            "actionable": False,
        }
    return {
        "id": str(o.id),
        "kind": "opportunity",
        "label": title[:120],
        "confidence": confidence,
        "reasons": reasons,
        "actionable": confidence in {"confirmed_test", "likely_test"},
        "status": o.status,
        "requirement_class": o.requirement_class,
    }


def classify_supplier_origin(s: Supplier) -> dict[str, Any]:
    name = (s.name or "").strip()
    low = name.lower()
    reasons: list[str] = []
    confidence = "ambiguous"

    if "sample" in low or "test" in low or "demo" in low:
        reasons.append("test/demo naming")
        confidence = "confirmed_test"
    if low in _SEED_SUPPLIER_NAMES or any(low == n or n in low for n in _SEED_SUPPLIER_NAMES if n):
        # Seed distributors bulk-imported by acceptance harness are not established CRM relationships
        # unless operator later advanced status beyond lead/not_started
        advanced = any(
            getattr(s, f) not in {None, "", "not_started", "lead", "unknown"}
            for f in (
                "reseller_application_status",
                "credit_application_status",
                "account_approval_status",
                "payment_terms",
            )
        ) or bool(s.follow_up_on) or bool((s.next_action or "").strip() and s.next_action != "Confirm reseller/credit path and record contacts")
        if not advanced:
            reasons.append("matches free-source distributor seed; still lead with no confirmed applications")
            confidence = "likely_test"
        else:
            reasons.append("seed-named supplier but operator progress detected — preserve")
            confidence = "operator_or_ambiguous"

    if not reasons:
        return {
            "id": str(s.id),
            "kind": "supplier",
            "label": name[:120],
            "confidence": "operator_or_ambiguous",
            "reasons": ["no strong test evidence — preserve"],
            "actionable": False,
        }
    return {
        "id": str(s.id),
        "kind": "supplier",
        "label": name[:120],
        "confidence": confidence,
        "reasons": reasons,
        "actionable": confidence in {"confirmed_test", "likely_test"},
        "relationship_status": s.relationship_status,
    }


def classify_user_origin(u: User) -> dict[str, Any] | None:
    if u.is_primary_owner:
        return None
    email = (u.email or "").lower()
    reasons = []
    if email.endswith("@m3.local") or email.endswith(".local"):
        reasons.append("local test domain")
    if "test" in email or "demo" in email or email.startswith("sam.supplier"):
        reasons.append("test/demo naming")
    if not reasons:
        return None
    return {
        "id": str(u.id),
        "kind": "user",
        "label": u.email,
        "full_name": u.full_name,
        "role": u.role,
        "is_active": u.is_active,
        "confidence": "confirmed_test",
        "reasons": reasons,
        "actionable": True,
    }


def build_cleanup_preview(db: Session) -> dict[str, Any]:
    opps = []
    for o in db.query(Opportunity).order_by(Opportunity.created_at.desc()).limit(500):
        c = classify_opportunity_origin(o)
        if c["actionable"]:
            opps.append(c)
    sups = []
    for s in db.query(Supplier).order_by(Supplier.created_at.desc()).limit(500):
        c = classify_supplier_origin(s)
        if c["actionable"]:
            sups.append(c)
    users = []
    for u in db.query(User).order_by(User.created_at.asc()).all():
        c = classify_user_origin(u)
        if c:
            users.append(c)
    return {
        "opportunities": opps,
        "suppliers": sups,
        "users": users,
        "counts": {
            "opportunities": len(opps),
            "suppliers": len(sups),
            "users": len(users),
        },
    }


ACTIVE_OPP_STATUSES = {"saved", "pursuing"}

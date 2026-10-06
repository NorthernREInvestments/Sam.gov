"""Financing source profiles + verified rules."""

from __future__ import annotations

from typing import Any

from financing_intelligence.constants import (
    BUILD,
    FACT_APPROVED,
    PREF_NEUTRAL,
    SRC_OTHER,
    UNKNOWN,
)
from financing_intelligence.store import (
    load_facts,
    load_sources,
    money_str,
    new_id,
    save_sources,
    _utc,
)


def upsert_source(payload: dict[str, Any]) -> dict[str, Any]:
    doc = load_sources()
    items = doc.setdefault("items", [])
    sid = payload.get("source_id")
    existing = None
    if sid:
        for it in items:
            if it.get("source_id") == sid:
                existing = it
                break
    if existing is None:
        existing = {
            "source_id": new_id("FS"),
            "created_at": _utc(),
            "active": True,
            "preference": PREF_NEUTRAL,
            "source_type": SRC_OTHER,
            "terms": {},
            "history": [],
        }
        items.append(existing)
    for key in (
        "company_name",
        "website",
        "contact_name",
        "phone",
        "email",
        "source_type",
        "notes",
        "active",
        "preference",
        "date_first_researched",
        "last_verified_date",
    ):
        if key in payload and payload[key] is not None:
            existing[key] = payload[key]
    if "terms" in payload and isinstance(payload["terms"], dict):
        terms = dict(existing.get("terms") or {})
        terms.update(payload["terms"])
        existing["terms"] = terms
        existing.setdefault("history", []).append(
            {"at": _utc(), "action": "terms_updated", "keys": list(payload["terms"].keys())}
        )
    existing["updated_at"] = _utc()
    existing["build"] = BUILD
    save_sources(doc)
    return existing


def list_sources(*, active_only: bool = False) -> list[dict[str, Any]]:
    items = list(load_sources().get("items") or [])
    if active_only:
        items = [i for i in items if i.get("active", True)]
    return items


def get_source(source_id: str) -> dict[str, Any] | None:
    for it in list_sources():
        if it.get("source_id") == source_id:
            return it
    return None


def approved_rules_for_source(source_id: str) -> dict[str, Any]:
    """Merge source.terms with APPROVED facts (facts win when newer evidence)."""
    src = get_source(source_id) or {}
    rules = dict(src.get("terms") or {})
    for f in load_facts().get("items") or []:
        if f.get("source_id") != source_id:
            continue
        if f.get("status") != FACT_APPROVED:
            continue
        field = f.get("field")
        if field:
            rules[field] = f.get("value")
            rules[f"{field}__evidence"] = {
                "evidence_type": f.get("evidence_type"),
                "snippet": f.get("source_snippet"),
                "verified_at": f.get("decided_at"),
                "fact_id": f.get("fact_id"),
            }
    rules.setdefault("personal_guarantee", UNKNOWN)
    rules.setdefault("personal_credit", UNKNOWN)
    return rules


def source_display(src: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": src.get("source_id"),
        "company_name": src.get("company_name") or "Unnamed source",
        "source_type": src.get("source_type"),
        "preference": src.get("preference"),
        "active": src.get("active", True),
        "contact_name": src.get("contact_name"),
        "phone": src.get("phone"),
        "email": src.get("email"),
        "last_verified_date": src.get("last_verified_date"),
        "terms_keys": sorted((src.get("terms") or {}).keys()),
    }

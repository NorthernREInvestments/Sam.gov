"""BUYER_PACKAGE_SOURCE_MEMORY + portal family learning."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from package_recovery_sam_budget.models import BUILD, BUYER_MEMORY, PORTAL_FAMILY


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: Any) -> None:
    data_path(name).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def remember_buyer_package_source(
    *,
    buyer: str,
    portal: str,
    source_system: str,
    package_url_pattern: str | None = None,
    auth_required: bool = False,
    success: bool = True,
) -> None:
    mem = _load(BUYER_MEMORY)
    if not mem:
        mem = {"kind": "BUYER_PACKAGE_SOURCE_MEMORY", "build": BUILD, "by_buyer": {}}
    key = (buyer or "UNKNOWN").strip().lower()[:120]
    row = mem.setdefault("by_buyer", {}).setdefault(
        key,
        {
            "buyer": buyer,
            "preferred_portal": portal,
            "source_system": source_system,
            "package_url_pattern": package_url_pattern,
            "auth_required": auth_required,
            "success_count": 0,
            "fail_count": 0,
            "success_rate": 0.0,
        },
    )
    if success:
        row["success_count"] = int(row.get("success_count") or 0) + 1
        row["preferred_portal"] = portal or row.get("preferred_portal")
        row["source_system"] = source_system or row.get("source_system")
        if package_url_pattern:
            row["package_url_pattern"] = package_url_pattern
    else:
        row["fail_count"] = int(row.get("fail_count") or 0) + 1
    total = int(row["success_count"]) + int(row["fail_count"])
    row["success_rate"] = round(int(row["success_count"]) / total, 4) if total else 0.0
    row["last_verified"] = now_utc().isoformat()
    row["auth_required"] = auth_required
    mem["updated_at"] = now_utc().isoformat()
    mem["build"] = BUILD
    _save(BUYER_MEMORY, mem)


def record_portal_family(
    *,
    family: str,
    success: bool,
    public_free: bool = True,
    auth_required: bool = False,
    failure_mode: str | None = None,
) -> None:
    store = _load(PORTAL_FAMILY)
    if not store:
        store = {"kind": "PORTAL_FAMILY_LEARNING", "build": BUILD, "by_family": {}}
    fam = store.setdefault("by_family", {}).setdefault(
        family or "unknown",
        {
            "family": family,
            "attempts": 0,
            "successes": 0,
            "package_access_rate": 0.0,
            "public_free": public_free,
            "auth_required": auth_required,
            "failure_modes": {},
        },
    )
    fam["attempts"] = int(fam.get("attempts") or 0) + 1
    if success:
        fam["successes"] = int(fam.get("successes") or 0) + 1
    elif failure_mode:
        fm = fam.setdefault("failure_modes", {})
        fm[failure_mode] = int(fm.get(failure_mode) or 0) + 1
    fam["package_access_rate"] = round(int(fam["successes"]) / max(int(fam["attempts"]), 1), 4)
    fam["public_free"] = public_free
    fam["auth_required"] = auth_required or fam.get("auth_required")
    store["updated_at"] = now_utc().isoformat()
    _save(PORTAL_FAMILY, store)


def top_buyer_sources(limit: int = 20) -> list[dict[str, Any]]:
    mem = _load(BUYER_MEMORY)
    rows = list((mem.get("by_buyer") or {}).values())
    rows.sort(key=lambda r: (-float(r.get("success_rate") or 0), -int(r.get("success_count") or 0)))
    return rows[:limit]

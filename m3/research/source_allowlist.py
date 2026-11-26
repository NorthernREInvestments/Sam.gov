"""Owner-approved procurement source allowlist (Build 002Q controlled reset).

A discovered source is NOT an approved source.
No source may execute production acquisition without explicit owner approval.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.models import SourceRegistry

# Seed allowlist: NextBid is the paid baseline inventory source.
# live_acquisition=False while emergency lock is engaged — offline recovery only.
# Free sources are added only when the owner provides verified acquisition details.
DEFAULT_ALLOWLIST: dict[str, Any] = {
    "version": 1,
    "updated_at": None,
    "discovery_auto_activation": False,
    "openai_source_discovery": False,
    "socrata_catalog_discover": False,
    "sources": {
        "nextbid_state_local": {
            "approved": True,
            "live_acquisition": False,
            "role": "paid_baseline_inventory",
            "purchasing_authority": "NextBid aggregated state/local (paid)",
            "acquisition_url": "https://api.nextbid.us/v1",
            "acquisition_method": "paid_api_offline_inventory_reuse",
            "document_access": "stored_packages_and_checkpoint",
            "pagination": "api_search_when_owner_unlocks",
            "access_cost": "paid_nextbid",
            "restrictions": "NEXTBID_ENABLED=false; emergency stop; zero metered calls until owner unlock",
            "approved_by": "owner_directive_002q_controlled_reset",
            "approved_at": "2026-11-22",
        }
    },
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def allowlist_path() -> Path:
    p = get_settings().data_root() / "config" / "owner_source_allowlist.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_allowlist() -> dict[str, Any]:
    path = allowlist_path()
    if not path.exists():
        data = deepcopy(DEFAULT_ALLOWLIST)
        data["updated_at"] = _utc()
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = deepcopy(DEFAULT_ALLOWLIST)
        data["updated_at"] = _utc()
    # Ensure required policy flags exist
    data.setdefault("discovery_auto_activation", False)
    data.setdefault("openai_source_discovery", False)
    data.setdefault("socrata_catalog_discover", False)
    data.setdefault("sources", {})
    return data


def save_allowlist(data: dict[str, Any]) -> Path:
    path = allowlist_path()
    data = dict(data)
    data["updated_at"] = _utc()
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return path


def owner_approved_entry(source_key: str) -> dict[str, Any] | None:
    key = (source_key or "").strip()
    if not key:
        return None
    entry = (load_allowlist().get("sources") or {}).get(key)
    if not entry or not entry.get("approved"):
        return None
    return dict(entry)


def is_owner_approved(source_key: str) -> bool:
    return owner_approved_entry(source_key) is not None


def can_execute_live_acquisition(source_key: str) -> bool:
    """True only when owner approved AND live_acquisition explicitly true."""
    entry = owner_approved_entry(source_key)
    if not entry:
        return False
    return bool(entry.get("live_acquisition"))


def discovery_auto_activation_allowed() -> bool:
    return bool(load_allowlist().get("discovery_auto_activation"))


def openai_source_discovery_allowed() -> bool:
    return bool(load_allowlist().get("openai_source_discovery"))


def socrata_catalog_discover_allowed() -> bool:
    return bool(load_allowlist().get("socrata_catalog_discover"))


def is_discovered_source_key(source_key: str) -> bool:
    return str(source_key or "").startswith("discovered_")


def apply_allowlist_to_registry(db: Session) -> dict[str, Any]:
    """Disable all sources that are not owner-approved for live acquisition.

    Preserves connector rows and historical data. Discovered connectors stay disabled.
    NextBid may remain is_enabled for registry presence but live calls stay fail-closed.
    """
    allow = load_allowlist()
    sources_cfg = allow.get("sources") or {}
    disabled: list[dict[str, Any]] = []
    kept_enabled: list[str] = []
    rows = db.query(SourceRegistry).all()
    for row in rows:
        key = row.source_key or ""
        entry = sources_cfg.get(key) if sources_cfg.get(key, {}).get("approved") else None
        cfg = dict(row.config or {})
        cfg["owner_allowlist"] = {
            "checked_at": _utc(),
            "approved": bool(entry),
            "live_acquisition": bool(entry.get("live_acquisition")) if entry else False,
            "role": (entry or {}).get("role") or (
                "discovered_unapproved"
                if is_discovered_source_key(key)
                else "not_on_allowlist"
            ),
        }
        # Never auto-keep discovered sources enabled
        if is_discovered_source_key(key) or cfg.get("discovered"):
            cfg["discovered"] = True
            cfg["operational"] = False
            if row.is_enabled:
                row.is_enabled = False
                disabled.append({"source_key": key, "reason": "discovered_not_owner_approved"})
            row.config = cfg
            db.add(row)
            continue

        if entry and entry.get("live_acquisition"):
            row.is_enabled = True
            cfg["operational"] = True
            cfg["owner_approved"] = True
            kept_enabled.append(key)
        elif entry and not entry.get("live_acquisition"):
            # Approved for inventory/offline only — keep registry row, disable live pull
            row.is_enabled = False
            cfg["operational"] = False
            cfg["owner_approved"] = True
            cfg["offline_inventory_only"] = True
            if key not in [d["source_key"] for d in disabled]:
                disabled.append({"source_key": key, "reason": "approved_offline_inventory_only"})
        else:
            if row.is_enabled:
                disabled.append({"source_key": key, "reason": "not_on_owner_allowlist"})
            row.is_enabled = False
            cfg["operational"] = False
            cfg["owner_approved"] = False

        # Catalog discover adapter never runs under controlled reset
        if (row.adapter or "") == "socrata_catalog_discover":
            row.is_enabled = False
            cfg["operational"] = False
            cfg["quarantine_reason"] = cfg.get("quarantine_reason") or "catalog_discover_disabled_by_allowlist"

        row.config = cfg
        db.add(row)

    db.commit()
    enabled = (
        db.query(SourceRegistry)
        .filter(SourceRegistry.is_enabled.is_(True))
        .all()
    )
    return {
        "allowlist_path": str(allowlist_path()),
        "approved_keys": sorted(k for k, v in sources_cfg.items() if v.get("approved")),
        "live_acquisition_keys": sorted(
            k for k, v in sources_cfg.items() if v.get("approved") and v.get("live_acquisition")
        ),
        "disabled_count": len(disabled),
        "disabled_samples": disabled[:50],
        "enabled_after": [r.source_key for r in enabled],
        "enabled_count": len(enabled),
        "discovery_auto_activation": allow.get("discovery_auto_activation"),
        "openai_source_discovery": allow.get("openai_source_discovery"),
        "socrata_catalog_discover": allow.get("socrata_catalog_discover"),
    }


def filter_sources_for_acquisition(rows: list[SourceRegistry]) -> list[SourceRegistry]:
    """Production acquisition may only use owner-approved live sources."""
    return [r for r in rows if can_execute_live_acquisition(r.source_key or "")]


def verify_unapproved_cannot_acquire(db: Session) -> dict[str, Any]:
    """Assert no unapproved / discovered source is enabled for acquisition."""
    enabled = db.query(SourceRegistry).filter(SourceRegistry.is_enabled.is_(True)).all()
    violations: list[dict[str, Any]] = []
    for row in enabled:
        key = row.source_key or ""
        if is_discovered_source_key(key) or (row.config or {}).get("discovered"):
            violations.append({"source_key": key, "reason": "discovered_still_enabled"})
            continue
        if not can_execute_live_acquisition(key):
            violations.append({"source_key": key, "reason": "enabled_without_live_allowlist"})
    return {
        "ok": len(violations) == 0,
        "enabled_count": len(enabled),
        "violations": violations,
        "live_allowed": [
            r.source_key for r in enabled if can_execute_live_acquisition(r.source_key or "")
        ],
    }

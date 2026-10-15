"""Shared access registry — reuse known PG/paid barriers without re-navigating.

Accuracy rules:
- Exact solicitation keys are always safe to reuse.
- Buyer→PG hints require consistent observations (same PG for a buyer).
- Never invent a barrier when scope is uncertain.
- REGISTERED groups are never treated as blocked.
"""

from __future__ import annotations

import json
import os
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

BUILD = "20261015-m3-shared-access-registry-v1"
REG_REL = "_og_commercial_research_pipeline_out/m3_shared_access_registry_v1.json"

_BLOCKED_PG_STATUSES = {
    "INCOMPLETE",
    "QUEUED",
    "REGISTERING",
    "PAID_REQUIRED",
    "OWNER_APPROVAL_REQUIRED",
    "PREPARED",
    "ERROR",
}

_lock = threading.RLock()


def _utc() -> str:
    return now_utc().isoformat()


def registry_path() -> Path:
    return Path(data_path(REG_REL))


def empty_registry() -> dict[str, Any]:
    return {
        "kind": "SharedAccessRegistry",
        "build": BUILD,
        "updated_at": _utc(),
        "by_solicitation": {},  # statewide/open-bids numeric id → access record
        "by_opportunity": {},  # opportunity_id → access record
        "by_buyer": {},  # normalized buyer → {pg_id, count, consistent}
        "by_paid_scope": {},  # paid entitlement key → evidence
        "metrics": {
            "fast_defer_hits": 0,
            "registry_misses": 0,
            "records_learned": 0,
        },
    }


def load_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.exists():
        return empty_registry()
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return empty_registry()
        for k in ("by_solicitation", "by_opportunity", "by_buyer", "by_paid_scope", "metrics"):
            obj.setdefault(k, {} if k != "metrics" else {})
        return obj
    except Exception:
        return empty_registry()


def save_registry(reg: dict[str, Any]) -> Path:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    reg = deepcopy(reg)
    reg["updated_at"] = _utc()
    reg["build"] = BUILD
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _lock:
        tmp.write_text(json.dumps(reg, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
    return path


def solicitation_key_from_url(url: str | None) -> str | None:
    u = str(url or "")
    m = re.search(r"/statewide/(\d{6,})", u, re.I)
    if m:
        return f"statewide:{m.group(1)}"
    m = re.search(r"/open-bids/[^/]+/(\d{6,})", u, re.I)
    if m:
        return f"openbids:{m.group(1)}"
    m = re.search(r"/private/solicitations/(\d{6,})", u, re.I)
    if m:
        return f"private:{m.group(1)}"
    return None


def normalize_buyer(buyer: Any) -> str | None:
    b = re.sub(r"\s+", " ", str(buyer or "").strip().lower())
    b = re.sub(r"[^a-z0-9 &./-]", "", b)
    return b[:120] if len(b) >= 4 else None


def _pg_status(pg_id: str) -> str | None:
    try:
        from bidnet_auth.purchasing_group_tracker import get_group, is_registered

        if is_registered(pg_id):
            return "REGISTERED"
        g = get_group(pg_id) or {}
        return str(g.get("status") or "") or None
    except Exception:
        return None


def learn_access(
    *,
    opportunity_id: str | None,
    url: str | None,
    buyer: Any = None,
    purchasing_group_id: str | None = None,
    paid_entitlement: str | None = None,
    blocker: str | None = None,
) -> None:
    """Persist a verified access observation for future fast decisions."""
    oid = str(opportunity_id or "").strip()
    pg = str(purchasing_group_id or "").strip() or None
    paid = str(paid_entitlement or "").strip() or None
    if paid and paid.startswith("PAID_ENTITLEMENT:"):
        paid = paid.split(":", 1)[1]
    sol = solicitation_key_from_url(url)
    buyer_key = normalize_buyer(buyer)
    if not (oid or sol or pg or paid):
        return
    rec = {
        "purchasing_group_id": pg,
        "paid_entitlement": paid,
        "blocker": (blocker or "")[:160] or None,
        "url": (url or "")[:220] or None,
        "learned_at": _utc(),
    }
    with _lock:
        reg = load_registry()
        if oid:
            reg["by_opportunity"][oid] = {**rec, "opportunity_id": oid}
        if sol:
            reg["by_solicitation"][sol] = {**rec, "solicitation_key": sol}
        if paid:
            reg["by_paid_scope"][paid] = {
                "paid_entitlement": paid,
                "blocker": rec["blocker"],
                "learned_at": _utc(),
                "sample_opportunity_id": oid or None,
            }
        if buyer_key and pg:
            prev = dict((reg.get("by_buyer") or {}).get(buyer_key) or {})
            prev_pg = str(prev.get("purchasing_group_id") or "")
            count = int(prev.get("count") or 0) + 1
            consistent = True if not prev_pg else (prev_pg == pg)
            reg["by_buyer"][buyer_key] = {
                "buyer_key": buyer_key,
                "purchasing_group_id": pg if consistent else None,
                "count": count,
                "consistent": consistent,
                "updated_at": _utc(),
                "conflict_pg": None if consistent else sorted({prev_pg, pg}),
            }
        reg.setdefault("metrics", {})
        reg["metrics"]["records_learned"] = int(reg["metrics"].get("records_learned") or 0) + 1
        # Cap growth
        for key, cap in (
            ("by_opportunity", 20000),
            ("by_solicitation", 20000),
            ("by_buyer", 8000),
            ("by_paid_scope", 2000),
        ):
            blob = reg.get(key) or {}
            if len(blob) > cap:
                # keep newest-ish by dropping arbitrary oldest keys
                for drop in list(blob.keys())[: len(blob) - cap]:
                    blob.pop(drop, None)
                reg[key] = blob
        save_registry(reg)


def hydrate_from_job_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Bulk-learn from existing checkpoints (no network)."""
    learned = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        pg = it.get("purchasing_group_id")
        blocker = str(it.get("terminal_blocker") or it.get("TERMINAL_BLOCKER") or "")
        paid = None
        if blocker.startswith("PAID_ENTITLEMENT:"):
            paid = blocker
        if not pg and not paid:
            continue
        learn_access(
            opportunity_id=it.get("opportunity_id") or it.get("OPPORTUNITY_ID"),
            url=it.get("authoritative_url") or it.get("AUTHORITATIVE_URL"),
            buyer=it.get("buyer") or it.get("BUYER"),
            purchasing_group_id=str(pg) if pg else None,
            paid_entitlement=paid,
            blocker=blocker or None,
        )
        learned += 1
    return {"learned": learned, "registry_path": str(registry_path())}


def resolve_fast_access(
    item: dict[str, Any],
    *,
    l23_rec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fast access decision without browser when scope is known.

    Decisions:
      ACCESS_VERIFIED — registered PG known for this solicitation/buyer
      ACCESS_BLOCKED_KNOWN_PG — defer; group registration already unresolved
      PAID_ENTITLEMENT_CONFIRMED — defer; same paid scope verified
      UNKNOWN — must investigate with authenticated access
    """
    l23_rec = l23_rec or {}
    oid = str(item.get("opportunity_id") or "").strip()
    url = str(item.get("authoritative_url") or l23_rec.get("authoritative_url") or "")
    buyer = item.get("buyer") or l23_rec.get("buyer")
    sol = solicitation_key_from_url(url)
    buyer_key = normalize_buyer(buyer)
    t0_note: list[str] = []

    # Exact URL PG (rare but free)
    try:
        from bidnet_auth.purchasing_group_registration import extract_pg_id_from_url

        pg_url = extract_pg_id_from_url(url)
    except Exception:
        pg_url = None

    reg = load_registry()
    rec = None
    if oid and oid in (reg.get("by_opportunity") or {}):
        rec = reg["by_opportunity"][oid]
        t0_note.append("opportunity_map")
    elif sol and sol in (reg.get("by_solicitation") or {}):
        rec = reg["by_solicitation"][sol]
        t0_note.append("solicitation_map")

    pg = pg_url or (str(rec.get("purchasing_group_id") or "") if rec else "") or None
    paid = (str(rec.get("paid_entitlement") or "") if rec else "") or None

    if not pg and buyer_key:
        b = (reg.get("by_buyer") or {}).get(buyer_key) or {}
        if b.get("consistent") and int(b.get("count") or 0) >= 2 and b.get("purchasing_group_id"):
            pg = str(b["purchasing_group_id"])
            t0_note.append("buyer_consistent_map")

    if paid:
        st = "PAID_ENTITLEMENT_CONFIRMED"
        with _lock:
            r2 = load_registry()
            r2.setdefault("metrics", {})
            r2["metrics"]["fast_defer_hits"] = int(r2["metrics"].get("fast_defer_hits") or 0) + 1
            save_registry(r2)
        return {
            "decision": st,
            "defer": True,
            "terminal_blocker": f"PAID_ENTITLEMENT:{paid}",
            "shared_access_key": f"paid:PAID_ENTITLEMENT:{paid}",
            "purchasing_group_id": pg,
            "stage_reached": "ACCESS_DOCS",
            "funnel_outcome": "EXTERNAL_DEPENDENCY",
            "access_fast_path": True,
            "access_basis": ",".join(t0_note) or "paid_map",
            "fetch_via": "shared_access_registry_fast_path",
        }

    if pg:
        status = _pg_status(pg)
        if status == "REGISTERED":
            return {
                "decision": "ACCESS_VERIFIED",
                "defer": False,
                "purchasing_group_id": pg,
                "access_fast_path": False,
                "access_basis": ",".join(t0_note + ["pg_registered"]),
            }
        if status in _BLOCKED_PG_STATUSES:
            # Ensure group-level queue still knows about this opportunity
            try:
                from bidnet_auth.purchasing_group_queue import enqueue_group_registration
                from bidnet_auth.purchasing_group_tracker import note_blocked_opportunity

                note_blocked_opportunity(pg, oid)
                enqueue_group_registration(pg, opportunity_id=oid, source_url=url)
            except Exception:
                pass
            with _lock:
                r2 = load_registry()
                r2.setdefault("metrics", {})
                r2["metrics"]["fast_defer_hits"] = int(r2["metrics"].get("fast_defer_hits") or 0) + 1
                save_registry(r2)
            return {
                "decision": "ACCESS_BLOCKED_KNOWN_PG",
                "defer": True,
                "terminal_blocker": "AWAITING_PURCHASING_GROUP_REGISTRATION",
                "shared_access_key": f"pg:{pg}",
                "purchasing_group_id": pg,
                "purchasing_group_registration_status": status,
                "stage_reached": "ACCESS_DOCS",
                "funnel_outcome": "EXTERNAL_DEPENDENCY",
                "access_fast_path": True,
                "access_basis": ",".join(t0_note + [f"pg_status:{status}"]),
                "fetch_via": "shared_access_registry_fast_path",
                "package_blocker": "AWAITING_PURCHASING_GROUP_REGISTRATION",
            }

    with _lock:
        r2 = load_registry()
        r2.setdefault("metrics", {})
        r2["metrics"]["registry_misses"] = int(r2["metrics"].get("registry_misses") or 0) + 1
        save_registry(r2)
    return {
        "decision": "UNKNOWN",
        "defer": False,
        "purchasing_group_id": pg,
        "access_fast_path": False,
        "access_basis": "investigate",
    }

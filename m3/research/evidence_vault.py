"""Versioned original-evidence vault and closure-retention planner.

Every successfully received source response (listing page, solicitation page, provider payload) is stored
once, in its original form, before parsing:

* ``blobs/<sha[:2]>/<sha>.gz`` — gzip of the exact bytes received, content-addressed (written atomically).
* ``records/<source>/<key[:2]>/<key>.json`` — per (source, record) manifest listing every version per kind
  with url, status, retrieval time, content type, sha256 and caller metadata (e.g. extracted deadline).

A changed body is a new version; earlier versions are never overwritten (amendments keep history).
Re-storing identical bytes only records the observation, so reprocessing never needs the source again.

Closure retention: :func:`retention_plan` previews which bodies may be dropped ~72 h after *verified*
closure and why everything else is kept; :func:`apply_retention` deletes nothing unless explicitly enabled.
The vault lives under ``M3_DATA_ROOT`` (outside the Git repository).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings

RETENTION_HOURS_DEFAULT = 72
# Lifecycle states that are verified closures (deadline passed per latest evidence, cancelled, awarded)
VERIFIED_CLOSED = {"EXPIRED", "CANCELLED", "AWARDED"}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def vault_root(root: Path | None = None) -> Path:
    p = root or get_settings().evidence_vault_root()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _record_key(source: str, record_id: str) -> str:
    return hashlib.sha256(f"{source}|{record_id}".encode("utf-8")).hexdigest()


def _manifest_path(root: Path, source: str, record_id: str) -> Path:
    k = _record_key(source, record_id)
    safe = re.sub(r"[^a-z0-9_.-]+", "_", source.lower())[:60]
    return root / "records" / safe / k[:2] / f"{k}.json"


def _blob_path(root: Path, sha: str) -> Path:
    return root / "blobs" / sha[:2] / f"{sha}.gz"


def load_manifest(source: str, record_id: str, *, root: Path | None = None) -> dict[str, Any] | None:
    p = _manifest_path(vault_root(root), source, record_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def put(source: str, record_id: str, kind: str, body: bytes | str, *, url: str | None = None, status: int | None = None,
        content_type: str | None = None, retrieved_at: str | None = None, meta: dict[str, Any] | None = None,
        root: Path | None = None) -> dict[str, Any]:
    """Store one received response. Returns ``{"sha256", "version", "changed", "first_for_record"}``."""
    root = vault_root(root)
    data = body.encode("utf-8") if isinstance(body, str) else body
    sha = hashlib.sha256(data).hexdigest()
    bp = _blob_path(root, sha)
    if not bp.exists():
        _atomic_write(bp, gzip.compress(data, mtime=0))
    mp = _manifest_path(root, source, record_id)
    first = not mp.exists()
    man = json.loads(mp.read_text(encoding="utf-8")) if not first else {
        "source": source, "record_id": record_id, "created_at": _utc(), "kinds": {}}
    versions = man["kinds"].setdefault(kind, [])
    now = retrieved_at or _utc()
    if versions and versions[-1]["sha256"] == sha:
        versions[-1].setdefault("observed_at", []).append(now)
        versions[-1]["observed_at"] = versions[-1]["observed_at"][-20:]
        changed = False
    else:
        versions.append({"version": len(versions) + 1, "sha256": sha, "bytes": len(data), "url": url, "status": status,
                         "content_type": content_type, "retrieved_at": now, "meta": meta or {}, "body_retained": True})
        changed = len(versions) > 1
    man["updated_at"] = _utc()
    _atomic_write(mp, json.dumps(man, indent=1, default=str).encode("utf-8"))
    return {"sha256": sha, "version": versions[-1]["version"], "changed": changed, "first_for_record": first}


def get_body(sha: str, *, root: Path | None = None) -> bytes | None:
    bp = _blob_path(vault_root(root), sha)
    return gzip.decompress(bp.read_bytes()) if bp.exists() else None


def latest(source: str, record_id: str, kind: str, *, root: Path | None = None) -> dict[str, Any] | None:
    """Latest version entry plus its body (``None`` body if retention removed it)."""
    man = load_manifest(source, record_id, root=root)
    versions = (man or {}).get("kinds", {}).get(kind) or []
    if not versions:
        return None
    v = dict(versions[-1])
    v["body"] = get_body(v["sha256"], root=root) if v.get("body_retained", True) else None
    return v


def needs_retrieval(source: str, record_id: str, kind: str, *, listing_fingerprint: str | None = None,
                    max_age_hours: float | None = None, now: datetime | None = None, root: Path | None = None) -> dict[str, Any]:
    """Cache-first decision: is another request justified for this record?

    Unchanged listing fingerprint (or a body younger than ``max_age_hours``) means reuse the retained body.
    """
    v = latest(source, record_id, kind, root=root)
    if not v or v.get("body") is None:
        return {"retrieve": True, "reason": "no_retained_body"}
    if listing_fingerprint and (v.get("meta") or {}).get("listing_fingerprint") == listing_fingerprint:
        return {"retrieve": False, "reason": "listing_unchanged_reuse_retained", "sha256": v["sha256"]}
    if max_age_hours is not None:
        got = datetime.fromisoformat(str(v["retrieved_at"]).replace("Z", "+00:00"))
        if ((now or datetime.now(timezone.utc)) - got).total_seconds() < max_age_hours * 3600:
            return {"retrieve": False, "reason": "retained_body_fresh", "sha256": v["sha256"]}
    if listing_fingerprint:
        return {"retrieve": True, "reason": "listing_changed"}
    return {"retrieve": True, "reason": "retained_body_stale"}


def amendment_history(source: str, record_id: str, kind: str, *, root: Path | None = None) -> list[dict[str, Any]]:
    """Version-to-version changes in caller-recorded fields (deadline, line items...)."""
    man = load_manifest(source, record_id, root=root)
    versions = (man or {}).get("kinds", {}).get(kind) or []
    out = []
    for a, b in zip(versions, versions[1:]):
        ma, mb = a.get("meta") or {}, b.get("meta") or {}
        diff = {k: {"from": ma.get(k), "to": mb.get(k)} for k in sorted(set(ma) | set(mb))
                if k != "listing_fingerprint" and ma.get(k) != mb.get(k)}
        out.append({"from_version": a["version"], "to_version": b["version"], "retrieved_at": b["retrieved_at"], "changes": diff})
    return out


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def retention_plan(rows: list[dict[str, Any]], *, now: datetime, protected_ids: set[str] | None = None,
                   retention_hours: float = RETENTION_HOURS_DEFAULT, root: Path | None = None) -> dict[str, Any]:
    """Dry-run preview of closure cleanup. ``rows`` are inventory rows (``source``, ``source_record_id``,
    ``lifecycle_status``, ``deadline_at``, ``owner_queue``). Nothing is deleted here.

    A body is eligible only when the record is a verified closure, the latest retained evidence does not
    show a later (extended) deadline, ``retention_hours`` have passed since closure, the record is not
    protected (bid, quote, award, review, dispute) and it is not in an active owner research queue.
    """
    root = vault_root(root)
    protected = protected_ids or set()
    eligible, kept = [], {}

    def keep(reason: str) -> None:
        kept[reason] = kept.get(reason, 0) + 1

    for r in rows:
        sid = str(r.get("source_record_id") or "")
        man_p = _manifest_path(root, str(r.get("source")), sid)
        if not man_p.exists():
            keep("no_vault_evidence")
            continue
        if sid in protected or str(r.get("staged_id")) in protected:
            keep("protected_business_record")
            continue
        if r.get("owner_queue") in {"actionable", "supplier_research_ready", "financing_verification", "easy_registration"}:
            keep("active_owner_queue")
            continue
        if r.get("lifecycle_status") not in VERIFIED_CLOSED:
            keep("not_verified_closed")
            continue
        man = json.loads(man_p.read_text(encoding="utf-8"))
        deadlines = [_dt((v.get("meta") or {}).get("deadline_at")) for vs in man["kinds"].values() for v in vs]
        latest_deadline = max([d for d in deadlines if d] + ([d] if (d := _dt(r.get("deadline_at"))) else []), default=None)
        if latest_deadline is None and r.get("lifecycle_status") == "EXPIRED":
            keep("closure_time_unknown")
            continue
        if latest_deadline and latest_deadline > now:
            keep("extended_deadline_in_evidence")
            continue
        closed_at = latest_deadline or _dt(r.get("last_seen_at")) or now
        if now - closed_at < timedelta(hours=retention_hours):
            keep("within_retention_window")
            continue
        bodies = [v["sha256"] for vs in man["kinds"].values() for v in vs if v.get("body_retained", True)]
        if not bodies:
            keep("already_cleaned")
            continue
        eligible.append({"source": r.get("source"), "source_record_id": sid, "closed_at": closed_at.isoformat(),
                         "lifecycle_status": r.get("lifecycle_status"), "bodies": bodies})
    return {"dry_run": True, "generated_at": now.isoformat(), "retention_hours": retention_hours,
            "eligible_records": len(eligible), "eligible_bodies": sum(len(e["bodies"]) for e in eligible),
            "kept_by_reason": kept, "eligible": eligible}


def apply_retention(plan: dict[str, Any], *, enabled: bool = False, root: Path | None = None) -> dict[str, Any]:
    """Drop eligible bodies; keep each manifest (ids, versions, hashes, closure time) for dedup and accounting.

    Refuses unless ``enabled`` is explicitly passed (owner approval). A blob still referenced by any
    retained version of another record is never deleted.
    """
    if not enabled:
        return {"applied": False, "reason": "retention_cleanup_not_enabled", "would_remove_bodies": plan.get("eligible_bodies", 0)}
    root = vault_root(root)
    drop = {(e["source"], e["source_record_id"]) for e in plan.get("eligible", [])}
    referenced: set[str] = set()
    for mp in (root / "records").rglob("*.json"):
        man = json.loads(mp.read_text(encoding="utf-8"))
        if (man["source"], man["record_id"]) in drop:
            continue
        referenced.update(v["sha256"] for vs in man["kinds"].values() for v in vs if v.get("body_retained", True))
    removed = 0
    for source, rid in drop:
        mp = _manifest_path(root, str(source), rid)
        man = json.loads(mp.read_text(encoding="utf-8"))
        for vs in man["kinds"].values():
            for v in vs:
                if v.get("body_retained", True):
                    v["body_retained"] = False
                    v["body_removed_at"] = _utc()
                    if v["sha256"] not in referenced and _blob_path(root, v["sha256"]).exists():
                        _blob_path(root, v["sha256"]).unlink()
                        removed += 1
        man["retention"] = {"bodies_removed_at": _utc(), "reason": "verified_closed_past_retention"}
        _atomic_write(mp, json.dumps(man, indent=1, default=str).encode("utf-8"))
    return {"applied": True, "records": len(drop), "blobs_removed": removed}

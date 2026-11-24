"""Permanent procurement record registry (Build 002L).

Preserves every discovered solicitation identity and change history.
Never deletes underlying records — archive/soft-delete only.
No metered NextBid calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings

_LOCK = threading.RLock()
REGISTRY_VERSION = 1


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def registry_root() -> Path:
    p = get_settings().data_root() / "procurement_registry"
    p.mkdir(parents=True, exist_ok=True)
    return p


def records_path() -> Path:
    return registry_root() / "records.jsonl"


def events_path() -> Path:
    return registry_root() / "events.jsonl"


def source_links_path() -> Path:
    return registry_root() / "source_links.jsonl"


def stable_record_id(*, provider: str, provider_id: str, solicitation_number: str | None = None) -> str:
    """Stable unique identity for a discovery record (never recycled)."""
    base = f"{provider.strip().lower()}|{str(provider_id).strip()}"
    if solicitation_number:
        base += f"|{str(solicitation_number).strip().upper()}"
    return "pr_" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def canonical_key(*, solicitation_number: str | None, official_url: str | None, title: str | None) -> str:
    sol = (solicitation_number or "").strip().upper()
    if sol:
        return f"sol:{sol}"
    url = (official_url or "").strip().lower().split("?")[0].rstrip("/")
    if url:
        return "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    t = (title or "").strip().lower()
    return "title:" + hashlib.sha256(t.encode("utf-8")).hexdigest()[:20]


def content_fingerprint(payload: dict[str, Any]) -> str:
    parts = [
        str(payload.get("title") or ""),
        str(payload.get("solicitation_number") or ""),
        str(payload.get("deadline_at") or ""),
        str(payload.get("official_url") or ""),
        str(payload.get("quantity") or ""),
        str(payload.get("quantity_semantic") or ""),
        str(payload.get("procurement_stage") or ""),
        str(payload.get("package_status") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _rewrite_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Atomic-ish rewrite resilient to Windows/OneDrive locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, default=str) + "\n" for row in rows)
    last_err: Exception | None = None
    for attempt in range(12):
        tmp = path.with_name(f"{path.stem}.{os.getpid()}.{attempt}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            try:
                os.replace(str(tmp), str(path))
                return
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.25 + attempt * 0.05)
                continue
        except PermissionError as exc:
            last_err = exc
            time.sleep(0.25 + attempt * 0.05)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
    # Last resort: append-only sidecar then copy content via direct open
    try:
        with path.open("w", encoding="utf-8") as f:
            f.write(payload)
        return
    except PermissionError as exc:
        last_err = exc
    if last_err:
        raise last_err


def load_records() -> dict[str, dict[str, Any]]:
    path = records_path()
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = row.get("record_id")
            if rid:
                out[str(rid)] = row
    return out


def append_event(
    *,
    record_id: str,
    event_type: str,
    detail: dict[str, Any] | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    ev = {
        "event_id": hashlib.sha256(f"{record_id}|{event_type}|{_utc()}|{json.dumps(detail or {}, default=str)}".encode()).hexdigest()[:20],
        "record_id": record_id,
        "event_type": event_type,
        "actor": actor,
        "at": _utc(),
        "detail": detail or {},
    }
    with _LOCK:
        _append_jsonl(events_path(), ev)
    return ev


def upsert_source_link(
    *,
    record_id: str,
    source: str,
    source_record_id: str,
    official_url: str | None = None,
    meta: dict[str, Any] | None = None,
    defer_flush: bool = False,
    _cache: dict[str, dict] | None = None,
) -> dict[str, Any]:
    link = {
        "link_id": hashlib.sha256(f"{record_id}|{source}|{source_record_id}".encode()).hexdigest()[:20],
        "record_id": record_id,
        "source": source,
        "source_record_id": source_record_id,
        "official_url": official_url,
        "first_seen_at": _utc(),
        "last_seen_at": _utc(),
        "meta": meta or {},
    }
    with _LOCK:
        path = source_links_path()
        if _cache is not None:
            existing = _cache
        else:
            existing = {}
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    lid = row.get("link_id")
                    if lid:
                        existing[str(lid)] = row
        prev = existing.get(link["link_id"])
        if prev:
            link["first_seen_at"] = prev.get("first_seen_at") or link["first_seen_at"]
            link["meta"] = {**(prev.get("meta") or {}), **(meta or {})}
        existing[link["link_id"]] = link
        if not defer_flush:
            _rewrite_jsonl(path, list(existing.values()))
    return link


def flush_source_links(cache: dict[str, dict]) -> None:
    with _LOCK:
        _rewrite_jsonl(source_links_path(), list(cache.values()))


def upsert_record(payload: dict[str, Any], *, actor: str = "build_002l") -> dict[str, Any]:
    """Create or update a permanent record. Never destroys prior field history."""
    provider = str(payload.get("provider") or payload.get("nextbid_source") or payload.get("source") or "unknown")
    provider_id = str(payload.get("provider_id") or payload.get("nb_id") or payload.get("external_key") or "")
    if not provider_id:
        provider_id = content_fingerprint(payload)
    solicitation_number = payload.get("solicitation_number")
    record_id = payload.get("record_id") or stable_record_id(
        provider=provider, provider_id=provider_id, solicitation_number=None
    )
    fp = content_fingerprint(payload)
    now = _utc()

    with _LOCK:
        records = load_records()
        prev = records.get(record_id)
        if not prev:
            row = {
                "record_id": record_id,
                "registry_version": REGISTRY_VERSION,
                "canonical_key": canonical_key(
                    solicitation_number=solicitation_number,
                    official_url=payload.get("official_url"),
                    title=payload.get("title"),
                ),
                "provider": provider,
                "provider_id": provider_id,
                "nb_id": payload.get("nb_id"),
                "solicitation_number": solicitation_number,
                "title": payload.get("title"),
                "agency": payload.get("agency") or payload.get("buyer_name"),
                "official_url": payload.get("official_url") or payload.get("source_url_original"),
                "deadline_at": payload.get("deadline_at"),
                "quantity": payload.get("quantity"),
                "quantity_unit": payload.get("quantity_unit"),
                "quantity_semantic": payload.get("quantity_semantic"),
                "quantity_evidence": payload.get("quantity_evidence"),
                "requirement_class": payload.get("requirement_class"),
                "procurement_stage": payload.get("procurement_stage") or payload.get("stage"),
                "package_status": payload.get("package_status") or "UNKNOWN",
                "lifecycle_status": payload.get("lifecycle_status") or "active_or_unknown",
                "active_queue": True,
                "archived": False,
                "protected": bool(payload.get("protected")),
                "first_discovered_at": payload.get("first_discovered_at") or now,
                "last_verified_at": now,
                "content_hash": fp,
                "document_manifest": payload.get("document_manifest") or [],
                "classification_history": [
                    {
                        "at": now,
                        "stage": payload.get("procurement_stage") or payload.get("stage"),
                        "requirement_class": payload.get("requirement_class"),
                        "actor": actor,
                    }
                ],
                "source_provenance": [
                    {
                        "provider": provider,
                        "provider_id": provider_id,
                        "at": now,
                    }
                ],
                "raw_snapshot": {
                    k: payload.get(k)
                    for k in (
                        "nb_id",
                        "title",
                        "solicitation_number",
                        "official_url",
                        "deadline_at",
                        "quantity",
                        "quantity_semantic",
                        "quantity_evidence",
                        "requirement_class",
                        "procurement_stage",
                        "document_text_len",
                        "verified_individual_solicitation",
                        "record_type",
                    )
                    if payload.get(k) is not None
                },
                "build_origins": list(payload.get("build_origins") or [actor]),
                "exclusion_reason": payload.get("exclusion_reason"),
                "updated_at": now,
            }
            records[record_id] = row
            # Caller may batch-flush; still write record immediately for durability
            _rewrite_jsonl(records_path(), list(records.values()))
            append_event(record_id=record_id, event_type="created", detail={"content_hash": fp}, actor=actor)
            if not payload.get("_skip_source_link"):
                upsert_source_link(
                    record_id=record_id,
                    source=provider,
                    source_record_id=provider_id,
                    official_url=row.get("official_url"),
                    meta={"first_build": actor},
                    defer_flush=bool(payload.get("_defer_source_link")),
                    _cache=payload.get("_source_link_cache"),
                )
            return row

        # Update — preserve history, never erase prior classification evidence
        changed: list[str] = []
        for field in (
            "title",
            "solicitation_number",
            "agency",
            "official_url",
            "deadline_at",
            "quantity",
            "quantity_unit",
            "quantity_semantic",
            "requirement_class",
            "procurement_stage",
            "package_status",
            "lifecycle_status",
            "exclusion_reason",
        ):
            new_v = payload.get(field) if field != "procurement_stage" else (
                payload.get("procurement_stage") or payload.get("stage")
            )
            if field == "agency" and new_v is None:
                new_v = payload.get("buyer_name")
            if new_v is not None and new_v != prev.get(field):
                changed.append(field)
                prev[field] = new_v
        if payload.get("quantity_evidence") and payload.get("quantity_evidence") != prev.get("quantity_evidence"):
            prev["quantity_evidence"] = payload.get("quantity_evidence")
            changed.append("quantity_evidence")
        if fp != prev.get("content_hash"):
            prev["content_hash"] = fp
            changed.append("content_hash")
        stage = payload.get("procurement_stage") or payload.get("stage")
        if stage and (
            not prev.get("classification_history")
            or (prev["classification_history"][-1] or {}).get("stage") != stage
        ):
            hist = list(prev.get("classification_history") or [])
            hist.append(
                {
                    "at": now,
                    "stage": stage,
                    "requirement_class": payload.get("requirement_class") or prev.get("requirement_class"),
                    "actor": actor,
                }
            )
            prev["classification_history"] = hist[-50:]
            changed.append("classification_history")
        origins = list(prev.get("build_origins") or [])
        if actor not in origins:
            origins.append(actor)
            prev["build_origins"] = origins
        prev["last_verified_at"] = now
        prev["updated_at"] = now
        # Merge raw snapshot without dropping prior keys
        raw = dict(prev.get("raw_snapshot") or {})
        for k, v in (payload.items() if False else []):
            pass
        for k in (
            "nb_id",
            "title",
            "solicitation_number",
            "official_url",
            "deadline_at",
            "quantity",
            "quantity_semantic",
            "quantity_evidence",
            "requirement_class",
            "procurement_stage",
            "document_text_len",
            "verified_individual_solicitation",
            "record_type",
        ):
            if payload.get(k) is not None:
                raw[k] = payload.get(k)
        prev["raw_snapshot"] = raw
        records[record_id] = prev
        _rewrite_jsonl(records_path(), list(records.values()))
        if changed:
            append_event(
                record_id=record_id,
                event_type="updated",
                detail={"changed_fields": changed, "content_hash": fp},
                actor=actor,
            )
        if not payload.get("_skip_source_link"):
            upsert_source_link(
                record_id=record_id,
                source=provider,
                source_record_id=provider_id,
                official_url=prev.get("official_url"),
                defer_flush=bool(payload.get("_defer_source_link")),
                _cache=payload.get("_source_link_cache"),
            )
        return prev


def archive_record(record_id: str, *, reason: str, actor: str = "system") -> dict[str, Any] | None:
    """Soft-archive: leave active queue, never erase."""
    with _LOCK:
        records = load_records()
        row = records.get(record_id)
        if not row:
            return None
        if row.get("protected"):
            append_event(
                record_id=record_id,
                event_type="archive_blocked_protected",
                detail={"reason": reason},
                actor=actor,
            )
            return row
        row["archived"] = True
        row["active_queue"] = False
        row["lifecycle_status"] = row.get("lifecycle_status") or "archived"
        row["exclusion_reason"] = reason
        row["updated_at"] = _utc()
        records[record_id] = row
        _rewrite_jsonl(records_path(), list(records.values()))
        append_event(record_id=record_id, event_type="archived", detail={"reason": reason}, actor=actor)
        return row


def find_duplicates(payload: dict[str, Any], *, records: dict[str, dict] | None = None) -> list[str]:
    """Return existing record_ids that match provider id, solicitation, or content hash."""
    records = records or load_records()
    hits: list[str] = []
    nb = str(payload.get("nb_id") or "")
    sol = str(payload.get("solicitation_number") or "").strip().upper()
    fp = content_fingerprint(payload)
    ck = canonical_key(
        solicitation_number=payload.get("solicitation_number"),
        official_url=payload.get("official_url"),
        title=payload.get("title"),
    )
    for rid, row in records.items():
        if nb and row.get("nb_id") == nb:
            hits.append(rid)
            continue
        if sol and str(row.get("solicitation_number") or "").strip().upper() == sol:
            hits.append(rid)
            continue
        if row.get("canonical_key") == ck or row.get("content_hash") == fp:
            hits.append(rid)
    return sorted(set(hits))


def ingest_population(items: list[dict[str, Any]], *, actor: str = "build_002l") -> dict[str, Any]:
    """Bulk upsert without destroying prior rows. Dedupes by provider id.

    Batches record + source-link flushes to avoid OneDrive lock storms.
    """
    created = 0
    updated = 0
    duplicate_links = 0
    ids: list[str] = []
    existing = load_records()
    link_cache: dict[str, dict] = {}
    # Seed link cache once
    sp = source_links_path()
    if sp.exists():
        for line in sp.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            lid = row.get("link_id")
            if lid:
                link_cache[str(lid)] = row

    pending_rows: dict[str, dict] = dict(existing)
    for idx, it in enumerate(items):
        dups = find_duplicates(it, records=pending_rows)
        payload = {
            **it,
            "provider": it.get("nextbid_source") or it.get("source") or "nextbid",
            "_defer_source_link": True,
            "_source_link_cache": link_cache,
            "_skip_record_flush": True,
        }
        row = _upsert_record_in_memory(pending_rows, payload, actor=actor)
        # Source link (deferred flush)
        upsert_source_link(
            record_id=row["record_id"],
            source=str(payload.get("provider") or "nextbid"),
            source_record_id=str(row.get("provider_id") or row.get("nb_id") or ""),
            official_url=row.get("official_url"),
            meta={"first_build": actor} if row["record_id"] not in existing else {},
            defer_flush=True,
            _cache=link_cache,
        )
        ids.append(row["record_id"])
        if row["record_id"] not in existing:
            created += 1
        else:
            updated += 1
        if len(dups) > 1 or (dups and dups[0] != row["record_id"]):
            duplicate_links += 1
        pending_rows[row["record_id"]] = row
        # Periodic flush every 100 rows for crash safety
        if (idx + 1) % 100 == 0:
            _rewrite_jsonl(records_path(), list(pending_rows.values()))
            flush_source_links(link_cache)

    _rewrite_jsonl(records_path(), list(pending_rows.values()))
    flush_source_links(link_cache)
    append_event(
        record_id="registry",
        event_type="bulk_ingest_complete",
        detail={"ingested": len(items), "created": created, "updated": updated, "actor": actor},
        actor=actor,
    )
    return {
        "total_records": len(pending_rows),
        "ingested": len(items),
        "created_or_touched": created + updated,
        "created": created,
        "updated": updated,
        "duplicate_links_detected": duplicate_links,
        "record_ids_sample": ids[:20],
        "metered_calls": 0,
    }


def _upsert_record_in_memory(
    records: dict[str, dict[str, Any]],
    payload: dict[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    """In-memory upsert used by bulk ingest; caller flushes JSONL."""
    provider = str(payload.get("provider") or payload.get("nextbid_source") or payload.get("source") or "unknown")
    provider_id = str(payload.get("provider_id") or payload.get("nb_id") or payload.get("external_key") or "")
    if not provider_id:
        provider_id = content_fingerprint(payload)
    solicitation_number = payload.get("solicitation_number")
    record_id = payload.get("record_id") or stable_record_id(
        provider=provider, provider_id=provider_id, solicitation_number=None
    )
    fp = content_fingerprint(payload)
    now = _utc()
    prev = records.get(record_id)
    if not prev:
        row = {
            "record_id": record_id,
            "registry_version": REGISTRY_VERSION,
            "canonical_key": canonical_key(
                solicitation_number=solicitation_number,
                official_url=payload.get("official_url"),
                title=payload.get("title"),
            ),
            "provider": provider,
            "provider_id": provider_id,
            "nb_id": payload.get("nb_id"),
            "solicitation_number": solicitation_number,
            "title": payload.get("title"),
            "agency": payload.get("agency") or payload.get("buyer_name"),
            "official_url": payload.get("official_url") or payload.get("source_url_original"),
            "deadline_at": payload.get("deadline_at"),
            "quantity": payload.get("quantity"),
            "quantity_unit": payload.get("quantity_unit"),
            "quantity_semantic": payload.get("quantity_semantic"),
            "quantity_evidence": payload.get("quantity_evidence"),
            "requirement_class": payload.get("requirement_class"),
            "procurement_stage": payload.get("procurement_stage") or payload.get("stage"),
            "package_status": payload.get("package_status") or "UNKNOWN",
            "lifecycle_status": payload.get("lifecycle_status") or "active_or_unknown",
            "active_queue": True,
            "archived": False,
            "protected": bool(payload.get("protected")),
            "first_discovered_at": payload.get("first_discovered_at") or now,
            "last_verified_at": now,
            "content_hash": fp,
            "document_manifest": payload.get("document_manifest") or [],
            "classification_history": [
                {
                    "at": now,
                    "stage": payload.get("procurement_stage") or payload.get("stage"),
                    "requirement_class": payload.get("requirement_class"),
                    "actor": actor,
                }
            ],
            "source_provenance": [{"provider": provider, "provider_id": provider_id, "at": now}],
            "raw_snapshot": {
                k: payload.get(k)
                for k in (
                    "nb_id",
                    "title",
                    "solicitation_number",
                    "official_url",
                    "deadline_at",
                    "quantity",
                    "quantity_semantic",
                    "quantity_evidence",
                    "requirement_class",
                    "procurement_stage",
                    "document_text_len",
                    "verified_individual_solicitation",
                    "record_type",
                )
                if payload.get(k) is not None
            },
            "build_origins": list(payload.get("build_origins") or [actor]),
            "exclusion_reason": payload.get("exclusion_reason"),
            "updated_at": now,
        }
        records[record_id] = row
        return row

    stage = payload.get("procurement_stage") or payload.get("stage")
    for field in (
        "title",
        "solicitation_number",
        "official_url",
        "deadline_at",
        "quantity",
        "quantity_unit",
        "quantity_semantic",
        "requirement_class",
        "package_status",
        "lifecycle_status",
        "exclusion_reason",
    ):
        new_v = payload.get(field)
        if new_v is not None:
            prev[field] = new_v
    if payload.get("agency") or payload.get("buyer_name"):
        prev["agency"] = payload.get("agency") or payload.get("buyer_name")
    if stage:
        prev["procurement_stage"] = stage
        hist = list(prev.get("classification_history") or [])
        if not hist or (hist[-1] or {}).get("stage") != stage:
            hist.append(
                {
                    "at": now,
                    "stage": stage,
                    "requirement_class": payload.get("requirement_class") or prev.get("requirement_class"),
                    "actor": actor,
                }
            )
            prev["classification_history"] = hist[-50:]
    if payload.get("quantity_evidence"):
        prev["quantity_evidence"] = payload.get("quantity_evidence")
    prev["content_hash"] = fp
    prev["last_verified_at"] = now
    prev["updated_at"] = now
    origins = list(prev.get("build_origins") or [])
    if actor not in origins:
        origins.append(actor)
        prev["build_origins"] = origins
    raw = dict(prev.get("raw_snapshot") or {})
    for k in (
        "nb_id",
        "title",
        "solicitation_number",
        "official_url",
        "deadline_at",
        "quantity",
        "quantity_semantic",
        "quantity_evidence",
        "requirement_class",
        "procurement_stage",
        "document_text_len",
        "verified_individual_solicitation",
        "record_type",
    ):
        if payload.get(k) is not None:
            raw[k] = payload.get(k)
    prev["raw_snapshot"] = raw
    records[record_id] = prev
    return prev

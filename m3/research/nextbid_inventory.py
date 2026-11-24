"""Persistent NextBid opportunity inventory for incremental acquisition (Build 002J).

No metered API calls. Tracks first/last seen, hashes, acquisition status.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def inventory_path() -> Path:
    p = get_settings().data_root() / "nextbid_inventory" / "opportunities.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_STATUS_FIELDS = (
    "lifecycle_status",
    "solicitation_status",
    "deadline_at",
    "response_deadline",
    "posted_at",
    "posted_date",
    "notice_type",
    "last_updated",
    "procurement_kind",
    "status_evidence",
    "provider_open_filter",
    "reconciliation_category",
)


def content_hash(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("nb_id") or ""),
        str(item.get("title") or ""),
        str(item.get("solicitation_number") or ""),
        str(item.get("deadline_at") or item.get("response_deadline") or ""),
        str(item.get("official_url") or item.get("source_url") or ""),
        str(item.get("document_count_hint") or item.get("document_count") or ""),
        str(item.get("last_updated") or ""),
        str(item.get("lifecycle_status") or ""),
        str(item.get("solicitation_status") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _pick_status_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Extract status/deadline fields that must survive inventory upsert."""
    out: dict[str, Any] = {}
    # Top-level preferred; fall back to evidence / NextBid aliases
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    life = (
        item.get("lifecycle_status")
        or evidence.get("lifecycle_status")
        or item.get("status")
    )
    if life not in (None, ""):
        out["lifecycle_status"] = str(life).strip().lower()
    sol = item.get("solicitation_status")
    if sol not in (None, ""):
        out["solicitation_status"] = str(sol).strip().lower()
    deadline = item.get("deadline_at") or item.get("response_deadline")
    if deadline not in (None, ""):
        out["deadline_at"] = deadline
        out["response_deadline"] = item.get("response_deadline") or deadline
    posted = item.get("posted_at") or item.get("posted_date")
    if posted not in (None, ""):
        out["posted_at"] = posted
        out["posted_date"] = item.get("posted_date") or posted
    for key in (
        "notice_type",
        "last_updated",
        "procurement_kind",
        "status_evidence",
        "provider_open_filter",
        "reconciliation_category",
    ):
        if item.get(key) not in (None, "", [], {}):
            out[key] = item[key]
    return out


def load_inventory() -> dict[str, dict[str, Any]]:
    path = inventory_path()
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
            nb = row.get("nb_id")
            if nb:
                out[str(nb)] = row
    return out


def upsert_inventory(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge listing/enriched rows into inventory. No API calls."""
    existing = load_inventory()
    now = _utc()
    new = 0
    updated = 0
    unchanged = 0
    for it in items:
        nb = str(it.get("nb_id") or "")
        if not nb:
            continue
        ch = content_hash(it)
        prev = existing.get(nb)
        status_fields = _pick_status_fields(it)
        if not prev:
            row = {
                "nb_id": nb,
                "solicitation_number": it.get("solicitation_number"),
                "title": it.get("title"),
                "source": it.get("nextbid_source") or it.get("source"),
                "official_url": it.get("official_url")
                or it.get("source_url_original")
                or it.get("source_url"),
                "first_seen_at": now,
                "last_seen_at": now,
                "last_updated_at": it.get("last_updated") or now,
                "content_hash": ch,
                "document_hash": it.get("document_hash"),
                "acquisition_status": it.get("acquisition_status") or "listed",
                "processing_status": it.get("procurement_stage")
                or it.get("processing_status")
                or "discovered",
                "amendment_history": it.get("amendments") or [],
            }
            row.update(status_fields)
            existing[nb] = row
            new += 1
        else:
            prev["last_seen_at"] = now
            # Always backfill missing status fields; overwrite when incoming has values
            for k, v in status_fields.items():
                if v in (None, "", [], {}):
                    continue
                if prev.get(k) in (None, "", [], {}, "unknown") or k in {
                    "lifecycle_status",
                    "solicitation_status",
                    "deadline_at",
                    "reconciliation_category",
                    "status_evidence",
                    "provider_open_filter",
                }:
                    prev[k] = v
            if it.get("solicitation_number") and not prev.get("solicitation_number"):
                prev["solicitation_number"] = it.get("solicitation_number")
            if it.get("source") or it.get("nextbid_source"):
                prev["source"] = it.get("nextbid_source") or it.get("source") or prev.get("source")
            if prev.get("content_hash") != ch:
                prev["content_hash"] = ch
                prev["last_updated_at"] = now
                prev["title"] = it.get("title") or prev.get("title")
                prev["official_url"] = (
                    it.get("official_url")
                    or it.get("source_url_original")
                    or it.get("source_url")
                    or prev.get("official_url")
                )
                prev["acquisition_status"] = "changed"
                updated += 1
            else:
                unchanged += 1
            if it.get("procurement_stage"):
                prev["processing_status"] = it.get("procurement_stage")
            existing[nb] = prev

    path = inventory_path()
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in existing.values():
            f.write(json.dumps(row, default=str) + "\n")
    tmp.replace(path)
    return {
        "total": len(existing),
        "new": new,
        "updated": updated,
        "unchanged": unchanged,
        "path": str(path),
    }


def needs_enrichment(nb_id: str, *, inventory: dict[str, dict] | None = None) -> bool:
    inv = inventory if inventory is not None else load_inventory()
    row = inv.get(nb_id)
    if not row:
        return True
    if row.get("acquisition_status") in {"changed", "listed"}:
        return True
    if row.get("processing_status") in {"discovered", "VERIFIED_PRODUCT_OPPORTUNITY"}:
        # May need docs — but incremental mode must not auto-spend
        return False
    return False

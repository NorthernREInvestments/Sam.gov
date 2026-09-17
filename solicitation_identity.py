"""Solicitation identity, fingerprints, and cross-source dedupe."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from application_clock import now_utc
from national_discovery_constants import CHG_NEW, CHG_UNCHANGED, CHG_UNKNOWN


def _utc() -> str:
    return now_utc().isoformat()


def normalize_title(title: str | None) -> str:
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    t = re.sub(r"[^\w\s\-]", "", t)
    return t[:200]


def normalize_solicitation_number(num: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (num or "").upper())


def fingerprint_record(record: dict[str, Any]) -> str:
    """Content fingerprint for change detection — compact, not a full dump."""
    parts = [
        str(record.get("solicitation_number") or ""),
        normalize_title(record.get("title")),
        str(record.get("deadline") or record.get("response_deadline") or ""),
        str(record.get("status") or ""),
        str(record.get("amendment_id") or record.get("version") or ""),
        str(record.get("quantity_hash") or ""),
        str(record.get("package_hash") or ""),
        str(record.get("detail_url") or record.get("canonical_url") or ""),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def identity_key(record: dict[str, Any]) -> str:
    """Stable identity preferring solicitation number + buyer; else source+record id."""
    sn = normalize_solicitation_number(record.get("solicitation_number") or record.get("external_id"))
    buyer = re.sub(r"\s+", "", (record.get("agency") or record.get("buyer") or "").lower())[:40]
    if sn and len(sn) >= 6:
        return f"sol:{sn}:{buyer}" if buyer else f"sol:{sn}"
    src = record.get("source_id") or "unknown"
    rid = record.get("source_record_id") or record.get("external_id") or fingerprint_record(record)
    return f"src:{src}:{rid}"


def build_solicitation_identity(record: dict[str, Any]) -> dict[str, Any]:
    now = _utc()
    fp = fingerprint_record(record)
    return {
        "kind": "SolicitationIdentity",
        "identity_key": identity_key(record),
        "source_id": record.get("source_id"),
        "source_record_id": record.get("source_record_id") or record.get("external_id"),
        "solicitation_number": record.get("solicitation_number") or record.get("external_id"),
        "buyer": record.get("agency") or record.get("buyer"),
        "canonical_url": record.get("canonical_url") or record.get("detail_url") or record.get("source_url"),
        "normalized_title": normalize_title(record.get("title")),
        "title": record.get("title"),
        "first_seen_at": record.get("first_seen_at") or now,
        "last_seen_at": now,
        "source_created_at": record.get("source_created_at") or record.get("posted_at"),
        "source_modified_at": record.get("source_modified_at") or record.get("modified_at"),
        "current_status": record.get("status") or record.get("deadline_status") or "OPEN",
        "deadline": record.get("deadline") or record.get("response_deadline"),
        "version_fingerprint": fp,
        "last_processed_fingerprint": record.get("last_processed_fingerprint"),
        "m3_stage": record.get("m3_stage") or "NEW",
        "tracked_status": record.get("tracked_status") or False,
        "source_references": list(record.get("source_references") or []),
    }


class SolicitationInventory:
    """Lightweight in-memory/JSON-backed inventory — no silent discard."""

    def __init__(self) -> None:
        self._by_key: dict[str, dict[str, Any]] = {}

    def upsert(self, record: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Returns (identity, change_state). Retains all source references on cross-source match."""
        ident = build_solicitation_identity(record)
        key = ident["identity_key"]
        existing = self._by_key.get(key)
        src_ref = {
            "source_id": record.get("source_id"),
            "url": ident.get("canonical_url"),
            "source_record_id": ident.get("source_record_id"),
        }
        if existing is None:
            ident["source_references"] = [src_ref] if src_ref.get("source_id") else []
            ident["last_processed_fingerprint"] = None
            self._by_key[key] = ident
            return ident, CHG_NEW

        # Cross-source: same identity, add reference — do not collapse distinct opps with weak keys
        refs = list(existing.get("source_references") or [])
        if src_ref.get("source_id") and not any(
            r.get("source_id") == src_ref["source_id"] and r.get("source_record_id") == src_ref.get("source_record_id")
            for r in refs
        ):
            refs.append(src_ref)
        existing["source_references"] = refs
        existing["last_seen_at"] = _utc()
        prev_fp = existing.get("version_fingerprint")
        new_fp = ident["version_fingerprint"]
        if prev_fp == new_fp:
            change = CHG_UNCHANGED
        else:
            change = classify_field_change(existing, ident)
            existing["version_fingerprint"] = new_fp
            existing["deadline"] = ident.get("deadline") or existing.get("deadline")
            existing["current_status"] = ident.get("current_status") or existing.get("current_status")
            existing["title"] = ident.get("title") or existing.get("title")
        self._by_key[key] = existing
        return existing, change

    def get(self, key: str) -> dict[str, Any] | None:
        return self._by_key.get(key)

    def all_rows(self) -> list[dict[str, Any]]:
        return list(self._by_key.values())

    def mark_processed(self, key: str) -> None:
        row = self._by_key.get(key)
        if row:
            row["last_processed_fingerprint"] = row.get("version_fingerprint")

    def __len__(self) -> int:
        return len(self._by_key)


def classify_field_change(old: dict[str, Any], new: dict[str, Any]) -> str:
    from national_discovery_constants import (
        CHG_AMENDMENT,
        CHG_CANCELLED,
        CHG_CLOSED,
        CHG_CONTENT,
        CHG_DEADLINE,
        CHG_METADATA,
        CHG_STATUS,
    )

    old_status = str(old.get("current_status") or "").upper()
    new_status = str(new.get("current_status") or "").upper()
    if new_status in {"CANCELLED", "CANCELED"} and old_status != new_status:
        return CHG_CANCELLED
    if new_status in {"CLOSED", "AWARDED"} and old_status != new_status:
        return CHG_CLOSED if new_status == "CLOSED" else "AWARDED"
    if (old.get("deadline") or "") != (new.get("deadline") or "") and new.get("deadline"):
        return CHG_DEADLINE
    if old_status != new_status and new_status:
        return CHG_STATUS
    if (old.get("normalized_title") or "") != (new.get("normalized_title") or ""):
        return CHG_CONTENT
    # amendment marker in fingerprint inputs
    return CHG_METADATA if old.get("version_fingerprint") != new.get("version_fingerprint") else CHG_UNCHANGED


def should_skip_unchanged(identity: dict[str, Any], change_state: str) -> bool:
    return change_state == CHG_UNCHANGED and identity.get("last_processed_fingerprint") == identity.get(
        "version_fingerprint"
    )

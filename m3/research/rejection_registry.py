"""Permanent rejection fingerprints — anti-reacquisition for unsuitable solicitations.

Removing a rejected record from the working inventory must not delete protected evidence.
Unchanged rejected versions must not be re-downloaded or reintroduced.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings

REJECTION_CATEGORIES = frozenset(
    {
        "construction",
        "significant_installation",
        "mandatory_maintenance",
        "equipment_repair_service",
        "substantial_labor_staffing",
        "other_non_product_execution",
        "expired_closed",
    }
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def registry_root() -> Path:
    p = get_settings().data_root() / "rejection_registry"
    p.mkdir(parents=True, exist_ok=True)
    (p / "by_nb_id").mkdir(parents=True, exist_ok=True)
    (p / "by_fingerprint").mkdir(parents=True, exist_ok=True)
    return p


def fingerprint_key(
    *,
    provider: str,
    nb_id: str,
    solicitation_number: str | None,
    content_hash: str | None,
    amendment_id: str | None = None,
) -> str:
    raw = "|".join(
        [
            (provider or "nextbid").lower(),
            (nb_id or "").upper(),
            (solicitation_number or "").strip().lower(),
            (content_hash or "")[:64],
            (amendment_id or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_rejection(
    *,
    nb_id: str,
    provider: str = "nextbid",
    solicitation_number: str | None = None,
    agency: str | None = None,
    source_url: str | None = None,
    category: str,
    evidence_passage: str | None = None,
    evidence_ref: str | None = None,
    content_hash: str | None = None,
    amendment_id: str | None = None,
    reason: str,
) -> dict[str, Any]:
    if category not in REJECTION_CATEGORIES:
        category = "other_non_product_execution"
    fp = fingerprint_key(
        provider=provider,
        nb_id=nb_id,
        solicitation_number=solicitation_number,
        content_hash=content_hash,
        amendment_id=amendment_id,
    )
    row = {
        "fingerprint": fp,
        "provider": provider,
        "nb_id": nb_id,
        "canonical_solicitation_id": nb_id,
        "solicitation_number": solicitation_number,
        "agency": agency,
        "source_url": source_url,
        "rejection_category": category,
        "evidence_passage": (evidence_passage or "")[:500],
        "evidence_ref": evidence_ref,
        "content_hash": content_hash,
        "amendment_id": amendment_id,
        "rejected_at": _utc(),
        "reason": reason,
        "further_acquisition_prohibited": True,
        "reconsider_only_on_material_amendment": True,
    }
    root = registry_root()
    (root / "by_nb_id" / f"{nb_id}.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
    (root / "by_fingerprint" / f"{fp}.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
    # append-only index
    idx = root / "index.jsonl"
    with idx.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"fingerprint": fp, "nb_id": nb_id, "at": row["rejected_at"], "category": category}) + "\n")
    return row


def lookup_rejection(
    *,
    nb_id: str | None = None,
    provider: str = "nextbid",
    solicitation_number: str | None = None,
    content_hash: str | None = None,
    amendment_id: str | None = None,
) -> dict[str, Any] | None:
    root = registry_root()
    if nb_id:
        path = root / "by_nb_id" / f"{nb_id}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    if nb_id or content_hash:
        fp = fingerprint_key(
            provider=provider,
            nb_id=nb_id or "",
            solicitation_number=solicitation_number,
            content_hash=content_hash,
            amendment_id=amendment_id,
        )
        path = root / "by_fingerprint" / f"{fp}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    return None


def should_block_reacquisition(
    *,
    nb_id: str,
    content_hash: str | None = None,
    solicitation_number: str | None = None,
    amendment_id: str | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """True if unchanged rejected solicitation must not be paid-for again."""
    prior = lookup_rejection(nb_id=nb_id)
    if not prior:
        return False, None
    # Material amendment → allow reconsideration path (caller investigates)
    if amendment_id and amendment_id != prior.get("amendment_id"):
        return False, {**prior, "reconsideration_candidate": True, "new_amendment_id": amendment_id}
    prior_hash = prior.get("content_hash")
    if content_hash and prior_hash and content_hash != prior_hash:
        return False, {**prior, "reconsideration_candidate": True, "new_content_hash": content_hash}
    # Same identity / unchanged
    return True, prior


def remove_from_active_inventory(nb_id: str) -> dict[str, Any]:
    """Remove rejected NB from active opportunities.jsonl only (archive line, no evidence delete)."""
    inv = get_settings().data_root() / "nextbid_inventory" / "opportunities.jsonl"
    if not inv.exists():
        return {"removed": False, "reason": "no_active_inventory"}
    kept: list[str] = []
    removed_row = None
    with inv.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                kept.append(line if line.endswith("\n") else line + "\n")
                continue
            if str(row.get("nb_id") or "") == nb_id:
                removed_row = row
                continue
            kept.append(json.dumps(row, default=str) + "\n")
    if removed_row is None:
        return {"removed": False, "reason": "not_in_active_inventory"}
    # archival pointer (single line, not full evidence dup)
    arch = get_settings().data_root() / "nextbid_inventory" / "rejected_removed.jsonl"
    with arch.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "nb_id": nb_id,
                    "removed_at": _utc(),
                    "title": removed_row.get("title"),
                    "solicitation_number": removed_row.get("solicitation_number"),
                    "content_hash": removed_row.get("content_hash"),
                },
                default=str,
            )
            + "\n"
        )
    tmp = inv.with_suffix(".tmp")
    tmp.write_text("".join(kept), encoding="utf-8")
    tmp.replace(inv)
    return {"removed": True, "nb_id": nb_id}

"""Stable BidNet identity for expected-minus-harvested reconciliation."""

from __future__ import annotations

import re
from typing import Any

_ID_RE = re.compile(r"/(\d{6,})(?:\?|#|$)")
_CLOSED = {"CLOSED", "CANCELLED", "CANCELED", "AWARDED", "EXPIRED"}


def _digit_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if text.isdigit() and len(text) >= 5:
        return text
    return None


def stable_bidnet_key(row: dict[str, Any] | None) -> str | None:
    """Prefer the numeric solicitation id so national and state URLs collapse."""
    if not isinstance(row, dict):
        return None
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    for cand in (
        meta.get("bidnet_internal_id"),
        meta.get("bidnet_path_id"),
        row.get("solicitation_id"),
        row.get("solicitation_event_id"),
        row.get("external_id"),
    ):
        found = _digit_id(cand)
        if found:
            return f"id:{found}"
    for url in (
        row.get("detail_url"),
        row.get("authoritative_url"),
        meta.get("result_href"),
    ):
        match = _ID_RE.search(str(url or ""))
        if match:
            return f"id:{match.group(1)}"
    return None


def is_bidnet_record(row: dict[str, Any]) -> bool:
    parts = [
        str(row.get("platform") or ""),
        str(row.get("source_id") or ""),
        str(row.get("platform_family") or ""),
    ]
    prov = row.get("source_provenance") or []
    if isinstance(prov, list):
        for item in prov[:4]:
            if isinstance(item, dict):
                parts.append(str(item.get("source_id") or ""))
                parts.append(str(item.get("feed") or ""))
    blob = " ".join(parts).lower()
    return "bidnet" in blob


def is_closed_or_stale(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or row.get("freshness") or "").upper()
    if status in _CLOSED:
        return True
    return False

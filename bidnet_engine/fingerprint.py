"""Cheap source fingerprints and change classification."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bidnet_engine.models import CHANGE_TYPES


def _h(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def source_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    docs = row.get("attachments_metadata") or row.get("documents") or []
    doc_ids = []
    if isinstance(docs, list):
        for d in docs[:40]:
            if isinstance(d, dict):
                doc_ids.append(str(d.get("document_id") or d.get("url") or d.get("filename") or ""))
            else:
                doc_ids.append(str(d)[:80])
    fp = {
        "bidnet_id": row.get("stable_key") or row.get("solicitation_event_id"),
        "status": str(row.get("status") or row.get("freshness") or row.get("source_status") or ""),
        "modified": str(row.get("updated_at") or row.get("modified_at") or ""),
        "deadline": str(row.get("deadline") or ""),
        "title": str(row.get("title") or "")[:240],
        "description_hash": _h((row.get("description") or "")[:2000]),
        "buyer": str(row.get("buyer") or ""),
        "solicitation_number": str(row.get("solicitation_event_id") or ""),
        "detail_hash": _h(row.get("authoritative_url") or ""),
        "document_list_hash": _h("|".join(sorted(doc_ids))),
        "document_ids": doc_ids[:40],
        "amendment_count": int(row.get("amendment_count") or 0),
        "package_state": str(row.get("package_state") or ""),
    }
    fp["fingerprint"] = _h(json.dumps(fp, sort_keys=True, default=str))
    return fp


def classify_change(prev: dict[str, Any] | None, curr: dict[str, Any]) -> str:
    if not prev:
        return "NEW_OPPORTUNITY"
    if prev.get("fingerprint") == curr.get("fingerprint"):
        return "NO_CHANGE"
    if str(prev.get("status") or "").upper() in {"CLOSED", "EXPIRED", "CANCELLED"} and str(
        curr.get("status") or ""
    ).upper() not in {"CLOSED", "EXPIRED", "CANCELLED"}:
        return "REOPENED"
    if str(curr.get("status") or "").upper() in {"CLOSED", "EXPIRED", "CANCELLED", "AWARDED"}:
        return "CLOSED"
    if prev.get("status") != curr.get("status"):
        return "STATUS_CHANGED"
    if int(prev.get("amendment_count") or 0) < int(curr.get("amendment_count") or 0):
        return "NEW_AMENDMENT"
    if prev.get("document_list_hash") != curr.get("document_list_hash"):
        return "DOCUMENT_LIST_CHANGED"
    if prev.get("package_state") != curr.get("package_state"):
        return "PACKAGE_CHANGED"
    if prev.get("description_hash") != curr.get("description_hash") or prev.get("detail_hash") != curr.get(
        "detail_hash"
    ):
        return "DETAIL_CHANGED"
    if prev.get("deadline") != curr.get("deadline") and prev.get("title") == curr.get("title"):
        return "DEADLINE_ONLY"
    if prev.get("title") != curr.get("title") or prev.get("buyer") != curr.get("buyer"):
        return "METADATA_CHANGED"
    if prev.get("description_hash") != curr.get("description_hash"):
        return "LINE_RELEVANT_CHANGE"
    return "METADATA_CHANGED"


def assert_known_change(change: str) -> str:
    return change if change in CHANGE_TYPES else "METADATA_CHANGED"

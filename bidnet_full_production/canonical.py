"""BidNet canonical source record (Phase 2)."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from application_clock import now_utc


def _pg_from_url(url: str) -> str | None:
    try:
        q = parse_qs(urlparse(url).query)
        return (q.get("purchasingGroupId") or [None])[0]
    except Exception:
        return None


def _event_id_from_url(url: str) -> str | None:
    m = re.search(r"/(\d{7,})(?:/abstract|\?|$)", url or "")
    return m.group(1) if m else None


def build_canonical_record(
    *,
    meta: dict[str, Any],
    l23_row: dict[str, Any],
    parsed: dict[str, Any] | None,
    detail_stats: dict[str, Any] | None,
    buyer_info: dict[str, Any],
    package_state: str,
    product_class: str,
) -> dict[str, Any]:
    parsed = parsed or {}
    detail_stats = detail_stats or {}
    url = str(
        l23_row.get("authoritative_url")
        or meta.get("authoritative_url")
        or l23_row.get("detail_url")
        or ""
    )
    bidnet_id = _event_id_from_url(url) or (l23_row.get("raw_metadata") or {}).get("bidnet_internal_id")
    oid = meta.get("opportunity_id") or l23_row.get("canonical_id")
    cid = str(meta.get("canonical_opportunity_id") or (oid.split(":", 1)[1] if oid and ":" in oid else oid))

    docs = list(l23_row.get("attachments_metadata") or parsed.get("documents") or [])
    content_fp = hashlib.sha256(
        f"{parsed.get('title')}|{parsed.get('solicitation_number')}|{len(docs)}".encode()
    ).hexdigest()[:16]

    return {
        "kind": "BIDNET_CANONICAL_SOURCE_RECORD",
        "bidnet_opportunity_id": bidnet_id,
        "canonical_opportunity_id": cid,
        "opportunity_id": oid,
        "title": parsed.get("title") or l23_row.get("title") or meta.get("title"),
        "buyer": buyer_info.get("normalized_buyer_name") or meta.get("buyer"),
        "buyer_organization": buyer_info.get("buyer_organization"),
        "agency": parsed.get("agency") or l23_row.get("agency"),
        "department": buyer_info.get("department"),
        "solicitation_number": parsed.get("solicitation_number") or l23_row.get("solicitation_number"),
        "bid_number": l23_row.get("bid_number"),
        "project_number": l23_row.get("project_number"),
        "description": parsed.get("description") or parsed.get("overview") or l23_row.get("description"),
        "category": l23_row.get("category_bucket") or meta.get("category_bucket"),
        "location": parsed.get("location") or l23_row.get("location"),
        "state": buyer_info.get("state"),
        "city": buyer_info.get("city"),
        "posted_date": parsed.get("issue_date") or l23_row.get("issue_date"),
        "due_date": parsed.get("deadline") or l23_row.get("deadline") or meta.get("deadline"),
        "due_time_raw": parsed.get("deadline_raw") or l23_row.get("deadline_raw"),
        "timezone": parsed.get("timezone"),
        "status": l23_row.get("status") or "OPEN",
        "detail_url": l23_row.get("detail_url") or url,
        "list_url": l23_row.get("source_url"),
        "source_url": url,
        "buyer_url": buyer_info.get("official_procurement_url"),
        "official_procurement_url": buyer_info.get("official_procurement_url"),
        "portal_url": buyer_info.get("preferred_portal"),
        "purchasing_group_id": _pg_from_url(url),
        "documents_available": bool(docs),
        "document_count": len(docs),
        "document_inventory": docs,
        "registration_requirement": bool(parsed.get("auth_wall")),
        "account_requirement": bool(parsed.get("locked_fields")),
        "package_access_state": package_state,
        "product_classification": product_class,
        "detail_status": detail_stats.get("mapped_detail_status"),
        "auth_page_state": detail_stats.get("auth_page_state"),
        "buyer_identity_confidence": buyer_info.get("buyer_identity_confidence"),
        "source_version_hash": content_fp,
        "retrieved_at": now_utc().isoformat(),
        "provenance": {
            "bidnet_discovery_source": "LARGE_TEST_CORPUS_V1" if meta.get("source_bucket") == "BidNet" else "L23",
            "official_package_source": l23_row.get("official_package_source"),
            "detail_stats": detail_stats,
        },
    }

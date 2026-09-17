"""Shared HTML/JSON portal package recovery for BidNet / Bonfire / OpenGov / Public Purchase / State / DLA."""

from __future__ import annotations

from typing import Any

from portal_document_resolver import (
    DETAIL_PAGE,
    DOCUMENT_BYTES_RECOVERED,
    DOWNLOAD_ENDPOINT_UNRESOLVED,
    LOGIN_REQUIRED,
    PACKAGE_ROUTE_NOT_FOUND,
    REGISTRATION_REQUIRED,
    BOT_CHALLENGE,
    _utc,
    live_http_get,
)
from portal_resolvers.attachment_extract import (
    classify_access_from_html,
    extract_attachment_candidates,
    fetch_document_bytes,
)


def _existing_links(row: dict[str, Any]) -> list[Any]:
    links: list[Any] = []
    for key in ("document_links", "attachments", "documents"):
        val = row.get(key)
        if isinstance(val, list):
            links.extend(val)
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    for key in ("document_links", "attachments", "resourceLinks"):
        val = meta.get(key)
        if isinstance(val, list):
            links.extend(val)
    return links


def _map_access(access: str | None) -> tuple[str, str | None, str | None]:
    """Return (access_status, access_state, failure)."""
    if access == "AUTH_REQUIRED":
        return "AUTH_REQUIRED", "AUTH_REQUIRED", LOGIN_REQUIRED
    if access == "REGISTRATION_REQUIRED":
        return "REGISTRATION_REQUIRED", "REGISTRATION_REQUIRED", REGISTRATION_REQUIRED
    if access == "ACCESS_BLOCKED":
        return "ACCESS_BLOCKED", "BOT_PROTECTED", BOT_CHALLENGE
    return "UNKNOWN", None, None


def resolve_html_portal_documents(
    row: dict[str, Any],
    *,
    family: str,
    source_id: str,
    retrieval_method: str,
    max_downloads: int = 5,
) -> dict[str, Any]:
    """Generic public detail-page → attachment candidate → validated bytes recovery."""
    attempts: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    detail = row.get("detail_url") or row.get("source_url") or row.get("url")
    if not detail and not _existing_links(row):
        return {
            "family": family,
            "ok": False,
            "failure": PACKAGE_ROUTE_NOT_FOUND,
            "access_status": "UNKNOWN",
            "documents": [],
            "documents_missing": ["solicitation", "attachments"],
            "line_items": [],
            "attempts": attempts,
            "retrieval_method": retrieval_method,
        }

    html = ""
    json_payload = None
    access_hint = None
    if detail:
        hit = live_http_get(str(detail), source_id=f"{source_id}_detail")
        attempts.append(
            {
                "step": DETAIL_PAGE,
                "url": str(detail)[:220],
                "status": hit.get("status_code"),
                "ok": hit.get("ok"),
                "failure": hit.get("failure"),
                "at": _utc(),
            }
        )
        html = hit.get("text") or ""
        access_hint = classify_access_from_html(html)
        ctype = str(hit.get("content_type") or "").lower()
        if "json" in ctype or (html.strip()[:1] in {"{", "["}):
            try:
                import json

                json_payload = json.loads(hit.get("text") or "")
            except Exception:
                json_payload = None
        if not hit.get("ok") and access_hint:
            status, state, failure = _map_access(access_hint)
            return {
                "family": family,
                "ok": False,
                "failure": failure or hit.get("failure") or DOWNLOAD_ENDPOINT_UNRESOLVED,
                "access_status": status,
                "access_state": state,
                "documents": [],
                "documents_missing": ["attachments"],
                "line_items": [],
                "attempts": attempts,
                "detail_url": detail,
                "retrieval_method": retrieval_method,
                "status": status,
            }

    candidates = extract_attachment_candidates(
        html=html or None,
        json_payload=json_payload,
        base_url=str(detail) if detail else None,
        existing_links=_existing_links(row),
    )

    if access_hint and not candidates:
        status, state, failure = _map_access(access_hint)
        return {
            "family": family,
            "ok": False,
            "failure": failure or DOWNLOAD_ENDPOINT_UNRESOLVED,
            "access_status": status,
            "access_state": state,
            "documents": [],
            "documents_missing": ["attachments", "bid_package"],
            "line_items": [],
            "attempts": attempts,
            "detail_url": detail,
            "retrieval_method": retrieval_method,
            "status": status,
        }

    for cand in candidates[:max_downloads]:
        result = fetch_document_bytes(
            str(cand["url"]),
            source_id=source_id,
            referer=str(detail) if detail else None,
            document_type=str(cand.get("document_type") or "attachment"),
            title=str(cand.get("name") or "Portal Document"),
        )
        if not result:
            continue
        attempts.append(result["attempt"])
        if result.get("access") and not documents:
            access_hint = result["access"]
        if result.get("document"):
            doc = result["document"]
            doc["source"] = source_id
            doc["attachment_id"] = cand.get("attachment_id")
            documents.append(doc)

    if documents:
        return {
            "family": family,
            "ok": True,
            "failure": None,
            "access_status": "DOCUMENTS_FOUND",
            "documents": documents,
            "documents_missing": [],
            "line_items": [],
            "attempts": attempts,
            "detail_url": detail,
            "retrieval_method": retrieval_method,
            "status": DOCUMENT_BYTES_RECOVERED,
            "candidates_found": len(candidates),
        }

    if candidates:
        stubs = [
            {
                "url": c["url"].split("?")[0],
                "download_url": c["url"],
                "title": c.get("name"),
                "name": c.get("name"),
                "document_type": c.get("document_type"),
                "bytes_recovered": False,
                "authority": "authoritative",
                "source": f"{source_id}_metadata",
                "attachment_id": c.get("attachment_id"),
                "retrieved_at": _utc(),
                "status": "LINKED_UNFETCHED",
            }
            for c in candidates[:15]
        ]
        return {
            "family": family,
            "ok": True,
            "failure": None,
            "access_status": "DOCUMENTS_FOUND",
            "documents": stubs,
            "documents_missing": [],
            "line_items": [],
            "attempts": attempts,
            "detail_url": detail,
            "retrieval_method": retrieval_method,
            "status": "DOCUMENTS_FOUND",
            "bytes_pending": True,
            "candidates_found": len(candidates),
        }

    if access_hint:
        status, state, failure = _map_access(access_hint)
    else:
        status, state, failure = "NO_DOCUMENTS_AVAILABLE", None, DOWNLOAD_ENDPOINT_UNRESOLVED

    return {
        "family": family,
        "ok": False,
        "failure": failure,
        "access_status": status,
        "access_state": state,
        "documents": [],
        "documents_missing": ["solicitation", "attachments", "bid_package"],
        "line_items": [],
        "attempts": attempts,
        "detail_url": detail,
        "retrieval_method": retrieval_method,
        "status": status,
    }


def resolve_bonfire_documents(row: dict[str, Any]) -> dict[str, Any]:
    return resolve_html_portal_documents(
        row,
        family="BONFIRE",
        source_id="bonfire_portal",
        retrieval_method="bonfire_public_detail",
    )


def resolve_bidnet_documents(row: dict[str, Any]) -> dict[str, Any]:
    return resolve_html_portal_documents(
        row,
        family="BIDNET",
        source_id="bidnet_portal",
        retrieval_method="bidnet_public_abstract",
    )


def resolve_opengov_documents(row: dict[str, Any]) -> dict[str, Any]:
    return resolve_html_portal_documents(
        row,
        family="OPENGOV",
        source_id="opengov_portal",
        retrieval_method="opengov_public_detail",
    )


def resolve_public_purchase_documents(row: dict[str, Any]) -> dict[str, Any]:
    return resolve_html_portal_documents(
        row,
        family="PUBLIC_PURCHASE",
        source_id="public_purchase_portal",
        retrieval_method="public_purchase_detail",
    )


def resolve_state_portal_documents(row: dict[str, Any]) -> dict[str, Any]:
    return resolve_html_portal_documents(
        row,
        family="STATE_PORTAL",
        source_id="state_portal",
        retrieval_method="state_portal_document_tab",
        max_downloads=6,
    )


def resolve_dla_documents(row: dict[str, Any]) -> dict[str, Any]:
    """DLA / DIBBS / federal alternate routes — prefer SAM notice recovery when linked."""
    url = str(row.get("detail_url") or row.get("source_url") or row.get("url") or "").lower()
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    sam_hint = (
        row.get("notice_id")
        or meta.get("noticeId")
        or meta.get("sam_notice_id")
        or ("sam.gov" in url)
    )
    if sam_hint:
        from portal_resolvers.sam import resolve_sam_documents

        res = resolve_sam_documents(row)
        res["family"] = "DLA"
        res["retrieval_method"] = "dla_sam_federal_route"
        return res
    return resolve_html_portal_documents(
        row,
        family="DLA",
        source_id="dla_dibbs_portal",
        retrieval_method="dla_public_detail",
    )

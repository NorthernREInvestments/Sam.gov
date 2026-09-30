"""Phase L.3 — original solicitation / submission path integrity."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc

ORIGINAL_SOLICITATION_SOURCE_UNVERIFIED = "ORIGINAL_SOLICITATION_SOURCE_UNVERIFIED"
AUTHORITATIVE = "AUTHORITATIVE"
DISCOVERY_ONLY = "DISCOVERY_ONLY"
AGGREGATOR = "AGGREGATOR"

_AGGREGATOR_HOSTS = (
    "highergov.com",
    "govtribe.com",
    "bloomberg.com",
    "google.com",
    "bing.com",
    "duckduckgo.com",
)


def _utc() -> str:
    return now_utc().isoformat()


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def resolve_original_solicitation(row: dict[str, Any]) -> dict[str, Any]:
    """
    Separate DISCOVERY_SOURCE from AUTHORITATIVE_SOLICITATION_SOURCE.
    Bid submission must trace to authoritative source.
    """
    discovery_url = (
        row.get("discovery_url")
        or row.get("found_via_url")
        or row.get("aggregator_url")
        or row.get("source_url")
    )
    auth_url = (
        row.get("original_posting_url")
        or row.get("solicitation_url")
        or row.get("notice_url")
        or row.get("portal_url")
        or row.get("ui_link")
        or row.get("sam_url")
    )
    # If only one URL and it's not an aggregator, treat as authoritative
    if not auth_url and discovery_url and _host(discovery_url) not in _AGGREGATOR_HOSTS:
        auth_url = discovery_url

    discovery_host = _host(str(discovery_url) if discovery_url else "")
    auth_host = _host(str(auth_url) if auth_url else "")
    discovery_is_agg = any(a in discovery_host for a in _AGGREGATOR_HOSTS)

    source_of_truth = AUTHORITATIVE if auth_url and not any(a in auth_host for a in _AGGREGATOR_HOSTS) else (
        AGGREGATOR if discovery_is_agg and not auth_url else DISCOVERY_ONLY
    )
    verified = bool(auth_url) and source_of_truth == AUTHORITATIVE

    return {
        "kind": "OriginalSolicitationLocation",
        "issuing_agency": row.get("agency") or row.get("buyer") or row.get("department"),
        "original_procurement_portal": auth_host or row.get("portal") or row.get("source_id"),
        "original_posting_url": auth_url,
        "solicitation_number": row.get("solicitation_id") or row.get("notice_id") or row.get("solicitation_number"),
        "direct_solicitation_document_url": row.get("document_url") or row.get("attachment_url"),
        "attachments": row.get("attachments") or row.get("resource_links") or [],
        "amendments": row.get("amendments") or [],
        "latest_amendment_version": row.get("latest_amendment") or row.get("amendment_number"),
        "authoritative_deadline": row.get("response_deadline") or row.get("due_date") or row.get("offer_due_date"),
        "timezone": row.get("deadline_timezone") or row.get("timezone"),
        "submission_method": row.get("submission_method") or row.get("how_to_submit"),
        "submission_location": row.get("submission_location") or row.get("submit_to"),
        "portal_upload_link": row.get("portal_upload_link") or auth_url,
        "bid_email": row.get("bid_email") or row.get("submission_email"),
        "physical_delivery_address": row.get("physical_delivery_address"),
        "registration_login_requirements": row.get("registration_requirements"),
        "instructions_page": row.get("instructions_url") or auth_url,
        "source_of_truth_status": source_of_truth if verified else ORIGINAL_SOLICITATION_SOURCE_UNVERIFIED,
        "discovery_source": {
            "url": discovery_url,
            "host": discovery_host or None,
            "is_aggregator": discovery_is_agg,
        },
        "authoritative_solicitation_source": {
            "url": auth_url,
            "host": auth_host or None,
            "verified": verified,
        },
        "original_source_verified": verified,
        "bid_readiness_blocker": None
        if verified
        else ORIGINAL_SOLICITATION_SOURCE_UNVERIFIED,
        "resolved_at": _utc(),
    }


def submission_path_checklist(row: dict[str, Any], *, original: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pre-bid submission path verification checklist (strict)."""
    original = original or resolve_original_solicitation(row)
    checks = {
        "solicitation_still_open": None,  # unknown without live re-fetch
        "latest_amendment_reviewed": bool(original.get("latest_amendment_version") or not original.get("amendments")),
        "exact_due_date": bool(original.get("authoritative_deadline")),
        "exact_due_time": bool(row.get("due_time") or (str(original.get("authoritative_deadline") or "").count(":") >= 1)),
        "timezone": bool(original.get("timezone")),
        "submission_method_known": bool(original.get("submission_method") or original.get("portal_upload_link") or original.get("bid_email")),
        "correct_portal": bool(original.get("original_source_verified")),
        "vendor_registration_noted": True,  # informational
        "required_forms_listed": bool(row.get("required_forms") or original.get("attachments")),
        "acknowledgement_requirements": bool(row.get("acknowledgement_required") is not None),
    }
    unresolved = [k for k, v in checks.items() if v is False]
    return {
        "kind": "SubmissionPathChecklist",
        "checks": checks,
        "unresolved": unresolved,
        "submission_path_ready": bool(original.get("original_source_verified")) and not unresolved,
        "note": "Do not infer submission instructions from aggregator sites.",
    }

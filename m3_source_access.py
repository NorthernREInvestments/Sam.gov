"""Source access queue — registration/auth as actionable access, not dead deals."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from m3_evidence_constants import (
    SA_AUTH_REQUIRED,
    SA_BOT_PROTECTED,
    SA_CREDENTIALS_AVAILABLE,
    SA_PUBLIC,
    SA_PUBLIC_METADATA_ONLY,
    SA_REGISTRATION_REQUIRED,
)
from m3_source_credentials import get_credential, list_credentials


def derive_source_access_state(row: dict[str, Any]) -> str:
    existing = str(row.get("source_access_state") or "").upper()
    if existing:
        return existing
    pkg = str(row.get("package_access") or "").upper()
    if pkg in {"AUTH_GATED", "LOGIN_REQUIRED"}:
        return SA_AUTH_REQUIRED
    if pkg == "BOT_PROTECTED":
        return SA_BOT_PROTECTED
    if "REGISTRATION" in pkg:
        return SA_REGISTRATION_REQUIRED
    portal = row.get("portal_resolution") or {}
    if str(portal.get("access_state") or "").upper() in {
        SA_AUTH_REQUIRED,
        SA_REGISTRATION_REQUIRED,
        SA_BOT_PROTECTED,
    }:
        return str(portal.get("access_state")).upper()
    if str(portal.get("failure") or "").upper() in {"BOT_CHALLENGE", "BOT_PROTECTED"}:
        return SA_BOT_PROTECTED
    if str(portal.get("failure") or "").upper() in {"REGISTRATION_REQUIRED", "LOGIN_REQUIRED"}:
        return (
            SA_REGISTRATION_REQUIRED
            if "REGISTRATION" in str(portal.get("failure") or "").upper()
            else SA_AUTH_REQUIRED
        )
    if row.get("detail_url") or row.get("source_url"):
        return SA_PUBLIC if row.get("documents") or row.get("line_items") else SA_PUBLIC_METADATA_ONLY
    return SA_PUBLIC_METADATA_ONLY


def source_access_queue(opportunities: list[dict[str, Any]]) -> dict[str, Any]:
    """Rank portals needing registration by likely M3 value."""
    creds = list_credentials(include_secrets=False).get("portals") or {}
    buckets: dict[str, dict[str, Any]] = {}
    for row in opportunities:
        access = derive_source_access_state(row)
        if access not in {SA_AUTH_REQUIRED, SA_REGISTRATION_REQUIRED, SA_BOT_PROTECTED}:
            continue
        source = str(row.get("source_id") or row.get("platform_family") or "unknown")
        b = buckets.setdefault(
            source,
            {
                "portal": source,
                "source_id": source,
                "access_state": access,
                "blocked_opportunities": 0,
                "product_relevant_estimate": 0,
                "titles": [],
                "registration_url": None,
                "login_url": None,
                "credentials_configured": False,
                "account_status": None,
                "last_successful_login": None,
                "auth_failure_reason": None,
                "operator_action_required": (
                    "BOT_PROTECTED_MANUAL_BROWSER_REQUIRED"
                    if access == SA_BOT_PROTECTED
                    else "ENTER_CREDENTIALS_OR_REGISTER"
                ),
                "estimated_source_value": 0,
            },
        )
        b["blocked_opportunities"] += 1
        if access == SA_BOT_PROTECTED:
            b["access_state"] = SA_BOT_PROTECTED
            b["operator_action_required"] = "BOT_PROTECTED_MANUAL_BROWSER_REQUIRED"
        deal = str(row.get("deal_type") or "")
        cat = str(row.get("product_category") or "")
        if deal == "PRODUCT_RESALE" or cat not in {"", "UNKNOWN", "LIKELY_SERVICE_FALSE_POSITIVE"}:
            b["product_relevant_estimate"] += 1
        if len(b["titles"]) < 5:
            b["titles"].append(str(row.get("title") or "")[:80])
        portal = row.get("portal_resolution") or {}
        if not b["registration_url"]:
            b["registration_url"] = (
                row.get("registration_url")
                or portal.get("registration_url")
                or row.get("detail_url")
                or row.get("source_url")
            )
        if not b["login_url"]:
            b["login_url"] = row.get("login_url") or portal.get("login_url") or portal.get("opengov_url") or b["registration_url"]

    for source, b in buckets.items():
        cred = creds.get(source) or get_credential(source)
        if cred:
            b["credentials_configured"] = bool(cred.get("password_set") or cred.get("username"))
            b["account_status"] = cred.get("account_status")
            b["last_successful_login"] = cred.get("last_successful_login")
            fail = cred.get("last_failure") or {}
            b["auth_failure_reason"] = fail.get("reason") if isinstance(fail, dict) else None
            if b["credentials_configured"]:
                b["operator_action_required"] = (
                    "MANUAL_BROWSER_LOGIN"
                    if cred.get("mfa_or_manual_login_required")
                    else "RETRY_AUTHENTICATED_ACCESS"
                )
                b["access_state_if_creds"] = SA_CREDENTIALS_AVAILABLE
        # Value score: blocked * product relevance weight + uniqueness proxy
        b["estimated_source_value"] = b["blocked_opportunities"] * 10 + b["product_relevant_estimate"] * 25
        if b["credentials_configured"]:
            b["estimated_source_value"] += 15

    ranked = sorted(buckets.values(), key=lambda x: (-x["estimated_source_value"], -x["blocked_opportunities"]))
    return {
        "kind": "M3SourceAccessQueue",
        "count": len(ranked),
        "portals": ranked,
        "note": "Do not register everywhere — highest-value portals first. No autonomous registration.",
    }

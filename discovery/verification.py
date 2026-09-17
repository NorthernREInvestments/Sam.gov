"""Live verification — promote/downgrade based on real validation evidence."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

from discovery.constants import (
    ADAPTER_AUTH_REQUIRED,
    ADAPTER_BLOCKED,
    ADAPTER_BROKEN,
    ADAPTER_DEGRADED,
    ADAPTER_LIVE_VERIFIED,
    ADAPTER_UNVERIFIED_LIVE,
    HEALTH_AUTH_REQUIRED,
    HEALTH_BLOCKED,
    HEALTH_BROKEN,
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
)


def map_validation_to_adapter_status(
    *,
    http_status: int | None,
    validation: dict[str, Any],
    records_found: int,
    parser_ok: bool,
) -> str:
    """
    Real validation → adapter status.
    404/410/403/login/CAPTCHA cannot be LIVE_VERIFIED.
    Zero records alone cannot prove health / LIVE_VERIFIED.
    """
    failure = (validation.get("failure_type") or "").upper()
    health = (validation.get("health_status") or "").upper()

    if http_status in {404, 410} or failure.startswith("HTTP_404") or failure.startswith("HTTP_410"):
        return ADAPTER_BROKEN
    if http_status in {401, 403} or failure in {"AUTH_REQUIRED"} or health == HEALTH_AUTH_REQUIRED:
        return ADAPTER_AUTH_REQUIRED
    if failure in {"CAPTCHA", "BLOCKED"} or health == HEALTH_BLOCKED:
        return ADAPTER_BLOCKED
    if failure == "PARSER_SCHEMA_CHANGE" or health == HEALTH_BROKEN:
        return ADAPTER_BROKEN
    if not validation.get("valid"):
        return ADAPTER_BROKEN

    # Valid response with recognizable layout
    if not parser_ok:
        return ADAPTER_DEGRADED

    # Zero records with recognizable layout → DEGRADED (not LIVE_VERIFIED)
    # Successful promotion requires at least one parsed record OR explicit empty-ok with strong signals
    if records_found <= 0:
        return ADAPTER_DEGRADED

    if health in {HEALTH_HEALTHY, HEALTH_DEGRADED} and records_found > 0 and parser_ok:
        return ADAPTER_LIVE_VERIFIED

    return ADAPTER_UNVERIFIED_LIVE


_JUNK_TITLES = {
    "status",
    "title",
    "welcome",
    "search for open bids",
    "event title",
    "solicitation",
    "description",
    "best deal",
}


def _quality_opportunities(opportunities: list[Any]) -> list[Any]:
    """Filter parser junk so promotion requires real solicitation-like titles."""
    good = []
    for o in opportunities or []:
        title = (getattr(o, "title", None) or (o.get("title") if isinstance(o, dict) else "") or "").strip()
        if not title or len(title) < 12:
            continue
        if title.lower() in _JUNK_TITLES:
            continue
        if title.lower().startswith("welcome"):
            continue
        if "flat membership plans" in title.lower() or "benefits of free registration" in title.lower():
            continue
        good.append(o)
    return good


def build_validation_evidence(
    *,
    source_id: str,
    list_url: str,
    http_status: int | None,
    validation: dict[str, Any],
    records_found: int,
    opportunities: list[Any],
    requests: int,
    platform_family: str | None = None,
) -> dict[str, Any]:
    quality = _quality_opportunities(opportunities)
    quality_count = len(quality)
    parser_ok = quality_count > 0 and bool(validation.get("valid") or quality_count > 0)
    # Re-evaluate with quality count; soft-login pages with real listings can verify
    effective_validation = dict(validation or {})
    if quality_count > 0 and effective_validation.get("failure_type") in {"AUTH_REQUIRED", "CAPTCHA", "BLOCKED"}:
        # Soft chrome only — listings prove public view
        effective_validation = {
            **effective_validation,
            "valid": True,
            "health_status": HEALTH_DEGRADED,
            "failure_type": None,
            "warnings": list(effective_validation.get("warnings") or []) + ["soft_auth_chrome_overridden_by_listings"],
        }
    status = map_validation_to_adapter_status(
        http_status=http_status,
        validation=effective_validation,
        records_found=quality_count,
        parser_ok=quality_count > 0,
    )
    sample = None
    sample_title = None
    if quality:
        o0 = quality[0]
        sample = getattr(o0, "external_id", None) or (o0.get("external_id") if isinstance(o0, dict) else None)
        sample_title = getattr(o0, "title", None) or (o0.get("title") if isinstance(o0, dict) else None)

    return {
        "source_id": source_id,
        "list_url": list_url,
        "platform_family": platform_family,
        "requests": requests,
        "http_status": http_status,
        "records_visible": quality_count,
        "raw_records_parsed": records_found,
        "parser_success": quality_count > 0,
        "validation": effective_validation,
        "sample_external_id": sample,
        "sample_title": sample_title,
        "adapter_status": status,
        "live_verified": status == ADAPTER_LIVE_VERIFIED,
        "validated_at": now_utc().isoformat(),
        "SAM": 0,
        "OpenAI": 0,
        "USAspending": 0,
        "paid": 0,
    }


def persist_validation_result(session: Any, evidence: dict[str, Any]) -> dict[str, Any]:
    """Persist validation evidence on DiscoverySource — not opportunities."""
    from models import DiscoverySource

    sid = evidence["source_id"]
    row = session.query(DiscoverySource).filter_by(source_id=sid).first()
    if row is None:
        return {"persisted": False, "reason": "source_not_in_registry", "LIVE_API_REQUESTS": 0}

    row.adapter_status = evidence["adapter_status"]
    row.health_status = (evidence.get("validation") or {}).get("health_status") or row.health_status
    row.last_live_verified_at = (
        now_utc() if evidence.get("live_verified") else row.last_live_verified_at
    )
    row.last_live_validation_result = evidence["adapter_status"]
    row.last_live_http_status = evidence.get("http_status")
    row.last_live_record_count = evidence.get("records_visible") or 0
    row.last_live_sample_external_id = evidence.get("sample_external_id")
    row.validation_notes = (
        f"sample={evidence.get('sample_title')!r}; "
        f"warnings={(evidence.get('validation') or {}).get('warnings')}"
    )[:2000]
    meta = dict(row.metadata_json or {})
    meta["last_validation"] = {
        k: evidence.get(k)
        for k in (
            "validated_at",
            "adapter_status",
            "http_status",
            "records_visible",
            "parser_success",
            "sample_title",
            "list_url",
        )
    }
    meta["live_verified"] = bool(evidence.get("live_verified"))
    row.metadata_json = meta
    # Only enable for LIVE_VERIFIED
    if evidence["adapter_status"] == ADAPTER_LIVE_VERIFIED:
        row.enabled = True
    elif evidence["adapter_status"] in {
        ADAPTER_AUTH_REQUIRED,
        ADAPTER_BLOCKED,
        ADAPTER_BROKEN,
    }:
        row.enabled = False
    session.flush()
    return {"persisted": True, "source_id": sid, "adapter_status": evidence["adapter_status"], "LIVE_API_REQUESTS": 0}

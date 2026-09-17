"""Source/agency onboarding — detect platform, suggest adapter, no fetch unless authorized."""

from __future__ import annotations

from typing import Any

from discovery.live_fetchers import PLATFORM_TO_FETCHER, get_fetcher_for_platform
from discovery.platform_detect import detect_platform


def onboard_source(
    *,
    name: str,
    url: str,
    jurisdiction: str | None = None,
    agency_type: str | None = None,
    state_code: str | None = None,
    authorize_fetch: bool = False,
) -> dict[str, Any]:
    """
    Create registry suggestion. Fetches nothing unless authorize_fetch=True
    (still not called during tests/build).
    """
    detection = detect_platform(url)
    platform = detection.get("platform") or "UNKNOWN"
    fetcher = get_fetcher_for_platform(platform)
    adapter_family = PLATFORM_TO_FETCHER.get(platform)
    live_capable = bool(fetcher and url)

    record = {
        "source_name": name,
        "list_url": url,
        "jurisdiction": jurisdiction,
        "buyer_type": agency_type,
        "state_code": state_code,
        "platform_family": platform,
        "adapter_family": adapter_family,
        "live_capable": live_capable,
        "adapter_status": "UNVERIFIED_LIVE" if live_capable else "PLANNED",
        "validation_needed": True,
        "detection": detection,
        "fetched": False,
        "note": "Validation needed before enabling. No fetch performed."
        if not authorize_fetch
        else "authorize_fetch set — caller must invoke live runner separately",
    }
    if authorize_fetch:
        record["note"] = "Onboarding does not auto-crawl; use controlled live runner"
    return {
        "suggested_record": record,
        "LIVE_API_REQUESTS": 0,
        "fetched": False,
    }


def persist_onboarded_agency(session: Any, suggested: dict[str, Any]) -> dict[str, Any]:
    from models import DiscoveryAgency

    rec = suggested.get("suggested_record") or suggested
    key = (rec.get("source_name") or "agency").lower().replace(" ", "_")[:120]
    existing = session.query(DiscoveryAgency).filter_by(agency_key=key).first()
    if existing:
        existing.procurement_url = rec.get("list_url")
        existing.platform_family = rec.get("platform_family")
        session.flush()
        return {"id": existing.id, "updated": True, "LIVE_API_REQUESTS": 0}
    row = DiscoveryAgency(
        agency_key=key,
        name=rec.get("source_name") or key,
        buyer_type=rec.get("buyer_type") or "OTHER_PUBLIC",
        state_code=rec.get("state_code"),
        jurisdiction=rec.get("jurisdiction"),
        procurement_url=rec.get("list_url"),
        platform_family=rec.get("platform_family"),
        source_id=rec.get("adapter_family"),
        platform_detection=rec.get("platform_family"),
        enabled=False,
        notes="validation_needed",
        metadata_json={"live_capable": rec.get("live_capable"), "validation_needed": True},
    )
    session.add(row)
    session.flush()
    return {"id": row.id, "created": True, "LIVE_API_REQUESTS": 0}

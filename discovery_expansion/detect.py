"""Platform-family detection enhancements + routing hints."""

from __future__ import annotations

from typing import Any

from discovery.platform_detect import detect_platform
from discovery_expansion.constants import (
    ADAPTER_AUTH_REQUIRED,
    ADAPTER_BROKEN,
    ADAPTER_METADATA_ONLY,
    ADAPTER_NOT_IMPLEMENTED,
    ADAPTER_PARTIAL,
    ADAPTER_WORKING,
    FREE_ACCOUNT_REQUIRED,
    PUBLIC_ANONYMOUS,
)


def detect_and_route(url: str, *, html_snippet: str | None = None) -> dict[str, Any]:
    """Unknown procurement URL → platform family → adapter hint."""
    det = detect_platform(url, html_snippet=html_snippet)
    platform = str(det.get("platform") or "UNKNOWN")
    adapter_map = {
        "Bonfire": "live_bonfire",
        "OpenGov": "live_opengov",  # authenticated harvest via opengov_auth when credentials present
        "IonWave": "live_ionwave",
        "PlanetBids": "live_planetbids",
        "BidNet": "live_bidnet",
        "DemandStar": "live_demandstar",
        "Jaggaer": "live_jaggaer",
        "PublicPurchase": "live_public_purchase",
        "Periscope": None,  # not implemented as live fetcher
        "SimpleHTML": "live_simple_html",
        "JSON": "live_json",
        "RSS": "live_rss",
    }
    adapter = adapter_map.get(platform)
    return {
        **det,
        "platform_family": platform,
        "adapter_family": adapter,
        "adapter_status": ADAPTER_NOT_IMPLEMENTED if adapter is None and platform == "Periscope" else (
            ADAPTER_WORKING if adapter else ADAPTER_METADATA_ONLY
        ),
        "route": f"use:{adapter}" if adapter else "research_needed",
    }


def classify_access_from_stop_reason(stop_reason: str | None, *, ok: bool = False) -> str:
    r = str(stop_reason or "").upper()
    if "AUTH" in r or "LOGIN" in r:
        return FREE_ACCOUNT_REQUIRED
    if "CAPTCHA" in r or "BOT" in r:
        return FREE_ACCOUNT_REQUIRED
    if ok:
        return PUBLIC_ANONYMOUS
    return PUBLIC_ANONYMOUS


def adapter_status_from_health(row: dict[str, Any]) -> str:
    if not row:
        return ADAPTER_NOT_IMPLEMENTED
    if row.get("ok") and (row.get("raw") or 0) > 0:
        return ADAPTER_WORKING
    reason = str(row.get("source_stop_reason") or row.get("root_cause") or "").upper()
    if "AUTH" in reason or "CAPTCHA" in reason:
        return ADAPTER_AUTH_REQUIRED
    if "PARSER" in reason or "SCHEMA" in reason:
        return ADAPTER_BROKEN
    if "BOT" in reason or "BLOCKED" in reason:
        return ADAPTER_AUTH_REQUIRED
    if row.get("ok") and (row.get("raw") or 0) == 0:
        return ADAPTER_PARTIAL
    if "EXCEPTION" in reason or "HTTP_404" in reason:
        return ADAPTER_BROKEN
    return ADAPTER_PARTIAL
